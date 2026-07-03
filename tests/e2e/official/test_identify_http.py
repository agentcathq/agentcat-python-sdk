"""Identify-per-event behavior over real Streamable HTTP.

Tests mutate the running server's AgentCatData.options.identify to vary the hook
per scenario. The default options-factory is tracing-only with no identify;
identify-swapping on the live server matches the pattern used by
tests/test_stateless.py.

Each test resets the hook in finally so subsequent tests start clean.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from mcpcat.modules.internal import get_server_tracking_data
from mcpcat.types import UserIdentity


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
    url, server = official_http_server
    received_extras: list = []

    def identify(request: Any, extra: Any) -> Optional[UserIdentity]:
        received_extras.append(extra)
        return UserIdentity(user_id="alice", user_name="Alice", user_data=None)

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
        assert received_extras, "identify hook never invoked"
        ev = _last_call(capture_queue)
        assert ev.identify_actor_given_id == "alice"
    finally:
        _set_identify(server, None)


@pytest.mark.asyncio
async def test_mcpcat_identify_self_event_published_per_request(
    official_http_server, capture_queue
):
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
        identify_events = [
            e for e in capture_queue if e.event_type == "agentcat:identify"
        ]
        assert identify_events, (
            f"expected agentcat:identify event, got "
            f"{[e.event_type for e in capture_queue]}"
        )
        assert identify_events[0].identify_actor_given_id == "bob"
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
async def test_identify_returning_none_yields_no_self_event(
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
        identify_events = [
            e for e in capture_queue if e.event_type == "agentcat:identify"
        ]
        assert not identify_events, (
            f"identify returned None; should NOT publish self-event, got "
            f"{len(identify_events)}"
        )
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
