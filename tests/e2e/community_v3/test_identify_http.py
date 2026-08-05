"""Identify-per-event behavior under the community adapter over real HTTP.

v2 has no standalone agentcat:identify event: the hook runs per tool call and
its result is stamped onto that call's event.

Tests mutate the running server's AgentCatData.options.identify to vary the hook
per scenario, and reset it in finally so later tests start clean.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

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
async def test_identify_hook_runs_under_the_community_adapter(
    v3_http_server, capture_queue
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
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, server = v3_http_server
    seen: list = []

    def identify(request: Any, extra: Any) -> UserIdentity | None:
        headers = getattr(getattr(extra, "request", None), "headers", {}) or {}
        seen.append((getattr(request, "name", None), headers.get("x-tenant")))
        return UserIdentity(
            user_id="v3-user", user_name="V3 User", user_data={"plan": "pro"}
        )

    _set_identify(server, identify)
    try:
        async with Client(
            StreamableHttpTransport(url, headers={"X-Tenant": "acme"})
        ) as client:
            await client.call_tool("add_todo", {"text": "id-v3", "context": "id"})

        time.sleep(0.5)
        assert seen == [("add_todo", "acme")], seen
        event = _last_call(capture_queue)
        # All three fields, not just the id: `user_name` and `user_data` are
        # what a customer segments and displays by, and each lands in a
        # differently-named event field.
        assert event.identify_actor_given_id == "v3-user"
        assert event.identify_actor_name == "V3 User"
        assert event.identify_data == {"plan": "pro"}
    finally:
        _set_identify(server, None)


@pytest.mark.asyncio
async def test_actor_rides_the_tool_call_event_not_a_self_event(
    v3_http_server, capture_queue
):
    """v2 stamps the actor onto every tools/call event; the standalone
    agentcat:identify event is gone."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, server = v3_http_server

    def identify(_req: Any, _extra: Any) -> UserIdentity | None:
        return UserIdentity(user_id="v3-bob", user_name=None, user_data=None)

    _set_identify(server, identify)
    try:
        async with Client(StreamableHttpTransport(url)) as client:
            await client.call_tool("add_todo", {"text": "self-v3", "context": "x"})

        time.sleep(0.5)
        assert {e.event_type for e in capture_queue} == {"mcp:tools/call"}
        assert _last_call(capture_queue).identify_actor_given_id == "v3-bob"
    finally:
        _set_identify(server, None)


@pytest.mark.asyncio
async def test_identity_is_resolved_per_call_never_cached(
    v3_http_server, capture_queue
):
    """The hook runs on EVERY call, so consecutive calls on one connection can
    return different actors — v1 cached the result for the connection's life
    and could not express this."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, server = v3_http_server
    counter = {"n": 0}

    def identify(_req: Any, _extra: Any) -> UserIdentity | None:
        counter["n"] += 1
        return UserIdentity(
            user_id=f"user-{counter['n']}", user_name=None, user_data=None
        )

    _set_identify(server, identify)
    try:
        async with Client(StreamableHttpTransport(url)) as client:
            await client.call_tool("add_todo", {"text": "first", "context": "x"})
            await client.call_tool("add_todo", {"text": "second", "context": "x"})

        time.sleep(0.5)
        events = [e for e in capture_queue if e.event_type == "mcp:tools/call"][-2:]
        assert counter["n"] == 2, "the hook did not run once per call"
        assert [e.identify_actor_given_id for e in events] == ["user-1", "user-2"]
    finally:
        _set_identify(server, None)


@pytest.mark.asyncio
async def test_returning_none_yields_an_anonymous_event(v3_http_server, capture_queue):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, server = v3_http_server

    def identify(_req: Any, _extra: Any) -> UserIdentity | None:
        return None

    _set_identify(server, identify)
    try:
        async with Client(StreamableHttpTransport(url)) as client:
            await client.call_tool("add_todo", {"text": "none", "context": "x"})

        time.sleep(0.5)
        event = _last_call(capture_queue)
        assert event.identify_actor_given_id is None
        assert event.identify_actor_name is None
    finally:
        _set_identify(server, None)


@pytest.mark.asyncio
async def test_identify_exception_does_not_break_the_tool_call(
    v3_http_server, capture_queue
):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, server = v3_http_server

    def identify(_req: Any, _extra: Any) -> UserIdentity | None:
        raise RuntimeError("identify exploded")

    _set_identify(server, identify)
    try:
        async with Client(StreamableHttpTransport(url)) as client:
            result = await client.call_tool(
                "add_todo", {"text": "boom", "context": "x"}
            )

        assert result.is_error is False
        time.sleep(0.5)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert call_events, "tool/call event must still publish despite hook crash"
        assert call_events[-1].identify_actor_given_id is None
    finally:
        _set_identify(server, None)
