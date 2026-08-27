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

MINT_BACK_HEADER = "[session_id issued — see this tool's session_id parameter description]"  # noqa: E501


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
            "add_todo",
            {
                "text": "one",
                "session_id": "start",
                "context": "first call of the task",
            },
        )
        text = _text(first)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id: ")[1].split("\n")[0]
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
    """Nothing is stored server-side, so two agents that each send `start`
    get two different tasks — start always begins a new, unrelated one."""
    url, _ = modern_http_server

    async def call_once(text: str) -> str:
        async with Client(url) as client:
            result = await client.call_tool(
                "add_todo",
                {
                    "text": text,
                    "session_id": "start",
                    "context": "independent task",
                },
            )
            return _text(result).split("session_id: ")[1].split("\n")[0]

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
async def test_identity_rides_every_call_not_just_the_first(
    modern_http_server, capture_queue
):
    """Name AND version on EVERY event of a connection.

    Reading only the last event — which every other identity assertion in the
    e2e suite used to do — cannot tell "resolved per request" from "resolved
    once and reused", and the two differ exactly where it matters: a rung that
    answers only for the call that follows the handshake leaves every later
    event of a long-lived connection anonymous. Three calls, three identities.
    """
    url, _ = modern_http_server
    async with Client(
        url, client_info=Implementation(name="Cursor", version="2.6.22")
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


@pytest.mark.asyncio
async def test_identity_comes_from_the_per_request_meta_rung(
    modern_http_server, capture_queue
):
    """The FIRST rung of the ladder, isolated from the handshake rung.

    On the 2026-07-28 wire the client stamps
    `io.modelcontextprotocol/clientInfo` into `_meta` on every request, so the
    identity on the event can be read without any handshake state at all. The
    two rungs are indistinguishable while both agree — so this drives the same
    connection twice with DIFFERENT identities, which only the per-request
    envelope can express: a handshake-only ladder reports the first client's
    name on the second call.
    """
    url, _ = modern_http_server

    async def call_as(name: str, version: str, text: str) -> None:
        async with Client(
            url, client_info=Implementation(name=name, version=version)
        ) as client:
            await client.call_tool("add_todo", {"text": text, "context": "meta"})

    await call_as("First", "1.0.0", "meta-first")
    await call_as("Second", "2.0.0", "meta-second")

    time.sleep(0.5)
    events = _call_events(capture_queue)[-2:]
    assert {
        e.parameters["arguments"]["text"]: (e.client_name, e.client_version)
        for e in events
    } == {
        "meta-first": ("First", "1.0.0"),
        "meta-second": ("Second", "2.0.0"),
    }


@pytest.mark.asyncio
async def test_identity_survives_the_legacy_handshake_rung(
    modern_http_server, capture_queue
):
    """The LAST rung: a legacy client sends `clientInfo` only in `initialize`.

    `mode="legacy"` has no per-request `_meta` envelope to read, so the ladder
    falls through to the identity the SDK's own `ServerSession` captured at
    handshake time. On this STATEFUL app that session outlives the handshake,
    so both calls are still attributed — the rung has to answer more than once.
    (Under `stateless_http=True` it cannot, which is what the looser assertion
    in `tests/e2e/official/test_stateless_http.py` records.)
    """
    url, _ = modern_http_server
    async with Client(
        url,
        mode="legacy",
        client_info=Implementation(name="LegacyAgent", version="0.9.0"),
    ) as client:
        await client.call_tool("add_todo", {"text": "legacy-1", "context": "id"})
        await client.call_tool("add_todo", {"text": "legacy-2", "context": "id"})

    time.sleep(0.5)
    events = _call_events(capture_queue)[-2:]
    assert [(e.client_name, e.client_version) for e in events] == [
        ("LegacyAgent", "0.9.0")
    ] * 2


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
    assert "mcp_session" in add.output_schema["properties"]
