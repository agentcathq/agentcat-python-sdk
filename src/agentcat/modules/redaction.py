"""PII redaction for AgentCat logs."""

import asyncio
import inspect
import threading
from typing import Any, TYPE_CHECKING, Callable, Coroutine, Set

from pydantic import BaseModel

if TYPE_CHECKING:
    from agentcat.types import Event, UnredactedEvent


# Set of field names that should be protected from redaction.
# These fields contain system-level identifiers and metadata that
# need to be preserved for analytics tracking.
PROTECTED_FIELDS: Set[str] = {
    "session_id",
    "id",
    "project_id",
    "server",
    "identify_actor_given_id",
    "identify_actor_name",
    "identify_data",
    "resource_name",
    "event_type",
    "actor_id",
    "tags",
    "properties",
}


def _run_coroutine(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine to completion from synchronous code.

    The event queue processes events on worker threads with no running event
    loop, so the coroutine is resolved with `asyncio.run`. If a loop is already
    running in this thread (defensive; not the normal publish path), the
    coroutine is resolved on a short-lived helper thread instead, since
    `asyncio.run` cannot be called from inside a running loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Normal path: event-queue worker threads have no event loop.
        return asyncio.run(coro)

    # A loop is running in this thread — resolve on a fresh thread.
    outcome: dict[str, Any] = {}

    def _runner() -> None:
        try:
            outcome["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — propagated to caller below
            outcome["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _resolve_redacted_value(result: Any) -> Any:
    """Resolve a redaction result that may be an awaitable.

    Handles the edge case of a *sync* redaction function that returns an
    awaitable. Coroutine functions are detected upfront in
    `redact_strings_in_object` and resolved on a single event loop for the
    whole object instead of one loop per string.
    """
    if not inspect.isawaitable(result):
        return result

    async def _await() -> Any:
        return await result

    return _run_coroutine(_await())


def redact_strings_in_object(
    obj: Any,
    redact_fn: Callable[[str], str],
    path: str = "",
    is_protected: bool = False,
) -> Any:
    """
    Recursively applies a redaction function to all string values in an object.
    This ensures that sensitive information is removed from all string fields
    before events are sent to the analytics service.

    Async redaction functions are resolved on a single event loop for the whole
    object walk (not one loop per string).

    Args:
        obj: The object to redact strings from
        redact_fn: The redaction function to apply to each string (sync or async)
        path: The current path in the object tree (used to check protected fields)
        is_protected: Whether the current object/value is within a protected field

    Returns:
        A new object with all strings redacted
    """
    if inspect.iscoroutinefunction(redact_fn):
        return _run_coroutine(
            _redact_strings_in_object_async(obj, redact_fn, path, is_protected)
        )
    return _redact_strings_in_object_sync(obj, redact_fn, path, is_protected)


def _redact_strings_in_object_sync(
    obj: Any,
    redact_fn: Callable[[str], str],
    path: str,
    is_protected: bool,
) -> Any:
    """Walk `obj` applying a sync redaction function (see redact_strings_in_object)."""
    if obj is None:
        return obj

    # Handle strings
    if isinstance(obj, str):
        # Don't redact if this field or any parent field is protected
        if is_protected:
            return obj
        return _resolve_redacted_value(redact_fn(obj))

    # Handle arrays/lists
    if isinstance(obj, list):
        return [
            _redact_strings_in_object_sync(
                item, redact_fn, f"{path}[{index}]", is_protected
            )
            for index, item in enumerate(obj)
        ]

    # Handle dictionaries/objects
    if isinstance(obj, dict):
        redacted_obj = {}

        for key, value in obj.items():
            # Skip None values
            if value is None:
                continue

            # Build the path for nested fields
            field_path = f"{path}.{key}" if path else key
            # Check if this field is protected (only check at top level)
            is_field_protected = is_protected or (
                path == "" and key in PROTECTED_FIELDS
            )
            redacted_obj[key] = _redact_strings_in_object_sync(
                value, redact_fn, field_path, is_field_protected
            )

        return redacted_obj

    # For all other types (numbers, booleans, datetimes, etc.), return as-is
    return obj


async def _redact_strings_in_object_async(
    obj: Any,
    redact_fn: Callable[[str], Any],
    path: str,
    is_protected: bool,
) -> Any:
    """Async twin of `_redact_strings_in_object_sync` for coroutine redact fns.

    Driven once per redact call by `_run_coroutine`, so every redacted string
    shares one event loop.
    """
    if obj is None:
        return obj

    # Handle strings
    if isinstance(obj, str):
        # Don't redact if this field or any parent field is protected
        if is_protected:
            return obj
        return await redact_fn(obj)

    # Handle arrays/lists
    if isinstance(obj, list):
        return [
            await _redact_strings_in_object_async(
                item, redact_fn, f"{path}[{index}]", is_protected
            )
            for index, item in enumerate(obj)
        ]

    # Handle dictionaries/objects
    if isinstance(obj, dict):
        redacted_obj = {}

        for key, value in obj.items():
            # Skip None values
            if value is None:
                continue

            # Build the path for nested fields
            field_path = f"{path}.{key}" if path else key
            # Check if this field is protected (only check at top level)
            is_field_protected = is_protected or (
                path == "" and key in PROTECTED_FIELDS
            )
            redacted_obj[key] = await _redact_strings_in_object_async(
                value, redact_fn, field_path, is_field_protected
            )

        return redacted_obj

    # For all other types (numbers, booleans, datetimes, etc.), return as-is
    return obj


def redact_event(event: "UnredactedEvent", redact_fn: Callable[[str], str]) -> "Event":
    """
    Applies the customer's redaction function to all string fields in an Event object.
    This is the main entry point for redacting sensitive information from events
    before they are sent to the analytics service.

    Accepts either a Pydantic event model (the live publish path — the event is
    dumped, redacted recursively, and rebuilt) or a plain dict.

    Args:
        event: The event to redact
        redact_fn: The customer's redaction function (sync or async)

    Returns:
        A new event object with all strings redacted
    """
    if isinstance(event, BaseModel):
        # Dump the model to a plain dict (dropping the redaction_fn callable),
        # redact recursively, then rebuild the same model class. The dump came
        # from an already-validated model, so skip re-validation on rebuild.
        event_dict = event.model_dump(exclude_none=True, exclude={"redaction_fn"})
        redacted_dict = redact_strings_in_object(event_dict, redact_fn, "", False)
        return type(event).model_construct(**redacted_dict)

    return redact_strings_in_object(event, redact_fn, "", False)
