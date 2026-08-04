"""A genuinely stateless HTTP server, on the modern official SDK.

Every other e2e module in this tree boots a stateful app, where one session
serves a whole connection. `stateless_http=True` builds a fresh one per
REQUEST, so nothing the server might have kept between calls is there — which
is the whole premise v2 is built on: handles are resolved per request from the
call's own arguments, and client identity is resolved per request from the
call's own envelope.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from mcp.client import Client
from mcp.types import Implementation

pytestmark = pytest.mark.e2e

# Read by `tests/e2e/official_modern/conftest.py`.
STATELESS_HTTP = True


def _call_events(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"]


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


async def test_a_task_handle_survives_with_no_server_side_session(
    modern_http_server, capture_queue
):
    """Nothing is held server-side, and the task is carried anyway."""
    url, _ = modern_http_server
    async with Client(url) as client:
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
    modern_http_server, capture_queue
):
    """Two named clients at once, on a server that remembers nothing.

    Exact attribution of name AND version, not "in (None, 'Cursor')": on the
    2026-07-28 wire identity does not depend on a server-side session at all.
    The client stamps `io.modelcontextprotocol/clientInfo` into `_meta` on every
    request (`mcp/client/session.py`), and the modern HTTP server re-stamps it
    from its own negotiation verdict, so the first rung of the ladder answers
    even here. A `None` on this transport is a regression in the `_meta` rung,
    not the honest silence it is on the legacy wire — see the sibling assertion
    in `tests/e2e/official/test_stateless_http.py`, which keeps the looser form
    for exactly that reason.

    Each event is matched to the call that produced it by the argument that
    call sent, so this asserts ATTRIBUTION rather than "both names appear
    somewhere" — a shared identity slot produces one name on both events, and
    a set membership check can be satisfied by the wrong pairing.
    """
    url, _ = modern_http_server

    async def call_as(name: str, version: str, text: str) -> None:
        async with Client(
            url, client_info=Implementation(name=name, version=version)
        ) as client:
            await client.call_tool("add_todo", {"text": text, "context": "no-bleed"})

    await asyncio.gather(
        call_as("Cursor", "2.6.22", "from-cursor"),
        call_as("Claude", "1.0.0", "from-claude"),
    )
    time.sleep(0.7)

    events = _call_events(capture_queue)
    assert len(events) == 2
    attributed = {
        e.parameters["arguments"]["text"]: (e.client_name, e.client_version)
        for e in events
    }
    assert attributed == {
        "from-cursor": ("Cursor", "2.6.22"),
        "from-claude": ("Claude", "1.0.0"),
    }
