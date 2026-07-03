"""Client-info propagation tests over real Streamable HTTP.

NOTE: User-Agent / X-MCP-Client-Name header parsing is *not* tested e2e
because real MCP clients always populate session.client_params.clientInfo
during initialize, and that path wins over header parsing in the SDK
(see src/agentcat/modules/session.py::get_client_info_from_request_context).
The header-fallback path is covered by unit tests in tests/test_stateless.py
that mock ctx.session = None to force the fallback. e2e here verifies the
real-world path: clientInfo from the SDK propagates to events.

Uses stateless mode so each test gets independent client_info extraction
rather than fixture-level caching.
"""

from __future__ import annotations

import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

from agentcat import AgentCatOptions


def AGENTCAT_OPTIONS_FACTORY() -> AgentCatOptions:
    return AgentCatOptions(enable_tracing=True, stateless=True)


pytestmark = pytest.mark.e2e


def _last_event(capture_queue, event_type: str):
    return [e for e in capture_queue if e.event_type == event_type][-1]


@pytest.mark.asyncio
async def test_custom_clientinfo_propagates_to_event(
    official_http_server, capture_queue
):
    """ClientSession.client_info=Implementation(name=..., version=...)
    surfaces on captured events as client_name / client_version."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(
            read,
            write,
            client_info=Implementation(name="MyAgent", version="1.2.3"),
        ) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "agent", "context": "id"}
            )

    time.sleep(0.5)
    ev = _last_event(capture_queue, "mcp:tools/call")
    assert ev.client_name == "MyAgent", f"expected MyAgent, got {ev.client_name}"
    assert ev.client_version == "1.2.3"


@pytest.mark.asyncio
async def test_default_clientinfo_used_when_unspecified(
    official_http_server, capture_queue
):
    """When the client doesn't pass client_info, the SDK's default
    Implementation(name='mcp', version='...') is used and propagates."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "default", "context": "id"}
            )

    time.sleep(0.5)
    ev = _last_event(capture_queue, "mcp:tools/call")
    # SDK default is "mcp" — assert it propagated. Don't pin the version,
    # which depends on installed SDK version.
    assert ev.client_name is not None, "expected non-None client_name"


@pytest.mark.asyncio
async def test_clientinfo_with_special_characters(
    official_http_server, capture_queue
):
    """Edge case: clientInfo with special characters round-trips intact."""
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(
            read,
            write,
            client_info=Implementation(
                name="My Test Agent v2",
                version="0.1.0-beta+build123",
            ),
        ) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "edge", "context": "edge"}
            )

    time.sleep(0.5)
    ev = _last_event(capture_queue, "mcp:tools/call")
    assert ev.client_name == "My Test Agent v2"
    assert ev.client_version == "0.1.0-beta+build123"


@pytest.mark.asyncio
async def test_clientinfo_in_extra_headers_when_set(
    official_http_server, capture_queue
):
    """X-MCP-Client-* headers ride along in extra.requestInfo.headers
    even though they don't drive client_name (clientInfo wins). This proves
    customers can still inspect the headers via the captured extra."""
    url, _ = official_http_server
    async with streamablehttp_client(
        url,
        headers={
            "X-MCP-Client-Name": "HeaderClient",
            "X-MCP-Client-Version": "8.8.8",
        },
    ) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool(
                "add_todo", {"text": "hdr", "context": "hdr"}
            )

    time.sleep(0.5)
    ev = _last_event(capture_queue, "mcp:tools/call")
    headers = (
        (ev.parameters or {})
        .get("extra", {})
        .get("requestInfo", {})
        .get("headers", {})
    )
    assert headers.get("x-mcp-client-name") == "HeaderClient"
    assert headers.get("x-mcp-client-version") == "8.8.8"
