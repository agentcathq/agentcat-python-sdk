"""Content sanitization for AgentCat events.

Strips binary/non-text content (images, audio, base64 blobs) from event
payloads before they are sent to the AgentCat API or telemetry exporters.
"""

import copy
import re
from datetime import date, datetime
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcpcat.types import UnredactedEvent

# Threshold for base64 scanning — strings shorter than this are passed through.
_BASE64_SIZE_THRESHOLD = 10_240

# Heuristic: may match non-base64 strings composed entirely of alphanumeric
# characters, but the 10 KB size gate makes false positives unlikely in practice.
_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/\n\r]+=*$")

# Redaction messages
_IMAGE_REDACTED = "[image content redacted - not supported by agentcat]"
_AUDIO_REDACTED = "[audio content redacted - not supported by agentcat]"
_BLOB_RESOURCE_REDACTED = "[binary resource content redacted - not supported by agentcat]"
_BINARY_DATA_REDACTED = "[binary data redacted - not supported by agentcat]"


def _unsupported_type_redacted(type_name: str) -> str:
    return f'[unsupported content type "{type_name}" redacted - not supported by agentcat]'


# ---------------------------------------------------------------------------
# Layer 2 — recursive base64 scanner
# ---------------------------------------------------------------------------

def _scan_for_base64(value: Any) -> Any:
    """Recursively walk a value and replace large base64 strings."""
    if value is None:
        return value

    if isinstance(value, (datetime, date)):
        return value

    if isinstance(value, str):
        if len(value) >= _BASE64_SIZE_THRESHOLD and _BASE64_PATTERN.match(value):
            return _BINARY_DATA_REDACTED
        return value

    if isinstance(value, list):
        return [_scan_for_base64(item) for item in value]

    if isinstance(value, dict):
        return {k: _scan_for_base64(v) for k, v in value.items()}

    # numbers, booleans, etc.
    return value


# ---------------------------------------------------------------------------
# Layer 1 — response content sanitization
# ---------------------------------------------------------------------------

def _sanitize_content_block(block: Any) -> Any:
    """Sanitize a single content block from ``response["content"]``."""
    if not isinstance(block, dict):
        return block

    block_type = block.get("type")

    if block_type in ("text", "resource_link"):
        return block

    if block_type == "image":
        return {"type": "text", "text": _IMAGE_REDACTED}

    if block_type == "audio":
        return {"type": "text", "text": _AUDIO_REDACTED}

    if block_type == "resource":
        resource = block.get("resource")
        if isinstance(resource, dict) and "blob" in resource:
            return {"type": "text", "text": _BLOB_RESOURCE_REDACTED}
        # text-only resource — pass through
        return block

    # Unknown / unsupported type
    return {"type": "text", "text": _unsupported_type_redacted(str(block_type))}


def _sanitize_response(response: dict[str, Any]) -> dict[str, Any]:
    """Sanitize non-text content blocks in an event response.

    Mutates *response* in place. Caller must pass a copy (e.g. via
    ``copy.deepcopy``) if the original must be preserved.
    """
    if "content" in response and isinstance(response["content"], list):
        response["content"] = [
            _sanitize_content_block(block) for block in response["content"]
        ]

    for key, value in response.items():
        if key != "content":
            response[key] = _scan_for_base64(value)

    return response


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def sanitize_event(event: Optional["UnredactedEvent"]) -> Optional["UnredactedEvent"]:
    """Return a sanitized copy of *event* with binary content stripped.

    - Replaces image/audio/blob content blocks in ``event.response``
    - Scans ``event.parameters`` and ``response.structured_content`` for
      large base64 strings and replaces them
    - Never mutates the original event (uses ``copy.deepcopy``)
    - Gracefully handles ``None``
    """
    if event is None:
        return None

    event = copy.deepcopy(event)

    if event.response is not None:
        _sanitize_response(event.response)

    if event.parameters is not None:
        event.parameters = _scan_for_base64(event.parameters)

    return event
