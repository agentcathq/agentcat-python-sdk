"""Community FastMCP v2 identify-per-event smoke.

Skips when v2 is not installed.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pytest

from agentcat.modules.internal import get_server_tracking_data
from agentcat.types import UserIdentity


pytestmark = pytest.mark.e2e


def _set_identify(server, fn) -> None:
    # v2 stores tracking data against server._mcp_server (the lowlevel Server),
    # not the FastMCP wrapper.
    target = getattr(server, "_mcp_server", server)
    data = get_server_tracking_data(target)
    assert data is not None
    data.options.identify = fn


@pytest.mark.asyncio
async def test_v2_identify_hook_receives_real_extra(
    v2_http_server, capture_queue
):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, server = v2_http_server
    seen: list = []

    def identify(_req: Any, extra: Any) -> Optional[UserIdentity]:
        seen.append(extra)
        return UserIdentity(user_id="v2-user", user_name=None, user_data=None)

    _set_identify(server, identify)
    try:
        async with Client(
            StreamableHttpTransport(url, headers={"X-Identify-V2": "yes"})
        ) as client:
            await client.call_tool(
                "add_todo", {"text": "id-v2", "context": "id"}
            )

        time.sleep(0.5)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert call_events
        assert call_events[0].identify_actor_given_id == "v2-user"
        assert seen, "v2 identify hook never invoked"
    finally:
        _set_identify(server, None)
