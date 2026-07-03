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
    Returns None if no valid entries remain.
    """
    if not tags:
        return None

    valid: list[tuple[str, str]] = []

    for key, value in tags.items():
        if not isinstance(key, str) or not key or not TAG_KEY_REGEX.match(key):
            write_to_log(
                f'Dropping invalid tag: "{key}" — key contains invalid characters or is empty'
            )
            continue

        if len(key) > MAX_TAG_KEY_LENGTH:
            write_to_log(
                f'Dropping invalid tag: "{key}" — key exceeds max length of {MAX_TAG_KEY_LENGTH}'
            )
            continue

        if not isinstance(value, str):
            write_to_log(
                f'Dropping invalid tag: "{key}" — non-string value (got {type(value).__name__})'
            )
            continue

        if len(value) > MAX_TAG_VALUE_LENGTH:
            write_to_log(
                f'Dropping invalid tag: "{key}" — value exceeds max length of {MAX_TAG_VALUE_LENGTH}'
            )
            continue

        if "\n" in value:
            write_to_log(
                f'Dropping invalid tag: "{key}" — value contains newline character'
            )
            continue

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
