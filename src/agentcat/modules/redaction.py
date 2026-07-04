"""PII redaction for AgentCat logs."""

from typing import Any, TYPE_CHECKING, Callable, Set

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

    Args:
        obj: The object to redact strings from
        redact_fn: The redaction function to apply to each string
        path: The current path in the object tree (used to check protected fields)
        is_protected: Whether the current object/value is within a protected field

    Returns:
        A new object with all strings redacted
    """
    if obj is None:
        return obj

    # Handle strings
    if isinstance(obj, str):
        # Don't redact if this field or any parent field is protected
        if is_protected:
            return obj
        return redact_fn(obj)

    # Handle arrays/lists
    if isinstance(obj, list):
        return [
            redact_strings_in_object(item, redact_fn, f"{path}[{index}]", is_protected)
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
            redacted_obj[key] = redact_strings_in_object(
                value, redact_fn, field_path, is_field_protected
            )

        return redacted_obj

    # For all other types (numbers, booleans, etc.), return as-is
    return obj


def redact_event(event: "UnredactedEvent", redact_fn: Callable[[str], str]) -> "Event":
    """
    Applies the customer's redaction function to all string fields in an Event object.
    This is the main entry point for redacting sensitive information from events
    before they are sent to the analytics service.

    Args:
        event: The event to redact
        redact_fn: The customer's redaction function

    Returns:
        A new event object with all strings redacted
    """
    return redact_strings_in_object(event, redact_fn, "", False)
