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
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, server = v3_http_server
    seen: list = []

    def identify(_req: Any, extra: Any) -> UserIdentity | None:
        seen.append(extra)
        return UserIdentity(user_id="v3-user", user_name=None, user_data=None)

    _set_identify(server, identify)
    try:
        async with Client(StreamableHttpTransport(url)) as client:
            await client.call_tool("add_todo", {"text": "id-v3", "context": "id"})

        time.sleep(0.5)
        assert _last_call(capture_queue).identify_actor_given_id == "v3-user"
        assert seen, "identify hook never invoked under the community adapter"
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
