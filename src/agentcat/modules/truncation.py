"""Event truncation for AgentCat.

Enforces a maximum event payload size by truncating oversized string
values, limiting nesting depth and collection breadth, and detecting
circular references. Acts as a safety net — most events pass through
unchanged.
"""

from datetime import date, datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from agentcat.types import UnredactedEvent

from .logging import write_to_log

MAX_EVENT_BYTES = 102_400   # 100 KB total event size
MAX_STRING_BYTES = 32_768   # 32 KB per individual string
MAX_DEPTH = 10              # Max nesting depth for dicts/lists
MAX_BREADTH = 100           # Max items per dict/list
MIN_DEPTH = 1               # Never reduce depth below this to avoid type mismatches

# Field-level limits (mirrors the TypeScript SDK's field-cap layer).
# Applied unconditionally to every event, before the size-budget pass.
MAX_USER_INTENT_LENGTH = 2_048      # user_intent
MAX_ERROR_MESSAGE_LENGTH = 2_048    # error.message
MAX_RESOURCE_NAME_LENGTH = 256      # resource_name
MAX_METADATA_LENGTH = 256           # server_name/server_version/client_name/client_version
MAX_STACK_FRAMES = 50               # error.frames — keep first 25 + last 25 when over
MAX_CONTENT_TEXT_LENGTH = 32_768    # response.content[].text per text block

# Only these fields get truncated; all other top-level event fields pass through untouched.
TRUNCATABLE_FIELDS = {"parameters", "response", "error", "identify_data", "user_intent", "additional_properties"}


def _truncate_string(value: str, max_bytes: int = MAX_STRING_BYTES) -> str:
    """Truncate a string if its UTF-8 byte size exceeds *max_bytes*."""
    byte_size = len(value.encode("utf-8"))
    if byte_size <= max_bytes:
        return value

    marker = f"[string truncated by AgentCat from {byte_size} bytes]"
    marker_bytes = len(marker.encode("utf-8"))
    keep_bytes = max_bytes - marker_bytes

    if keep_bytes <= 0:
        return marker

    truncated = value.encode("utf-8")[:keep_bytes].decode("utf-8", errors="ignore")
    return truncated + marker


