"""Stateless mode behavior over real Streamable HTTP."""

from __future__ import annotations

import asyncio
import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

from mcpcat import AgentCatOptions


def MCPCAT_OPTIONS_FACTORY() -> AgentCatOptions:
    return AgentCatOptions(enable_tracing=True, stateless=True)


pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_stateless_mode_returns_null_session_id(
    official_http_server, capture_queue
):
    """In stateless mode, captured events have session_id=None."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "s", "context": "stateless"}
            )

    time.sleep(0.5)
    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    assert call_events
    # In stateless mode, the SDK-level event session_id field is None.
    assert call_events[0].session_id is None


@pytest.mark.asyncio
async def test_stateless_two_clients_different_clientinfo_dont_bleed(
    official_http_server, capture_queue
):
    """Concurrent stateless requests with different clientInfo must produce
    events whose client_name reflects the *requesting* connection, not a
    cached value from a different connection."""
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
        call_with_client("Cursor", "2.6.22", "a"),
        call_with_client("Claude", "1.0.0", "b"),
    )
    time.sleep(0.7)

    call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
    client_names = {ev.client_name for ev in call_events}
    assert "Cursor" in client_names and "Claude" in client_names, (
        f"stateless mode bled client_info across requests: {client_names}"
    )


@pytest.mark.asyncio
async def test_stateless_no_session_info_pollution(
    official_http_server, capture_queue
):
    """After multiple stateless requests, the server's data.session_info
    fields should remain unset, proving we're not caching."""
    url, server = official_http_server

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(
            read,
            write,
            client_info=Implementation(name="First", version="1.0"),
        ) as client:
            await client.initialize()
            await client.call_tool("add_todo", {"text": "1", "context": "x"})

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(
            read,
            write,
            client_info=Implementation(name="Second", version="2.0"),
        ) as client:
            await client.initialize()
            await client.call_tool("add_todo", {"text": "2", "context": "x"})

    time.sleep(0.5)

    from mcpcat.modules.internal import get_server_tracking_data

    data = get_server_tracking_data(server)
    assert data is not None
    # In stateless mode, we never cache client_info onto data.session_info.
    assert data.session_info.client_name is None, (
        f"stateless mode polluted session_info.client_name = "
        f"{data.session_info.client_name}"
    )
