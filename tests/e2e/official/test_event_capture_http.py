"""Event-capture and round-trip tests over real Streamable HTTP.

v2 publishes exactly one event type — mcp:tools/call. initialize is handled by
ServerSession before any user handler fires, and tools/list is intercepted for
schema injection only, so neither produces an event.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_handshake_and_list_publish_nothing(official_http_server, capture_queue):
    """A real handshake plus list_tools produces no events at all."""
    url, _server = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            listed = await client.list_tools()

    time.sleep(0.5)
    # The listing still went through AgentCat: the handles are on the schemas.
    add = next(t for t in listed.tools if t.name == "add_todo")
    assert "session_id" in add.inputSchema["properties"]
    assert capture_queue == [], [e.event_type for e in capture_queue]


@pytest.mark.asyncio
async def test_tools_call_event_captured(official_http_server, capture_queue):
    url, _server = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "hi", "context": "e2e smoke"}
            )

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert call_events, "expected a mcp:tools/call event"
    assert call_events[0].resource_name == "add_todo"


@pytest.mark.asyncio
async def test_event_duration_is_non_negative(official_http_server, capture_queue):
    """Tool round-trip records a non-negative duration."""
    url, _server = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "duration test", "context": "duration"}
            )

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert call_events, "expected a mcp:tools/call event"
    assert call_events[0].duration is not None
    assert call_events[0].duration >= 0


@pytest.mark.asyncio
async def test_concurrent_clients_get_distinct_session_ids(
    official_http_server, capture_queue
):
    """Two stateful clients connecting concurrently should produce events with
    distinct mcp-session-id values in extra.sessionId."""
    url, _server = official_http_server

    async def call_once(text: str) -> None:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool(
                    "add_todo", {"text": text, "context": "concurrent"}
                )

    await asyncio.gather(call_once("a"), call_once("b"))
    time.sleep(0.7)

    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert len(call_events) >= 2, f"expected >=2 call events, got {len(call_events)}"
    session_ids = {
        (e.parameters or {}).get("extra", {}).get("sessionId") for e in call_events
    }
    # Each connection gets its own MCP session id.
    assert len(session_ids - {None}) >= 2, (
        f"expected distinct sessionIds across concurrent clients, got {session_ids}"
    )
