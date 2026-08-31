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
        assert (
            "[session_id issued — see this tool's session_id parameter description]"
            in text
        )  # noqa: E501
        minted = text.split("session_id: ")[1].split("\n")[0]

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
    """Name AND version reach the tools/call event over a real connection.

    The handshake capture is the last rung of the identity ladder and the only
    one this era can use. Asserted against a `client_info` this test supplies
    rather than against "some name resolved": the SDK's own default satisfies a
    truthiness check while proving nothing about what the ladder read, and
    `client_version` is null-by-default, so a rung that dropped it would go
    unnoticed by a name-only assertion.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.types import Implementation

    url, _ = v3_http_server
    async with Client(
        StreamableHttpTransport(url),
        client_info=Implementation(name="MyAgent", version="1.2.3"),
    ) as client:
        await client.call_tool("add_todo", {"text": "who", "context": "x"})

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert call_events
    event = call_events[-1]
    assert (event.client_name, event.client_version) == ("MyAgent", "1.2.3")


@pytest.mark.asyncio
async def test_identity_rides_every_call_not_just_the_first(
    v3_http_server, capture_queue
):
    """Name AND version on EVERY event of a connection.

    Reading only the last event cannot tell "resolved per request" from
    "resolved once and reused", and the two differ exactly where it matters: a
    rung that answers only for the call following the handshake leaves every
    later event of a long-lived connection anonymous. This is a STATEFUL app,
    so the connection whose handshake was captured is still there for all three
    calls — the rung has to answer more than once.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.types import Implementation

    url, _ = v3_http_server
    async with Client(
        StreamableHttpTransport(url),
        client_info=Implementation(name="Cursor", version="2.6.22"),
    ) as client:
        for n in range(3):
            await client.call_tool("add_todo", {"text": f"call-{n}", "context": "id"})

    time.sleep(0.5)
    events = [e for e in capture_queue if e.event_type == "mcp:tools/call"][-3:]
    assert [e.parameters["arguments"]["text"] for e in events] == [
        "call-0",
        "call-1",
        "call-2",
    ]
    assert [(e.client_name, e.client_version) for e in events] == [
        ("Cursor", "2.6.22")
    ] * 3
