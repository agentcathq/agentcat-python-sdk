"""Task-handle and client-info propagation over real Streamable HTTP.

Two things ride every tools/call event and both are resolved per request:
the task handle (minted on the first call, echoed back by the agent on later
ones) and the client identity. The identity ladder reads the per-request
`_meta` keys first and falls back to the handshake clientInfo the SDK's own
ServerSession captured — no header parsing, no caching of our own.
"""

from __future__ import annotations

import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

pytestmark = pytest.mark.e2e


def _last_event(capture_queue, event_type: str):
    return [e for e in capture_queue if e.event_type == event_type][-1]


@pytest.mark.asyncio
async def test_custom_clientinfo_propagates_to_event(
    official_http_server, capture_queue
):
    """ClientSession.client_info=Implementation(name=..., version=...)
    surfaces on captured events as client_name / client_version."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(
            read,
            write,
            client_info=Implementation(name="MyAgent", version="1.2.3"),
        ) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "agent", "context": "id"}
            )

    time.sleep(0.5)
    ev = _last_event(capture_queue, "mcp:tools/call")
    assert ev.client_name == "MyAgent", f"expected MyAgent, got {ev.client_name}"
    assert ev.client_version == "1.2.3"


@pytest.mark.asyncio
async def test_default_clientinfo_used_when_unspecified(
    official_http_server, capture_queue
):
    """When the client doesn't pass client_info, the SDK's default
    Implementation(name='mcp', version='...') is used and propagates."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "default", "context": "id"}
            )

    time.sleep(0.5)
    ev = _last_event(capture_queue, "mcp:tools/call")
    # SDK default is "mcp" — assert it propagated. Don't pin the version,
    # which depends on installed SDK version.
    assert ev.client_name is not None, "expected non-None client_name"


@pytest.mark.asyncio
async def test_clientinfo_with_special_characters(
    official_http_server, capture_queue
):
    """Edge case: clientInfo with special characters round-trips intact."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(
            read,
            write,
            client_info=Implementation(
                name="My Test Agent v2",
                version="0.1.0-beta+build123",
            ),
        ) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "edge", "context": "edge"}
            )

    time.sleep(0.5)
    ev = _last_event(capture_queue, "mcp:tools/call")
    assert ev.client_name == "My Test Agent v2"
    assert ev.client_version == "0.1.0-beta+build123"


@pytest.mark.asyncio
async def test_clientinfo_in_extra_headers_when_set(
    official_http_server, capture_queue
):
    """X-MCP-Client-* headers ride along in extra.requestInfo.headers
    even though they don't drive client_name (clientInfo wins). This proves
    customers can still inspect the headers via the captured extra."""
    url, _ = official_http_server
    async with streamablehttp_client(
        url,
        headers={
            "X-MCP-Client-Name": "HeaderClient",
            "X-MCP-Client-Version": "8.8.8",
        },
    ) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "hdr", "context": "hdr"}
            )

    time.sleep(0.5)
    ev = _last_event(capture_queue, "mcp:tools/call")
    headers = (
        (ev.parameters or {})
        .get("extra", {})
        .get("requestInfo", {})
        .get("headers", {})
    )
    assert headers.get("x-mcp-client-name") == "HeaderClient"
    assert headers.get("x-mcp-client-version") == "8.8.8"


def _call_events(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"]


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


@pytest.mark.asyncio
async def test_minted_session_id_is_echoed_across_http_calls(
    official_http_server, capture_queue
):
    """First call mints and hands the handle back; the agent echoes it on the
    next call and both events land on the same task."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            first = await client.call_tool(
                "add_todo", {"text": "one", "context": "first call of the task"}
            )
            text = _text(first)
            assert "[MCP INSTRUCTIONS]: session_id issued." in text
            minted = text.split("session_id=")[1].split(" ")[0]
            assert minted.startswith("ses_")

            second = await client.call_tool(
                "add_todo", {"text": "two", "session_id": minted}
            )
            # Already supplied: nothing is minted back a second time.
            assert "[MCP INSTRUCTIONS]: session_id issued." not in _text(second)

    time.sleep(0.5)
    events = _call_events(capture_queue)
    assert len(events) == 2
    assert [e.session_id for e in events] == [minted, minted]
    assert [e.tags["agentcat_session_id_source"] for e in events] == [
        "minted",
        "supplied",
    ]


@pytest.mark.asyncio
async def test_separate_connections_get_separate_tasks(
    official_http_server, capture_queue
):
    """Nothing is stored server-side, so two agents that never echo a handle
    get two different tasks."""
    url, _ = official_http_server

    async def call_once(text: str) -> str:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.call_tool(
                    "add_todo", {"text": text, "context": "independent task"}
                )
                return _text(result).split("session_id=")[1].split(" ")[0]

    first = await call_once("a")
    second = await call_once("b")
    assert first != second

    time.sleep(0.5)
    minted = {e.session_id for e in _call_events(capture_queue)}
    assert {first, second} <= minted
