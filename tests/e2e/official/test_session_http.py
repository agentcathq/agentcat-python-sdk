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
async def test_identity_rides_every_call_not_just_the_first(
    official_http_server, capture_queue
):
    """Name AND version on EVERY event of a connection.

    Reading only the last event cannot tell "resolved per request" from
    "resolved once and reused", and the two differ exactly where it matters: a
    rung that answers only for the call following the handshake leaves every
    later event of a long-lived connection anonymous. This is a STATEFUL app,
    so the `ServerSession` that captured the handshake is still there for all
    three calls — the rung has to answer more than once.
    """
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(
            read,
            write,
            client_info=Implementation(name="Cursor", version="2.6.22"),
        ) as client:
            await client.initialize()
            for n in range(3):
                await client.call_tool(
                    "add_todo", {"text": f"call-{n}", "context": "id"}
                )

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
                "add_todo",
                {
                    "text": "one",
                    "session_id": "start",
                    "context": "first call of the task",
                },
            )
            text = _text(first)
            assert "[session_id issued — see this tool's session_id parameter description]" in text  # noqa: E501
            minted = text.split("session_id: ")[1].split("\n")[0]
            assert minted.startswith("ses_")

            second = await client.call_tool(
                "add_todo", {"text": "two", "session_id": minted}
            )
            # Already supplied: nothing is minted back a second time.
            assert "[session_id issued — see this tool's session_id parameter description]" not in _text(second)  # noqa: E501

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
    """Nothing is stored server-side, so two agents that each send `start`
    get two different tasks — start always begins a new, unrelated one."""
    url, _ = official_http_server

    async def call_once(text: str) -> str:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
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
