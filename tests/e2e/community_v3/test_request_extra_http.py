"""parameters.extra.requestInfo.headers parity for community FastMCP over HTTP."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.e2e


def _last_call(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"][-1]


def _extra(event):
    return (event.parameters or {}).get("extra", {})


@pytest.mark.asyncio
async def test_custom_header_lands_in_extra(v3_http_server, capture_queue):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(
        StreamableHttpTransport(url, headers={"X-V3-Header": "v3-value"})
    ) as client:
        await client.call_tool("add_todo", {"text": "v3-h", "context": "v3-h"})

    time.sleep(0.5)
    headers = _extra(_last_call(capture_queue)).get("requestInfo", {}).get(
        "headers", {}
    )
    assert headers.get("x-v3-header") == "v3-value", (
        f"expected x-v3-header in extra.requestInfo.headers, got {headers}"
    )


@pytest.mark.asyncio
async def test_extra_sits_beside_the_raw_arguments(v3_http_server, capture_queue):
    """v2 builds parameters as {"arguments": raw, "extra": {...}} rather than
    dumping the whole request."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(
        StreamableHttpTransport(url, headers={"X-V3-Shape": "shape"})
    ) as client:
        await client.call_tool("add_todo", {"text": "shape", "context": "why"})

    time.sleep(0.5)
    event = _last_call(capture_queue)
    assert set(event.parameters) == {"arguments", "extra"}
    assert event.parameters["arguments"] == {"text": "shape", "context": "why"}
    assert _extra(event)["requestInfo"]["headers"]["x-v3-shape"] == "shape"


@pytest.mark.asyncio
async def test_session_id_and_meta_shapes(v3_http_server, capture_queue):
    """Sanity: extra.sessionId is a string when present, extra.meta a dict."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        await client.call_tool("add_todo", {"text": "v3-meta", "context": "meta"})

    time.sleep(0.5)
    extra = _extra(_last_call(capture_queue))
    if extra.get("sessionId") is not None:
        assert isinstance(extra["sessionId"], str)
    if extra.get("meta") is not None:
        assert isinstance(extra["meta"], dict)
