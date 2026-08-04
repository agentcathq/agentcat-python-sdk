"""Identify-per-event behavior over real Streamable HTTP (modern official SDK).

v2 has no standalone `agentcat:identify` event: the hook runs per tool call and
its result is stamped onto that call's event.

Tests mutate the running server's `AgentCatData.options.identify` to vary the
hook per scenario, rather than declaring an options factory — the hook is read
per request, so swapping it on the live server costs no second uvicorn boot.
Each test resets it in `finally` so the next one starts clean.
"""

from __future__ import annotations

import time
from typing import Any

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from agentcat.modules.internal import get_server_tracking_data
from agentcat.types import UserIdentity

pytestmark = pytest.mark.e2e


def _set_identify(server, fn) -> None:
    data = get_server_tracking_data(server)
    assert data is not None
    data.options.identify = fn


def _call_events(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"]


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


async def test_the_hook_reads_the_real_http_request(
    modern_http_server, capture_queue
):
    """`extra` carries the live HTTP request, not a placeholder.

    The hook's second argument is the SDK's `RequestContext`, and the header
    read below is verbatim the idiom the README documents for
    `resolve_session_id` ("receives the same `(request, extra)` pair as
    `identify`") — keying off a header the customer's gateway set. Only a
    socket can prove it: the in-process client has no HTTP request at all, so
    `extra.request` is None there and the assertion would be vacuous.

    `request` is the tool call's PARAMS on every flavor, which the same
    assertion pins from the other side.
    """
    url, server = modern_http_server
    # Recorded rather than asserted in place: `resolve_identity` swallows every
    # exception the hook raises, so an assertion inside it would surface as a
    # silently anonymous event instead of a failure.
    seen: list[tuple[str, str | None]] = []

    def identify(request: Any, extra: Any) -> UserIdentity | None:
        headers = getattr(getattr(extra, "request", None), "headers", {}) or {}
        tenant = headers.get("x-tenant")
        seen.append((getattr(request, "name", None), tenant))
        return UserIdentity(user_id=f"tenant:{tenant}", user_name=None, user_data=None)

    _set_identify(server, identify)
    try:
        http_client = httpx2.AsyncClient(headers={"X-Tenant": "acme"})
        async with Client(
            streamable_http_client(url, http_client=http_client)
        ) as client:
            result = await client.call_tool(
                "add_todo", {"text": "tenant", "context": "id"}
            )
            assert result.is_error is False, _text(result)

        time.sleep(0.5)
        assert seen == [("add_todo", "acme")], seen
        assert _call_events(capture_queue)[-1].identify_actor_given_id == "tenant:acme"
    finally:
        _set_identify(server, None)


async def test_the_actor_rides_the_tool_call_event_not_a_self_event(
    modern_http_server, capture_queue
):
    """v2 stamps the actor onto every tools/call event; the standalone
    `agentcat:identify` event is gone."""
    url, server = modern_http_server

    def identify(_request: Any, _extra: Any) -> UserIdentity | None:
        return UserIdentity(
            user_id="bob",
            user_name="Bob Bobson",
            user_data={"plan": "enterprise"},
        )

    _set_identify(server, identify)
    try:
        async with Client(url) as client:
            await client.call_tool("add_todo", {"text": "self", "context": "x"})

        time.sleep(0.5)
        assert {e.event_type for e in capture_queue} == {"mcp:tools/call"}
        event = _call_events(capture_queue)[-1]
        # All three fields, not just the id: `user_name` and `user_data` are
        # what a customer segments and displays by, and each lands in a
        # differently-named event field.
        assert event.identify_actor_given_id == "bob"
        assert event.identify_actor_name == "Bob Bobson"
        assert event.identify_data == {"plan": "enterprise"}
    finally:
        _set_identify(server, None)


async def test_identity_is_resolved_per_call_never_cached(
    modern_http_server, capture_queue
):
    """The hook runs on EVERY call, so consecutive calls on one connection can
    return different actors — v1 cached the result for the connection's life
    and could not express this."""
    url, server = modern_http_server
    counter = {"n": 0}

    def identify(_request: Any, _extra: Any) -> UserIdentity | None:
        counter["n"] += 1
        return UserIdentity(
            user_id=f"user-{counter['n']}", user_name=None, user_data=None
        )

    _set_identify(server, identify)
    try:
        async with Client(url) as client:
            await client.call_tool("add_todo", {"text": "first", "context": "x"})
            await client.call_tool("add_todo", {"text": "second", "context": "x"})

        time.sleep(0.5)
        events = _call_events(capture_queue)[-2:]
        assert counter["n"] == 2, "the hook did not run once per call"
        assert [e.identify_actor_given_id for e in events] == ["user-1", "user-2"]
    finally:
        _set_identify(server, None)


async def test_returning_none_yields_an_anonymous_event(
    modern_http_server, capture_queue
):
    url, server = modern_http_server

    def identify(_request: Any, _extra: Any) -> UserIdentity | None:
        return None

    _set_identify(server, identify)
    try:
        async with Client(url) as client:
            result = await client.call_tool(
                "add_todo", {"text": "none", "context": "x"}
            )
            assert result.is_error is False, _text(result)

        time.sleep(0.5)
        event = _call_events(capture_queue)[-1]
        assert event.identify_actor_given_id is None
        assert event.identify_actor_name is None
    finally:
        _set_identify(server, None)


async def test_an_async_hook_works_over_the_wire(modern_http_server, capture_queue):
    """`identify` may be `async def`, and the contract holds on a real socket.

    The cross-flavor suite (`tests/test_async_hooks.py`) proves the hook
    contract itself; this proves the request path an actual deployment takes
    still has a running loop when the hook is reached — which is what makes
    awaiting the hook viable at all, on every adapter and transport.

    The hook awaits something to make the point: a hook that merely returned
    from an `async def` would pass even if the coroutine were driven by
    accident somewhere upstream.
    """
    import asyncio

    url, server = modern_http_server

    async def identify(_request: Any, _extra: Any) -> UserIdentity | None:
        await asyncio.sleep(0)
        return UserIdentity(user_id="async-alice", user_name="Alice", user_data=None)

    _set_identify(server, identify)
    try:
        async with Client(url) as client:
            result = await client.call_tool(
                "add_todo", {"text": "async id", "context": "x"}
            )
            assert result.is_error is False, _text(result)

        time.sleep(0.5)
        event = _call_events(capture_queue)[-1]
        assert event.identify_actor_given_id == "async-alice"
        assert event.identify_actor_name == "Alice"
    finally:
        _set_identify(server, None)


async def test_a_raising_hook_does_not_break_the_call(
    modern_http_server, capture_queue
):
    """A customer hook that blows up yields an anonymous call, not a failed one
    — and not a dropped event either."""
    url, server = modern_http_server

    def identify(_request: Any, _extra: Any) -> UserIdentity | None:
        raise RuntimeError("identify exploded")

    _set_identify(server, identify)
    try:
        async with Client(url) as client:
            result = await client.call_tool(
                "add_todo", {"text": "boom", "context": "x"}
            )
            assert result.is_error is False, _text(result)

        time.sleep(0.5)
        event = _call_events(capture_queue)[-1]
        assert event.parameters["arguments"]["text"] == "boom"
        assert event.identify_actor_given_id is None
    finally:
        _set_identify(server, None)
