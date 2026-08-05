"""Identify-per-event behavior over real Streamable HTTP.

v2 has no standalone agentcat:identify event: the hook runs per tool call and
its result is stamped onto that call's event.

Tests mutate the running server's AgentCatData.options.identify to vary the hook
per scenario. The default options-factory is tracing-only with no identify, so
the hook is swapped on the live server instead of re-tracking it.

Each test resets the hook in finally so subsequent tests start clean.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agentcat.modules.internal import get_server_tracking_data
from agentcat.types import UserIdentity


pytestmark = pytest.mark.e2e


def _set_identify(server, fn) -> None:
    data = get_server_tracking_data(server)
    assert data is not None
    data.options.identify = fn


def _last_call(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"][-1]


@pytest.mark.asyncio
async def test_identify_hook_receives_real_request_extra(
    official_http_server, capture_queue
):
    """`extra` carries the live HTTP request, not a placeholder.

    The header read below is verbatim the idiom the README documents for
    `resolve_session_id` ("receives the same `(request, extra)` pair as
    `identify`") — keying off a header the customer's gateway set. Only a
    socket can prove it: the in-process client has no HTTP request at all, so
    `extra.request` is None there and the assertion would be vacuous.

    Recorded rather than asserted in place: `resolve_identity` swallows every
    exception the hook raises, so an assertion inside it would surface as a
    silently anonymous event instead of a failure.
    """
    url, server = official_http_server
    seen: list = []

    def identify(request: Any, extra: Any) -> Optional[UserIdentity]:
        headers = getattr(getattr(extra, "request", None), "headers", {}) or {}
        seen.append((getattr(request, "name", None), headers.get("x-identify-hook")))
        return UserIdentity(
            user_id="alice", user_name="Alice", user_data={"plan": "pro"}
        )

    _set_identify(server, identify)
    try:
        async with streamablehttp_client(
            url, headers={"X-Identify-Hook": "yes"}
        ) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool(
                    "add_todo", {"text": "id", "context": "id"}
                )

        time.sleep(0.5)
        assert seen == [("add_todo", "yes")], seen
        ev = _last_call(capture_queue)
        # All three fields, not just the id: `user_name` and `user_data` are
        # what a customer segments and displays by, and each lands in a
        # differently-named event field.
        assert ev.identify_actor_given_id == "alice"
        assert ev.identify_actor_name == "Alice"
        assert ev.identify_data == {"plan": "pro"}
    finally:
        _set_identify(server, None)


@pytest.mark.asyncio
async def test_actor_rides_the_tool_call_event_not_a_self_event(
    official_http_server, capture_queue
):
    """v2 stamps the actor onto every tools/call event; the standalone
    agentcat:identify event is gone."""
    url, server = official_http_server

    def identify(_req: Any, _extra: Any) -> Optional[UserIdentity]:
        return UserIdentity(user_id="bob", user_name=None, user_data=None)

    _set_identify(server, identify)
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool(
                    "add_todo", {"text": "self", "context": "x"}
                )

        time.sleep(0.5)
        assert {e.event_type for e in capture_queue} == {"mcp:tools/call"}
        assert _last_call(capture_queue).identify_actor_given_id == "bob"
    finally:
        _set_identify(server, None)


@pytest.mark.asyncio
async def test_identify_can_change_identity_mid_session(
    official_http_server, capture_queue
):
    """Identify runs per-event; consecutive tool calls in the same session can
    return different identities."""
    url, server = official_http_server
    counter = {"n": 0}

    def identify(_req: Any, _extra: Any) -> Optional[UserIdentity]:
        counter["n"] += 1
        if counter["n"] == 1:
            return UserIdentity(user_id="user-A", user_name=None, user_data=None)
        return UserIdentity(user_id="user-B", user_name=None, user_data=None)

    _set_identify(server, identify)
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool(  # n=1 -> user-A
                    "add_todo", {"text": "first", "context": "x"}
                )
                await client.call_tool(  # n=2 -> user-B
                    "add_todo", {"text": "second", "context": "x"}
                )

        time.sleep(0.5)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert len(call_events) >= 2, f"expected 2 tool/call events, got {len(call_events)}"
        actor_ids = [e.identify_actor_given_id for e in call_events]
        assert "user-A" in actor_ids and "user-B" in actor_ids, (
            f"expected user-A and user-B in actor ids, got {actor_ids}"
        )
    finally:
        _set_identify(server, None)


@pytest.mark.asyncio
async def test_identify_returning_none_yields_an_anonymous_event(
    official_http_server, capture_queue
):
    url, server = official_http_server

    def identify(_req: Any, _extra: Any) -> Optional[UserIdentity]:
        return None

    _set_identify(server, identify)
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool(
                    "add_todo", {"text": "none", "context": "x"}
                )

        time.sleep(0.5)
        event = _last_call(capture_queue)
        assert event.identify_actor_given_id is None
        assert event.identify_actor_name is None
    finally:
        _set_identify(server, None)


@pytest.mark.asyncio
async def test_identify_exception_does_not_break_tool_call(
    official_http_server, capture_queue
):
    url, server = official_http_server

    def identify(_req: Any, _extra: Any) -> Optional[UserIdentity]:
        raise RuntimeError("identify exploded")

    _set_identify(server, identify)
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                # Tool call must still succeed despite identify raising.
                await client.call_tool(
                    "add_todo", {"text": "boom", "context": "x"}
                )

        time.sleep(0.5)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert call_events, "tool/call event must still publish despite hook crash"
    finally:
        _set_identify(server, None)
