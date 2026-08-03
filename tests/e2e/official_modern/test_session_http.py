"""Task handles, client identity and per-request extra over real Streamable HTTP.

The in-process tests cover the adapter's logic; this file covers what only a
socket can: the HTTP request object the transport hands the handler
(`ctx.request`), the headers riding on it, and the transport's own session id.
"""

from __future__ import annotations

import time

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from agentcat.modules.constants import (
    AGENTCAT_TAG_PROTOCOL_VERSION,
    AGENTCAT_TAG_SESSION_SOURCE,
)

pytestmark = pytest.mark.e2e

MINT_BACK_HEADER = "[MCP INSTRUCTIONS]: session_id issued."


def _call_events(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"]


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


@pytest.mark.asyncio
async def test_minted_session_id_is_echoed_across_http_calls(
    modern_http_server, capture_queue
):
    """First call mints and hands the handle back; the agent echoes it on the
    next call and both events land on the same task."""
    url, _ = modern_http_server
    async with Client(url) as client:
        first = await client.call_tool(
            "add_todo", {"text": "one", "context": "first call of the task"}
        )
        text = _text(first)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id=")[1].split(" ")[0]
        assert minted.startswith("ses_")

        second = await client.call_tool(
            "add_todo", {"text": "two", "session_id": minted}
        )
        # Already supplied: nothing is minted back a second time.
        assert MINT_BACK_HEADER not in _text(second)

    time.sleep(0.5)
    events = _call_events(capture_queue)
    assert len(events) == 2
    assert [e.session_id for e in events] == [minted, minted]
    assert [e.tags[AGENTCAT_TAG_SESSION_SOURCE] for e in events] == [
        "minted",
        "supplied",
    ]
    assert all(e.tags[AGENTCAT_TAG_PROTOCOL_VERSION] for e in events)


@pytest.mark.asyncio
async def test_separate_connections_get_separate_tasks(
    modern_http_server, capture_queue
):
    """Nothing is stored server-side, so two agents that never echo a handle
    get two different tasks."""
    url, _ = modern_http_server

    async def call_once(text: str) -> str:
        async with Client(url) as client:
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


@pytest.mark.asyncio
async def test_custom_clientinfo_propagates_to_event(
    modern_http_server, capture_queue
):
    """`client_info` reaches the event as client_name / client_version, whether
    it rides the handshake or the per-request `_meta` envelope."""
    url, _ = modern_http_server
    async with Client(
        url, client_info=Implementation(name="MyAgent", version="1.2.3")
    ) as client:
        await client.call_tool("add_todo", {"text": "agent", "context": "id"})

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert (event.client_name, event.client_version) == ("MyAgent", "1.2.3")


@pytest.mark.asyncio
async def test_request_headers_ride_the_event(modern_http_server, capture_queue):
    """`parameters.extra.requestInfo.headers` is only reachable from the HTTP
    request the transport attaches to the context — the in-process client has
    none, so this is the only place it can be asserted."""
    url, _ = modern_http_server
    http_client = httpx2.AsyncClient(
        headers={"X-MCP-Client-Name": "HeaderClient", "X-Tenant": "acme"}
    )
    async with Client(streamable_http_client(url, http_client=http_client)) as client:
        await client.call_tool("add_todo", {"text": "hdr", "context": "hdr"})

    time.sleep(0.5)
    extra = (_call_events(capture_queue)[-1].parameters or {}).get("extra", {})
    headers = extra.get("requestInfo", {}).get("headers", {})
    assert headers.get("x-mcp-client-name") == "HeaderClient"
    assert headers.get("x-tenant") == "acme"


@pytest.mark.asyncio
async def test_session_id_is_reported_only_when_the_transport_issues_one(
    modern_http_server, capture_queue
):
    """The 2026-07-28 wire dropped the handshake, so a connection on it has no
    session at all and the event must not invent one. The handshake era still
    issues `Mcp-Session-Id`, and there it is reported verbatim."""
    url, _ = modern_http_server

    async with Client(url) as client:
        await client.call_tool("add_todo", {"text": "modern", "context": "x"})
    time.sleep(0.5)
    modern = (_call_events(capture_queue)[-1].parameters or {}).get("extra", {})
    assert "sessionId" not in modern

    async with Client(url, mode="legacy") as client:
        await client.call_tool("add_todo", {"text": "legacy", "context": "x"})
    time.sleep(0.5)
    legacy = (_call_events(capture_queue)[-1].parameters or {}).get("extra", {})
    assert isinstance(legacy.get("sessionId"), str) and legacy["sessionId"]


@pytest.mark.asyncio
async def test_injected_schema_survives_the_wire(modern_http_server, capture_queue):
    """The SDK validates every outbound spec result against the negotiated
    protocol surface, so a malformed injected schema fails server-side rather
    than reaching the agent. Listing over HTTP proves ours is valid."""
    url, _ = modern_http_server
    async with Client(url) as client:
        listed = await client.list_tools()

    add = next(t for t in listed.tools if t.name == "add_todo")
    assert list(add.input_schema["properties"])[-2:] == ["session_id", "context"]
    assert "_mcp_instructions" in add.output_schema["properties"]
