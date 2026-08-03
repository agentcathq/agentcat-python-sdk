"""parameters.extra.requestInfo.headers parity tests over real Streamable HTTP."""

from __future__ import annotations

import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


pytestmark = pytest.mark.e2e


def _last_call_event(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"][-1]


def _extra(event):
    return (event.parameters or {}).get("extra", {})


@pytest.mark.asyncio
async def test_custom_header_lands_in_request_info(
    official_http_server, capture_queue
):
    url, _ = official_http_server
    async with streamablehttp_client(
        url, headers={"X-Demo-Header": "demo-value"}
    ) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "h", "context": "header test"}
            )

    time.sleep(0.5)
    headers = _extra(_last_call_event(capture_queue)).get("requestInfo", {}).get(
        "headers", {}
    )
    assert headers.get("x-demo-header") == "demo-value", (
        f"expected x-demo-header in extra.requestInfo.headers, got {headers}"
    )


@pytest.mark.asyncio
async def test_user_agent_preserved(official_http_server, capture_queue):
    url, _ = official_http_server
    async with streamablehttp_client(
        url, headers={"User-Agent": "Cursor/2.6.22"}
    ) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "ua", "context": "ua"}
            )

    time.sleep(0.5)
    headers = _extra(_last_call_event(capture_queue)).get("requestInfo", {}).get(
        "headers", {}
    )
    assert headers.get("user-agent") == "Cursor/2.6.22"


@pytest.mark.asyncio
async def test_mcp_session_id_header_promoted_to_extra_session_id(
    official_http_server, capture_queue
):
    """The SDK issues mcp-session-id during initialize; subsequent requests carry
    it back, so extra.sessionId on tool/call events must equal that header."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, get_sid):
        async with ClientSession(read, write) as client:
            await client.initialize()
            sdk_session_id = get_sid()
            await client.call_tool(
                "add_todo", {"text": "sid", "context": "session id"}
            )

    time.sleep(0.5)
    extra = _extra(_last_call_event(capture_queue))
    assert sdk_session_id, "MCP SDK should have issued an mcp-session-id"
    assert extra.get("sessionId") == sdk_session_id, (
        f"extra.sessionId={extra.get('sessionId')} should match SDK session id "
        f"{sdk_session_id}"
    )


@pytest.mark.asyncio
async def test_request_id_present_per_call(official_http_server, capture_queue):
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "r1", "context": "req"}
            )
            await client.call_tool(
                "add_todo", {"text": "r2", "context": "req"}
            )

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert len(call_events) >= 2
    request_ids = [_extra(e).get("requestId") for e in call_events]
    assert all(rid is not None for rid in request_ids), request_ids
    # JSON-RPC ids should be unique per request on a single session.
    assert len(set(request_ids)) == len(request_ids), (
        f"expected distinct requestIds, got {request_ids}"
    )


@pytest.mark.asyncio
async def test_meta_dict_present_when_supported(official_http_server, capture_queue):
    """If the SDK passes _meta on the request (progressToken, client_id), it
    surfaces in extra.meta. If the SDK version does not surface it, extra.meta
    is simply absent — both are acceptable."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "meta", "context": "meta"}
            )

    time.sleep(0.5)
    extra = _extra(_last_call_event(capture_queue))
    meta = extra.get("meta")
    if meta is not None:
        assert isinstance(meta, dict), f"extra.meta should be dict, got {type(meta)}"


@pytest.mark.asyncio
async def test_extra_survives_a_listing_in_the_same_session(
    official_http_server, capture_queue
):
    """tools/list publishes no event in v2, so `extra` rides the tools/call
    that follows it — with the headers of THAT request, not the listing's."""
    url, _ = official_http_server
    async with streamablehttp_client(
        url, headers={"X-List-Header": "list-value"}
    ) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.list_tools()
            await client.call_tool("add_todo", {"text": "l", "context": "listing"})

    time.sleep(0.5)
    assert {e.event_type for e in capture_queue} == {"mcp:tools/call"}
    headers = _extra(_last_call_event(capture_queue)).get("requestInfo", {}).get(
        "headers", {}
    )
    assert headers.get("x-list-header") == "list-value"
