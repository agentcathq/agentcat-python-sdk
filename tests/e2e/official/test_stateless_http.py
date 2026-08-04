"""Per-request resolution over a real STATELESS Streamable-HTTP server.

The `stateless` option is gone in 2.0 — resolution is per request either way —
so these guard what the option used to protect: a task handle that survives
without any server-side session, and client identity that never leaks from one
connection to another.

The transport is the point, and it is where the sibling e2e modules cannot
reach: `stateless_http=True` builds a fresh session per REQUEST, so nothing a
server might have kept between calls exists, and every identity lookup falls
through to whatever rung can still answer. A shared "last seen" slot anywhere
in that ladder fires here on every call rather than never.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

pytestmark = pytest.mark.e2e

# Read by this tree's conftest: the app really is served statelessly, so the
# transport builds a fresh session per REQUEST and nothing survives a call.
STATELESS_HTTP = True


@pytest.mark.asyncio
async def test_every_call_carries_a_task_handle(official_http_server, capture_queue):
    """Handles are resolved per request from the arguments, so nothing is held
    server-side: session_id carries the task, minted or echoed."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool(
                "add_todo", {"text": "s", "context": "stateless"}
            )
            text = "".join(c.text for c in result.content if hasattr(c, "text"))
            minted = text.split("session_id=")[1].split(" ")[0]

            await client.call_tool("add_todo", {"text": "s2", "session_id": minted})

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert len(call_events) == 2
    assert [e.session_id for e in call_events] == [minted, minted]


@pytest.mark.asyncio
async def test_two_clients_different_clientinfo_dont_bleed(
    official_http_server, capture_queue
):
    """Concurrent requests with different clientInfo must produce events whose
    client_name reflects the *requesting* connection, not a cached value from a
    different one. There is no identity cache left to bleed from.

    Each event is matched to the call that produced it by the argument that
    call sent, so this asserts ATTRIBUTION: a shared slot puts one name on both
    events, and a set-membership check can be satisfied by the wrong pairing.
    """
    url, _ = official_http_server

    async def call_with_client(name: str, version: str, text: str) -> None:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(
                read,
                write,
                client_info=Implementation(name=name, version=version),
            ) as client:
                await client.initialize()
                await client.call_tool(
                    "add_todo", {"text": text, "context": "no-bleed"}
                )

    await asyncio.gather(
        call_with_client("Cursor", "2.6.22", "from-cursor"),
        call_with_client("Claude", "1.0.0", "from-claude"),
    )
    time.sleep(0.7)

    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert len(call_events) == 2
    attributed = {e.parameters["arguments"]["text"]: e.client_name for e in call_events}
    # No name at all is the honest answer for a connection that is already
    # gone; SOMEONE ELSE's name never is.
    #
    # The tolerance is deliberate and specific to THIS wire. A pre-2026 client
    # sends `clientInfo` once, in `initialize`, so the only rung that can answer
    # is the session capture — and a stateless server rebuilds the session per
    # REQUEST, which means the handshake that carried the name belongs to an
    # object that no longer exists by the time the call arrives. Absence there
    # is unknowable, not a defect. On the 2026-07-28 wire the client re-stamps
    # its identity into `_meta` on every request, so the same scenario IS
    # strictly attributable and the modern sibling
    # (`tests/e2e/official_modern/test_stateless_http.py`) asserts exact name
    # and version. Do not loosen that one to match this.
    assert attributed["from-cursor"] in (None, "Cursor"), attributed
    assert attributed["from-claude"] in (None, "Claude"), attributed
