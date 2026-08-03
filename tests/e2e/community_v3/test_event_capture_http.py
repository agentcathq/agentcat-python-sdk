"""Community FastMCP event-capture tests over real Streamable HTTP.

v2 publishes exactly one event type — mcp:tools/call. initialize only feeds the
client-identity ladder, and tools/list is intercepted for schema injection only,
so neither produces an event.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_handshake_and_list_publish_nothing(v3_http_server, capture_queue):
    """A real handshake plus list_tools produces no events at all."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        listed = await client.list_tools()

    time.sleep(0.5)
    # The listing still went through AgentCat: the handles are on the schemas.
    add = next(t for t in listed if t.name == "add_todo")
    assert "session_id" in add.inputSchema["properties"]
    assert capture_queue == [], [e.event_type for e in capture_queue]


@pytest.mark.asyncio
async def test_call_tool_via_v3(v3_http_server, capture_queue):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        await client.call_tool("add_todo", {"text": "v3-call", "context": "x"})

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert call_events
    assert call_events[0].resource_name == "add_todo"
    assert call_events[0].user_intent == "x"
    # Raw arguments on the event; the tool received the stripped copy.
    assert call_events[0].parameters["arguments"]["context"] == "x"


@pytest.mark.asyncio
async def test_task_handle_is_minted_then_echoed(v3_http_server, capture_queue):
    """The mint-back travels over the wire, and the echoed handle keys the
    second event to the same task."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        first = await client.call_tool("add_todo", {"text": "one"})
        text = "".join(c.text for c in first.content if hasattr(c, "text"))
        assert "[MCP INSTRUCTIONS]: session_id issued." in text
        minted = text.split("session_id=")[1].split(" ")[0]

        await client.call_tool("add_todo", {"text": "two", "session_id": minted})

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert [e.session_id for e in call_events] == [minted, minted]


@pytest.mark.asyncio
async def test_v3_event_duration_is_non_negative(v3_http_server, capture_queue):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        await client.call_tool("add_todo", {"text": "duration", "context": "x"})

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert call_events
    assert call_events[0].duration is not None
    assert call_events[0].duration >= 0


@pytest.mark.asyncio
async def test_client_identity_reaches_the_event(v3_http_server, capture_queue):
    """The handshake clientInfo is the last rung of the identity ladder, and it
    reaches the tools/call event over a real connection."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        await client.call_tool("add_todo", {"text": "who", "context": "x"})

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert call_events
    assert call_events[-1].client_name, "no client name resolved for the call"
