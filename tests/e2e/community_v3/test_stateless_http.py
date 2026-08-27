"""Per-request resolution over a STATELESS Streamable-HTTP server (community).

The `stateless` option is gone in 2.0 — resolution is per request either way —
so these guard what the option used to protect: a task handle that survives
without any server-side session, and client identity that never leaks from one
connection to another even though one middleware instance serves them all.

The transport is the point. `stateless_http=True` builds a fresh
`ServerSession` per REQUEST, so the middleware's per-connection filing of the
handshake `clientInfo` has nothing to match and the ladder falls all the way
through on EVERY call. That is exactly where a single "last seen" slot — what
this rung used to be — renames one client's call after another's handshake, and
it is why the fix needed a transport test rather than only a unit one.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.e2e

# Read by this tree's conftest: the app really is served statelessly, so the
# transport builds a fresh session per REQUEST and nothing survives a call.
STATELESS_HTTP = True


@pytest.mark.asyncio
async def test_every_call_carries_a_task_handle(v3_http_server, capture_queue):
    """Handles are resolved per request from the arguments, so nothing is held
    server-side: session_id carries the task, minted or echoed."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        first = await client.call_tool("add_todo", {"text": "s", "context": "x"})
        text = "".join(c.text for c in first.content if hasattr(c, "text"))
        minted = text.split("session_id: ")[1].split("\n")[0]

        await client.call_tool("add_todo", {"text": "s2", "session_id": minted})

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert len(call_events) == 2
    assert [e.session_id for e in call_events] == [minted, minted]


@pytest.mark.asyncio
async def test_two_clients_different_clientinfo_dont_bleed(
    v3_http_server, capture_queue
):
    """One middleware object serves every connection, and its handshake capture
    is the LAST rung of the identity ladder — the one a stateless server always
    reaches, because the session that handshook is gone by the time the call
    arrives. It is filed per connection, so it can only ever answer for the
    connection that made it.

    Each event is matched to the call that produced it by the argument that
    call sent, so this asserts ATTRIBUTION: a shared slot puts one name on both
    events, and a set-membership check can be satisfied by the wrong pairing.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.types import Implementation

    url, _ = v3_http_server

    async def call_as(name: str, version: str, text: str) -> None:
        async with Client(
            StreamableHttpTransport(url),
            client_info=Implementation(name=name, version=version),
        ) as client:
            await client.call_tool("add_todo", {"text": text, "context": "no-bleed"})

    await asyncio.gather(
        call_as("Cursor", "2.6.22", "from-cursor"),
        call_as("Claude", "1.0.0", "from-claude"),
    )
    time.sleep(0.7)

    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert len(call_events) == 2
    attributed = {e.parameters["arguments"]["text"]: e.client_name for e in call_events}
    # No name at all is the honest answer for a connection that is already
    # gone; SOMEONE ELSE's name never is.
    #
    # The tolerance is deliberate and specific to THIS era. FastMCP 3 speaks the
    # pre-2026 wire, where `clientInfo` travels once, in `initialize` — so the
    # per-connection capture is the only rung that can answer, and a stateless
    # server has already discarded the connection that made it. Absence is
    # unknowable here, not a defect. FastMCP 4 re-stamps identity into `_meta`
    # on every request, so its sibling
    # (`tests/e2e/community_v4/test_stateless_http.py`) asserts exact name and
    # version. Do not loosen that one to match this.
    assert attributed["from-cursor"] in (None, "Cursor"), attributed
    assert attributed["from-claude"] in (None, "Claude"), attributed
