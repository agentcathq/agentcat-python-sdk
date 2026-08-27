"""Community FastMCP 4 over real Streamable HTTP.

The in-process tests cover the adapter's logic; this file covers what only a
socket can — the SDK's outbound validation of the injected schema, the wire
mint-back, and the protocol error a malformed request must keep. v2 publishes
exactly one event type: `mcp:tools/call`. `initialize` only feeds the
client-identity ladder and `tools/list` is intercepted for schema injection, so
neither produces an event.

Every `fastmcp` / `httpx2` import is inside a test body, as in the rest of this
tree. `tests/conftest.py` keeps the community trees out of a run with no
fastmcp installed, but a module-scope import here would fail at COLLECTION,
which no conftest gate downstream of it can rescue.
"""

from __future__ import annotations

import json
import time

import pytest

from agentcat.modules.constants import (
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_SESSION_KEY,
    SESSION_ID_PARAM,
)

pytestmark = pytest.mark.e2e

MINT_BACK_HEADER = (
    "[session_id issued — see this tool's session_id parameter description]"  # noqa: E501
)


def _call_events(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"]


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


async def test_handshake_and_list_publish_nothing(v4_http_server, capture_queue):
    """A real handshake plus list_tools produces no events at all — and the
    injected schema survives the SDK's outbound validation."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v4_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        listed = await client.list_tools()

    time.sleep(0.5)
    add = next(t for t in listed if t.name == "add_todo")
    assert list(add.input_schema["properties"])[-2:] == [SESSION_ID_PARAM, "context"]
    assert MCP_SESSION_KEY in add.output_schema["properties"]
    assert capture_queue == [], [e.event_type for e in capture_queue]


async def test_task_handle_is_minted_then_echoed(v4_http_server, capture_queue):
    """The mint-back travels over the wire — text and structured — and the
    echoed handle keys the next event to the same task."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v4_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        first = await client.call_tool(
            "add_todo",
            {
                "text": "one",
                SESSION_ID_PARAM: "start",
                "context": "first call of the task",
            },
        )
        text = _text(first)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id: ")[1].split("\n")[0]
        assert minted.startswith("ses_")
        assert first.structured_content[MCP_SESSION_KEY]["session_id"] == minted

        second = await client.call_tool(
            "add_todo", {"text": "two", SESSION_ID_PARAM: minted}
        )
        assert MINT_BACK_HEADER not in _text(second)

    time.sleep(0.5)
    events = _call_events(capture_queue)
    assert [e.session_id for e in events] == [minted, minted]
    sources = [e.tags[AGENTCAT_TAG_SESSION_SOURCE] for e in events]
    assert sources == ["minted", "supplied"]
    # The event records the call as the agent made it: raw arguments in, the
    # customer's undecorated result out.
    assert events[0].parameters["arguments"]["context"] == "first call of the task"
    assert events[0].user_intent == "first call of the task"
    assert MINT_BACK_HEADER not in json.dumps(events[0].response)
    assert events[0].duration is not None and events[0].duration >= 0


async def test_client_identity_reaches_the_event(v4_http_server, capture_queue):
    """Name AND version reach the event over a real connection.

    Asserted against a `client_info` this test supplies rather than against
    "some name resolved": the SDK's own default satisfies a truthiness check
    while proving nothing about what the ladder actually read, and
    `client_version` is null-by-default, so a rung that dropped it would go
    unnoticed by a name-only assertion.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.types import Implementation

    url, _ = v4_http_server
    async with Client(
        StreamableHttpTransport(url),
        client_info=Implementation(name="MyAgent", version="1.2.3"),
    ) as client:
        await client.call_tool("add_todo", {"text": "who", "context": "x"})

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert (event.client_name, event.client_version) == ("MyAgent", "1.2.3")


async def test_identity_rides_every_call_not_just_the_first(
    v4_http_server, capture_queue
):
    """Name AND version on EVERY event of a connection.

    Reading only the last event cannot tell "resolved per request" from
    "resolved once and reused", and the two differ exactly where it matters: a
    rung that answers only for the call following the handshake leaves every
    later event of a long-lived connection anonymous. Three calls, three
    identities.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.types import Implementation

    url, _ = v4_http_server
    async with Client(
        StreamableHttpTransport(url),
        client_info=Implementation(name="Cursor", version="2.6.22"),
    ) as client:
        for n in range(3):
            await client.call_tool("add_todo", {"text": f"call-{n}", "context": "id"})

    time.sleep(0.5)
    events = _call_events(capture_queue)[-3:]
    assert [e.parameters["arguments"]["text"] for e in events] == [
        "call-0",
        "call-1",
        "call-2",
    ]
    assert [(e.client_name, e.client_version) for e in events] == [
        ("Cursor", "2.6.22")
    ] * 3


async def test_a_malformed_tools_call_keeps_its_own_protocol_error(
    v4_http_server, capture_queue
):
    """FastMCP 4 runs the middleware chain a SECOND time for a component
    request that failed before the interior chain did, and hands the hooks the
    RAW params mapping rather than a typed model.

    A `tools/call` with no `name` is exactly that request. AgentCat must let it
    past untouched: an `AttributeError` raised in the hook would REPLACE the
    customer server's own `-32602` on the wire, and no real client can be made
    to send this, so only a hand-built request reaches it.
    """
    import httpx2

    url, _ = v4_http_server
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    async with httpx2.AsyncClient(follow_redirects=True) as http:
        handshake = await http.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "malformed-probe", "version": "1"},
                },
            },
        )
        session_id = handshake.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        await http.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        response = await http.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"arguments": {"text": "no name at all"}},
            },
        )

    body = response.text
    payload = json.loads(body.split("data: ", 1)[1])
    assert payload["error"]["code"] == -32602, body
    assert "model_copy" not in payload["error"]["message"], body

    time.sleep(0.5)
    assert _call_events(capture_queue) == []
