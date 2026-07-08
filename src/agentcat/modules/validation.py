"""Client-side validation for customer-defined event tags."""

import re
from typing import Optional

from .logging import write_to_log

TAG_KEY_REGEX = re.compile(r"^[a-zA-Z0-9$_.:\- ]+$")
MAX_TAG_KEY_LENGTH = 32
MAX_TAG_VALUE_LENGTH = 200
MAX_TAG_ENTRIES = 50


def validate_tags(tags: dict) -> Optional[dict]:
    """Validate and filter a tags dict against AgentCat tag constraints.

    Invalid entries are dropped with a warning logged via write_to_log.
    Returns None if no valid entries remain. Never raises — callers include
    the customer's request path, and keys/values are arbitrary user objects
    (possibly with broken __str__/__format__).
    """
    if not tags:
        return None

    if not isinstance(tags, dict):
        write_to_log(
            f"Dropping tags — expected a dict, got {type(tags).__name__}"
        )
        return None

    valid: list[tuple[str, str]] = []

    for key, value in tags.items():
        try:
            valid_entry = _validate_tag_entry(key, value)
        except Exception:
            # e.g. a key/value whose __str__/__format__ raises during logging
            continue
        if valid_entry:
            valid.append((key, value))

    if not valid:
        return None

    if len(valid) > MAX_TAG_ENTRIES:
        dropped = len(valid) - MAX_TAG_ENTRIES
        write_to_log(
            f"Dropping {dropped} tag(s) — exceeds maximum of {MAX_TAG_ENTRIES} entries per event"
        )
        valid = valid[:MAX_TAG_ENTRIES]

    return dict(valid)


def _validate_tag_entry(key, value) -> bool:
    """Validate a single tag entry; returns True if it should be kept.

    May raise if key/value have a broken __str__/__format__ (log formatting);
    validate_tags catches that per-entry and drops the tag.
    """
    if not isinstance(key, str) or not key or not TAG_KEY_REGEX.match(key):
        write_to_log(
            f'Dropping invalid tag: "{key}" — key contains invalid characters or is empty'
        )
        return False

    if len(key) > MAX_TAG_KEY_LENGTH:
        write_to_log(
            f'Dropping invalid tag: "{key}" — key exceeds max length of {MAX_TAG_KEY_LENGTH}'
        )
        return False

    if not isinstance(value, str):
        write_to_log(
            f'Dropping invalid tag: "{key}" — non-string value (got {type(value).__name__})'
        )
        return False

    if len(value) > MAX_TAG_VALUE_LENGTH:
        write_to_log(
            f'Dropping invalid tag: "{key}" — value exceeds max length of {MAX_TAG_VALUE_LENGTH}'
        )
        return False

    if "\n" in value:
        write_to_log(
            f'Dropping invalid tag: "{key}" — value contains newline character'
        )
        return False

    return True
