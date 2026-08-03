"""A genuinely stateless HTTP server — the transport the identity bug lived on.

Every other e2e module in this tree boots a stateful app, where one
`ServerSession` serves a whole connection. `stateless_http=True` builds a fresh
one per REQUEST, and that is not a configuration detail for AgentCat: the
session is what tells one caller from another, so under stateless HTTP the
per-connection filing of the handshake `clientInfo` has nothing to hold and
every call falls through to whatever rung answers next.

One middleware object serves every connection. A single "last seen" slot for
the handshake identity therefore let a later client's `initialize` rename an
earlier client's call — and stateless HTTP is exactly where that fires on every
call rather than never. The fix is unit-tested; this is the transport that
would have caught it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.e2e

# Read by `tests/e2e/community_v4/conftest.py`.
STATELESS_HTTP = True


def _call_events(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"]


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


async def test_a_task_handle_survives_with_no_server_side_session(
    v4_http_server, capture_queue
):
    """Handles are resolved per request from the arguments, so a server that
    keeps nothing between requests keeps the task anyway."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v4_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        first = await client.call_tool(
            "add_todo", {"text": "s", "context": "stateless first call"}
        )
        minted = _text(first).split("session_id=")[1].split(" ")[0]
        await client.call_tool("add_todo", {"text": "s2", "session_id": minted})

    time.sleep(0.5)
    events = _call_events(capture_queue)
    assert len(events) == 2
    assert [e.session_id for e in events] == [minted, minted]


async def test_concurrent_clients_never_wear_each_others_name(
    v4_http_server, capture_queue
):
    """Two clients at once, each named, on a server that remembers nothing.

    Each event is matched to the call that produced it by the argument that
    call sent, so this asserts ATTRIBUTION rather than "both names appear
    somewhere" — the shared-slot bug produced one name on both events, and a
    set membership check can be satisfied by the wrong pairing.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.types import Implementation

    url, _ = v4_http_server

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

    events = _call_events(capture_queue)
    assert len(events) == 2
    attributed = {e.parameters["arguments"]["text"]: e.client_name for e in events}
    assert attributed == {"from-cursor": "Cursor", "from-claude": "Claude"}
