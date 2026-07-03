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
MAX_STRING_BYTES = 10_240   # 10 KB per individual string
MAX_DEPTH = 5               # Max nesting depth for dicts/lists
MAX_BREADTH = 500           # Max items per dict/list
MIN_DEPTH = 1               # Never reduce depth below this to avoid type mismatches

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


def truncate_event(event: "UnredactedEvent | None") -> "UnredactedEvent | None":
    """Return a truncated copy of *event* if it exceeds MAX_EVENT_BYTES.

    Uses size-targeted normalization strategy: normalize with the
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
