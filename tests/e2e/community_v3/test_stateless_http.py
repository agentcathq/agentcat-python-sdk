"""FastMCP v3 stateless mode over real HTTP."""

from __future__ import annotations

import asyncio
import time

import pytest

from agentcat import AgentCatOptions


def AGENTCAT_OPTIONS_FACTORY() -> AgentCatOptions:
    return AgentCatOptions(enable_tracing=True, stateless=True)


pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_v3_stateless_session_id_null(v3_http_server, capture_queue):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        await client.call_tool(
            "add_todo", {"text": "s", "context": "stateless-v3"}
        )

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert call_events
    assert call_events[0].session_id is None


@pytest.mark.asyncio
async def test_v3_stateless_no_session_info_pollution(
    v3_http_server, capture_queue
):
    """After stateless requests, data.session_info.client_name stays None."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, server = v3_http_server

    async def call_once(text: str) -> None:
        async with Client(StreamableHttpTransport(url)) as client:
            await client.call_tool(
                "add_todo", {"text": text, "context": "no-bleed"}
            )

    await asyncio.gather(call_once("a"), call_once("b"))
    time.sleep(0.7)

    from agentcat.modules.internal import get_server_tracking_data

    data = get_server_tracking_data(server)
    assert data is not None
    assert data.session_info.client_name is None, (
        f"v3 stateless mode polluted session_info.client_name = "
        f"{data.session_info.client_name}"
    )
