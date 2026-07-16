"""Event-level redaction hook (redact_event) over real-wire payloads.

Unlike the string-level hook (see test_redaction_http.py, xfail-tracked), the
event-level hook receives the Pydantic event object directly, so it works on
the live publish path today.
"""

from __future__ import annotations

import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agentcat.modules.internal import get_server_tracking_data


pytestmark = pytest.mark.e2e


def _set_redact_event(server, fn) -> None:
    data = get_server_tracking_data(server)
    assert data is not None
    data.options.redact_event = fn


@pytest.mark.asyncio
async def test_hook_inspects_metadata_and_rewrites_event_fields(
    official_http_server, capture_queue
):
    url, server = official_http_server

    seen_resource_names: list[str | None] = []

    def hook(event):
        seen_resource_names.append(event.resource_name)
        if event.resource_name == "add_todo":
            modified = event.model_copy()
            modified.parameters = {"replaced": True}
            return modified
        return event

    _set_redact_event(server, hook)
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool(
                    "add_todo", {"text": "buy milk", "context": "rewrite"}
                )

        time.sleep(0.5)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert call_events
        assert "add_todo" in seen_resource_names
        assert call_events[0].parameters == {"replaced": True}

        # System-managed fields are intact
        assert call_events[0].session_id
        assert call_events[0].event_type == "mcp:tools/call"
    finally:
        _set_redact_event(server, None)


@pytest.mark.asyncio
async def test_hook_returning_none_drops_the_event(
    official_http_server, capture_queue
):
    url, server = official_http_server

    def hook(event):
        if event.event_type == "mcp:tools/call":
            return None
        return event

    _set_redact_event(server, hook)
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool("add_todo", {"text": "drop", "context": "drop"})
                await client.list_tools()

        time.sleep(1.0)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert not call_events, (
            f"redact_event returning None must drop the event, got {len(call_events)}"
        )
        # Other event types still publish
        assert capture_queue
    finally:
        _set_redact_event(server, None)


@pytest.mark.asyncio
async def test_hook_raising_drops_only_the_affected_event(
    official_http_server, capture_queue
):
    url, server = official_http_server

    def hook(event):
        if event.event_type == "mcp:tools/call":
            raise RuntimeError("hook exploded")
        return event

    _set_redact_event(server, hook)
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool("add_todo", {"text": "boom", "context": "boom"})
                # Server still serves requests after the hook error
                tools = await client.list_tools()
                assert tools.tools

        time.sleep(1.0)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert not call_events, (
            f"redact_event raising must drop the event, got {len(call_events)}"
        )
        assert capture_queue  # other events still arrived
    finally:
        _set_redact_event(server, None)
