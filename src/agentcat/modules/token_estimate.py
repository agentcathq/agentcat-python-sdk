"""Token estimates for tool-call events.

One divisor, pinned byte-for-byte across the TypeScript, Python and Go SDKs:
``ceil(utf8_bytes / 3.5)``. Measured on production MCP responses, the inner
text runs 3.71 bytes per token on OpenAI tokenizers and about 3.15 on
Claude's; 3.5 splits the difference. The input side counts the raw arguments
the model emitted; the output side counts the content-block text the harness
feeds back. Nothing else counts. See the TypeScript repo's
docs/superpowers/specs/2026-09-19-sdk-token-estimates-design.md.
"""

from __future__ import annotations

import json
import math
from typing import Any

TOKEN_ESTIMATE_BYTES_PER_TOKEN = 3.5

# The server's MAX_TOKEN_COUNT: the columns are int32.
_MAX_TOKEN_COUNT = 2_147_483_647


def estimate_tokens(byte_count: int) -> int:
    """``ceil(bytes / 3.5)``, 0 for nothing, clamped to the server's column."""
    if byte_count <= 0:
        return 0
    return min(
        math.ceil(byte_count / TOKEN_ESTIMATE_BYTES_PER_TOKEN), _MAX_TOKEN_COUNT
    )


def _utf8_len(text: str) -> int:
    # surrogatepass: a lone surrogate off the wire (json.loads on a string
    # containing an unpaired \udXXX escape) must still be counted, not
    # dropped. TypeScript and Go's decoders substitute U+FFFD (3 bytes) for
    # the same input; surrogatepass encodes a lone surrogate to the same 3
    # bytes, so the byte count matches across SDKs.
    return len(text.encode("utf-8", errors="surrogatepass"))


def _compact_json_bytes(value: Any) -> int | None:
    """UTF-8 length of the compact JSON: no spaces, no ASCII escaping.

    ``ensure_ascii=False`` matters: the default would spell ``café`` as
    ``caf\\u00e9`` and count 20 bytes where TypeScript and Go count 16. No
    ``default=str``: an unserializable value must be omitted, matching
    TypeScript and Go, which also drop the field rather than counting a
    stand-in string.
    """
    try:
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return None
    return _utf8_len(text)


def estimate_input_tokens(arguments: Any) -> int | None:
    """Tokens the model spent emitting the call: the raw arguments, injected
    parameters included. None when there are no arguments to count.

    Never raises: this runs inside the customer's request, and a failure to
    estimate must cost at most this field, never the tool's response.
    """
    try:
        if arguments is None:
            return None
        byte_count = _compact_json_bytes(arguments)
        return None if byte_count is None else estimate_tokens(byte_count)
    except Exception:
        return None


def estimate_output_tokens(response: Any) -> int | None:
    """Tokens the model reads back: the text of the content blocks.

    Falls back to the whole response when it carries no content list; None
    when there is no response at all. Never raises, for the same reason as
    ``estimate_input_tokens``.
    """
    try:
        if response is None:
            return None
        content = response.get("content") if isinstance(response, dict) else None
        if not isinstance(content, list):
            byte_count = _compact_json_bytes(response)
            return None if byte_count is None else estimate_tokens(byte_count)
        return estimate_tokens(
            sum(_content_block_bytes(block) for block in content)
        )
    except Exception:
        return None


def _content_block_bytes(block: Any) -> int:
    if not isinstance(block, dict):
        return 0
    if block.get("type") == "text" and isinstance(block.get("text"), str):
        return _utf8_len(block["text"])
    if block.get("type") == "resource":
        resource = block.get("resource")
        if isinstance(resource, dict) and isinstance(resource.get("text"), str):
            return _utf8_len(resource["text"])
    return 0