def _truncate_value(
    value: Any,
    *,
    max_depth: int = MAX_DEPTH,
    max_string_bytes: int = MAX_STRING_BYTES,
    max_breadth: int = MAX_BREADTH,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> Any:
    """Recursively walk a value and apply truncation limits."""
    if value is None or isinstance(value, (bool, int, float, datetime, date)):
        return value

    if isinstance(value, str):
        return _truncate_string(value, max_bytes=max_string_bytes)

    if _seen is None:
        _seen = set()

    obj_id = id(value)
    if obj_id in _seen:
        return "[circular reference]"

    _seen.add(obj_id)
    try:
        at_depth_limit = _depth >= max_depth

        if isinstance(value, dict):
            items = list(value.items())
            result = {}
            for i, (k, v) in enumerate(items):
                if i >= max_breadth:
                    remaining = len(items) - max_breadth
                    result["__truncated__"] = (
                        f"[... {remaining} more items truncated by AgentCat]"
                    )
                    break
                if at_depth_limit and isinstance(v, (dict, list, tuple)):
                    result[str(k)] = f"[nested content truncated by AgentCat at depth {max_depth}]"
                else:
                    result[str(k)] = _truncate_value(
                        v, max_depth=max_depth, max_string_bytes=max_string_bytes,
                        max_breadth=max_breadth,
                        _depth=_depth + 1, _seen=_seen,
                    )
            return result

        if isinstance(value, (list, tuple)):
            if at_depth_limit:
                return f"[nested content truncated by AgentCat at depth {max_depth}]"
            result_list = [
                _truncate_value(
                    item, max_depth=max_depth, max_string_bytes=max_string_bytes,
                    max_breadth=max_breadth,
                    _depth=_depth + 1, _seen=_seen,
                )
                for i, item in enumerate(value)
                if i < max_breadth
            ]
            if len(value) > max_breadth:
                remaining = len(value) - max_breadth
                result_list.append(
                    f"[... {remaining} more items truncated by AgentCat]"
                )
            return result_list

        if at_depth_limit:
            return f"[nested content truncated by AgentCat at depth {max_depth}]"

        # Fallback for unknown types — repr and truncate
        return _truncate_string(repr(value), max_bytes=max_string_bytes)
    finally:
        _seen.discard(obj_id)


# --- Field-level cap layer (mirrors TypeScript truncateEvent layer 1) ---


def _truncate_stack_frames(frames: Any) -> Any:
    """Trim a stack frame list to MAX_STACK_FRAMES, keeping first + last halves."""
    if not isinstance(frames, list) or len(frames) <= MAX_STACK_FRAMES:
        return frames
    half = MAX_STACK_FRAMES // 2
    return frames[:half] + frames[-half:]


def _truncate_response_content(response: Any) -> Any:
    """Cap each text content block in a response to MAX_CONTENT_TEXT_LENGTH bytes.

    Returns the original object unchanged (same identity) when no block
    needed truncation.
    """
    if not isinstance(response, dict):
        return response
    content = response.get("content")
    if not isinstance(content, list):
        return response

    changed = False
    new_content = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            capped = _truncate_string(block["text"], max_bytes=MAX_CONTENT_TEXT_LENGTH)
            if capped != block["text"]:
                block = {**block, "text": capped}
                changed = True
        new_content.append(block)

    if not changed:
        return response
    return {**response, "content": new_content}


def _apply_field_caps(event: "UnredactedEvent") -> "UnredactedEvent":
    """Apply per-field limits to an event, returning a copy only if changed.

    Mirrors the TypeScript SDK's field-level truncation layer, which runs
    unconditionally on every event before the 100 KB size-budget pass:
    - user_intent -> 2048
    - error.message -> 2048; error.frames -> first 25 + last 25 when > 50
    - resource_name -> 256
    - server_name / server_version / client_name / client_version -> 256
    - response.content[].text -> 32 KB per text block
    """
    updates: dict[str, Any] = {}

    for field_name, limit in (
        ("user_intent", MAX_USER_INTENT_LENGTH),
        ("resource_name", MAX_RESOURCE_NAME_LENGTH),
        ("server_name", MAX_METADATA_LENGTH),
        ("server_version", MAX_METADATA_LENGTH),
        ("client_name", MAX_METADATA_LENGTH),
        ("client_version", MAX_METADATA_LENGTH),
    ):
        value = getattr(event, field_name, None)
        if isinstance(value, str):
            capped = _truncate_string(value, max_bytes=limit)
            if capped != value:
                updates[field_name] = capped

    error = getattr(event, "error", None)
    if isinstance(error, dict):
        new_error = dict(error)
        error_changed = False

        message = new_error.get("message")
        if isinstance(message, str):
            capped = _truncate_string(message, max_bytes=MAX_ERROR_MESSAGE_LENGTH)
            if capped != message:
                new_error["message"] = capped
                error_changed = True

        frames = new_error.get("frames")
        trimmed_frames = _truncate_stack_frames(frames)
        if trimmed_frames is not frames:
            new_error["frames"] = trimmed_frames
            error_changed = True

        if error_changed:
            updates["error"] = new_error

    response = getattr(event, "response", None)
    capped_response = _truncate_response_content(response)
    if capped_response is not response:
        updates["response"] = capped_response

    if not updates:
        return event
    return event.model_copy(update=updates)


def truncate_event(event: "UnredactedEvent | None") -> "UnredactedEvent | None":
    """Apply layered truncation to *event*.

    Layer 1 — field-level caps (unconditional, mirrors the TypeScript SDK):
    user_intent, resource_name, server/client metadata, error.message,
    error.frames, and response content text blocks are capped regardless
    of overall event size.

    Layer 2 — size budget: if the event still exceeds MAX_EVENT_BYTES,
    uses a size-targeted normalization strategy: normalize with the
    current limits, check JSON byte size, and if still over the limit tighten
    limits and re-normalize until it fits.

    Each pass halves the per-string byte limit and (once MIN_DEPTH is reached)
    reduces breadth. Depth never goes below MIN_DEPTH to avoid replacing
    dict-typed fields with string markers that fail model validation.

    - Checks serialized JSON byte size first (fast path)
    - Never mutates the original event
    - Returns original event unchanged if under limit
    - Returns last valid truncated candidate if loop exhausts limits
    """
    if event is None:
        return None

    try:
        # Layer 1: field-level caps, applied to every event unconditionally
        event = _apply_field_caps(event)

        # Layer 2: overall size budget
        serialized_bytes = event.model_dump_json().encode("utf-8")
        byte_size = len(serialized_bytes)
        if byte_size <= MAX_EVENT_BYTES:
            return event

        write_to_log(
            f"Event {event.id or 'unknown'} exceeds {MAX_EVENT_BYTES} bytes "
            f"({byte_size} bytes), truncating"
        )

        event_cls = type(event)
        depth = MAX_DEPTH
        string_bytes = MAX_STRING_BYTES
        breadth = MAX_BREADTH
        candidate = None

        while string_bytes >= 1:
            # Always start from a fresh dump to avoid compounding artifacts
            event_dict = event.model_dump()
            for field_name in TRUNCATABLE_FIELDS:
                if field_name in event_dict and event_dict[field_name] is not None:
                    if isinstance(event_dict[field_name], str):
                        event_dict[field_name] = _truncate_string(event_dict[field_name], max_bytes=string_bytes)
                    else:
                        event_dict[field_name] = _truncate_value(
                            event_dict[field_name],
                            max_depth=depth,
                            max_string_bytes=string_bytes,
                            max_breadth=breadth,
                        )
            candidate = event_cls.model_validate(event_dict)
            result_bytes = len(candidate.model_dump_json().encode("utf-8"))
            if result_bytes <= MAX_EVENT_BYTES:
                return candidate
            write_to_log(
                f"Event still {result_bytes} bytes at depth={depth} "
                f"string_limit={string_bytes} breadth={breadth}, tightening limits"
            )
            # Tighten: reduce depth (down to MIN_DEPTH), halve string limit
            if depth > MIN_DEPTH:
                depth -= 1
            string_bytes //= 2
            # Breadth reduction as fallback once depth is at minimum
            if depth <= MIN_DEPTH and breadth > 1:
                breadth //= 2

        return candidate

    except Exception as e:
        write_to_log(f"WARNING: Truncation failed for event {event.id or 'unknown'}: {e}")
        return event
