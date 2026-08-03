"""Community FastMCP 4 over real Streamable HTTP.

The in-process tests cover the adapter's logic; this file covers what only a
socket can — the SDK's outbound validation of the injected schema, the wire
mint-back, and the protocol error a malformed request must keep. v2 publishes
exactly one event type: `mcp:tools/call`. `initialize` only feeds the
client-identity ladder and `tools/list` is intercepted for schema injection, so
neither produces an event.
"""

from __future__ import annotations

import json
import time

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from agentcat.modules.constants import (
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_INSTRUCTIONS_KEY,
    SESSION_ID_PARAM,
)

pytestmark = pytest.mark.e2e

MINT_BACK_HEADER = "[MCP INSTRUCTIONS]: session_id issued."


def _call_events(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"]


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


async def test_handshake_and_list_publish_nothing(v4_http_server, capture_queue):
    """A real handshake plus list_tools produces no events at all — and the
    injected schema survives the SDK's outbound validation."""
    url, _ = v4_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        listed = await client.list_tools()

    time.sleep(0.5)
    add = next(t for t in listed if t.name == "add_todo")
    assert list(add.input_schema["properties"])[-2:] == [SESSION_ID_PARAM, "context"]
    assert MCP_INSTRUCTIONS_KEY in add.output_schema["properties"]
    assert capture_queue == [], [e.event_type for e in capture_queue]


async def test_task_handle_is_minted_then_echoed(v4_http_server, capture_queue):
    """The mint-back travels over the wire — text and structured — and the
    echoed handle keys the next event to the same task."""
    url, _ = v4_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        first = await client.call_tool(
            "add_todo", {"text": "one", "context": "first call of the task"}
        )
        text = _text(first)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id=")[1].split(" ")[0]
        assert minted.startswith("ses_")
        assert first.structured_content[MCP_INSTRUCTIONS_KEY]["session_id"] == minted

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
    """The handshake clientInfo is the last rung of the identity ladder, and it
    reaches the tools/call event over a real connection."""
    url, _ = v4_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        await client.call_tool("add_todo", {"text": "who", "context": "x"})

    time.sleep(0.5)
    assert _call_events(capture_queue)[-1].client_name


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
