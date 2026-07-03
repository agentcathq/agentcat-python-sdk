"""Identify-per-event behavior under FastMCP v3 middleware over real HTTP."""

from __future__ import annotations

import time
from typing import Any, Optional

import pytest

from mcpcat.modules.internal import get_server_tracking_data
from mcpcat.types import UserIdentity


pytestmark = pytest.mark.e2e


def _set_identify(server, fn) -> None:
    data = get_server_tracking_data(server)
    assert data is not None
    data.options.identify = fn


@pytest.mark.asyncio
async def test_identify_hook_runs_under_v3_middleware(
    v3_http_server, capture_queue
):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, server = v3_http_server
    seen: list = []

    def identify(_req: Any, extra: Any) -> Optional[UserIdentity]:
        seen.append(extra)
        return UserIdentity(user_id="v3-user", user_name=None, user_data=None)

    _set_identify(server, identify)
    try:
        async with Client(StreamableHttpTransport(url)) as client:
            await client.call_tool(
                "add_todo", {"text": "id-v3", "context": "id"}
            )

        time.sleep(0.5)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert call_events
        assert call_events[0].identify_actor_given_id == "v3-user"
        assert seen, "identify hook never invoked under v3"
    finally:
        _set_identify(server, None)


@pytest.mark.asyncio
async def test_mcpcat_identify_self_event_via_v3_middleware(
    v3_http_server, capture_queue
):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, server = v3_http_server

    def identify(_req: Any, _extra: Any) -> Optional[UserIdentity]:
        return UserIdentity(user_id="v3-bob", user_name=None, user_data=None)

    _set_identify(server, identify)
    try:
        async with Client(StreamableHttpTransport(url)) as client:
            await client.call_tool(
                "add_todo", {"text": "self-v3", "context": "x"}
            )

        time.sleep(0.5)
        identify_events = [
            e for e in capture_queue if e.event_type == "agentcat:identify"
        ]
        assert identify_events, (
            f"expected agentcat:identify under v3, got "
            f"{[e.event_type for e in capture_queue]}"
        )
        assert identify_events[0].identify_actor_given_id == "v3-bob"
    finally:
        _set_identify(server, None)
