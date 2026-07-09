"""End-to-end tests for AgentCat against OpenAPI-generated FastMCP v3 servers.

These drive the real middleware dispatch (``tools/list`` and ``tools/call``)
through a FastMCP ``Client`` against tools that hold a live ``httpx.AsyncClient``
(mock transport, no network). This is the coverage that was missing when the
``copy.deepcopy(tool)`` regression (PR #38) shipped: the whole suite only ever
exercised plain function tools, which deep-copy cleanly.
"""

import copy
import time
from unittest.mock import patch

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.event_queue import EventQueue, set_event_queue
from agentcat.modules.overrides.community_v3 import middleware as v3_middleware

from ..test_utils.community_client import create_community_test_client
from ..test_utils.community_openapi_server import (
    HAS_FASTMCP_V3,
    OPENAPI_TOOL_NAMES,
    create_community_openapi_server,
)

pytestmark = pytest.mark.skipif(
    not HAS_FASTMCP_V3,
    reason="Requires FastMCP v3+ (OpenAPI middleware path)",
)

CONTEXT_DESC = "Why are you making this tool call?"


@pytest.fixture
def captured_events():
    """Swap in a mock-backed event queue and collect published events."""
    from agentcat.modules.event_queue import event_queue as original_queue
    from unittest.mock import MagicMock

    events: list = []
    mock_api_client = MagicMock()

    def capture_event(publish_event_request):
        events.append(publish_event_request)

    mock_api_client.publish_event = MagicMock(side_effect=capture_event)
    set_event_queue(EventQueue(api_client=mock_api_client))
    try:
        yield events
    finally:
        set_event_queue(original_queue)


def _options(**overrides) -> AgentCatOptions:
    base = dict(enable_tracing=True, custom_context_description=CONTEXT_DESC)
    base.update(overrides)
    return AgentCatOptions(**base)


def _tool_call_events(events, name=None):
    out = [e for e in events if e.event_type == "mcp:tools/call"]
    return [e for e in out if name is None or e.resource_name == name]


@pytest.mark.asyncio
async def test_list_tools_injects_context(captured_events):
    """Every OpenAPI tool exposes the injected context param to the client."""
    server = create_community_openapi_server()
    track(server, "test_project", _options())

    async with create_community_test_client(server) as client:
        tools = await client.list_tools()

    by_name = {t.name: t for t in tools}
    for name in OPENAPI_TOOL_NAMES:
        assert name in by_name, f"{name} missing from list_tools"
        schema = by_name[name].inputSchema
        props = schema.get("properties", {})
        assert "context" in props, f"context not injected into {name}"
        assert props["context"]["description"] == CONTEXT_DESC
        assert "context" in schema.get("required", []), f"context not required on {name}"


@pytest.mark.asyncio
async def test_no_copy_error_logged(captured_events):
    """A tools/list against OpenAPI tools must not log any copy failure."""
    server = create_community_openapi_server()
    track(server, "test_project", _options())

    with patch.object(v3_middleware, "write_to_log") as mock_log:
        async with create_community_test_client(server) as client:
            await client.list_tools()

    copy_errors = [
        c.args[0]
        for c in mock_log.call_args_list
        if c.args and "Error copying tool" in str(c.args[0])
    ]
    assert copy_errors == [], f"unexpected copy failures logged: {copy_errors}"


@pytest.mark.asyncio
async def test_original_tools_not_mutated(captured_events):
    """Injection must not mutate the server's cached tools across repeated lists."""
    server = create_community_openapi_server()
    track(server, "test_project", _options())

    async def raw_severity_props():
        # run_middleware=False returns the server's cached tools, un-injected.
        raw = {t.name: t for t in await server.list_tools(run_middleware=False)}
        return (raw["get_severity"].parameters or {}).get("properties", {})

    # The server's own cached tool never gains a context param.
    assert "context" not in await raw_severity_props()

    async with create_community_test_client(server) as client:
        first = {t.name: t.inputSchema for t in await client.list_tools()}
        second = {t.name: t.inputSchema for t in await client.list_tools()}

    # Client sees context injected...
    assert "context" in first["get_severity"]["properties"]
    # ...repeated calls are stable...
    assert first == second
    # ...and the cached originals remain clean after the middleware ran.
    assert "context" not in await raw_severity_props()


@pytest.mark.asyncio
async def test_call_tool_strips_context_and_captures_intent(captured_events):
    """context is captured as intent and stripped before the downstream HTTP call."""
    requests: list = []
    server = create_community_openapi_server(record_requests=requests)
    track(server, "test_project", _options())

    async with create_community_test_client(server) as client:
        await client.call_tool(
            "get_severity", {"id": "42", "context": "investigating an outage"}
        )
        time.sleep(1.0)  # let the event-queue worker drain

    calls = _tool_call_events(captured_events, "get_severity")
    assert calls, "no tools/call event captured"
    event = calls[-1]
    assert event.user_intent == "investigating an outage"
    assert (event.parameters.get("arguments") or {}) == {"id": "42"}
    # The intent string must never reach the customer's backend.
    assert requests, "downstream request was not recorded"
    assert not any(b"investigating an outage" in (r.content or b"") for r in requests)
    assert not any("investigating an outage" in str(r.url) for r in requests)


@pytest.mark.asyncio
async def test_call_tool_error_captured(captured_events):
    """A failing OpenAPI HTTP call surfaces to the client and is captured as an error."""
    server = create_community_openapi_server()
    track(server, "test_project", _options())

    async with create_community_test_client(server) as client:
        with pytest.raises(Exception):
            await client.call_tool("boom", {})
        time.sleep(1.0)

    boom = _tool_call_events(captured_events, "boom")
    assert boom, "no tools/call event captured for boom"
    assert boom[-1].is_error is True
    assert boom[-1].error is not None


@pytest.mark.asyncio
async def test_tools_list_event_serializes_openapi_tool(captured_events):
    """The tools/list event response serializes OpenAPITool subclasses cleanly."""
    server = create_community_openapi_server()
    track(server, "test_project", _options())

    async with create_community_test_client(server) as client:
        await client.list_tools()
        time.sleep(1.0)

    list_events = [e for e in captured_events if e.event_type == "mcp:tools/list"]
    assert list_events, "no tools/list event captured"
    response = list_events[-1].response
    assert response and isinstance(response.get("tools"), list)
    names = {t.get("name") for t in response["tools"]}
    assert set(OPENAPI_TOOL_NAMES).issubset(names)


@pytest.mark.asyncio
async def test_get_more_tools_alongside_openapi(captured_events):
    """get_more_tools coexists with OpenAPI tools and is excluded from context injection."""
    server = create_community_openapi_server()
    track(server, "test_project", _options(enable_report_missing=True))

    async with create_community_test_client(server) as client:
        by_name = {t.name: t for t in await client.list_tools()}
        assert "get_more_tools" in by_name
        # get_more_tools carries its own context arg by design; other tools get one injected.
        assert "context" in by_name["get_severity"].inputSchema.get("properties", {})
        result = await client.call_tool("get_more_tools", {"context": "need more tools"})
        assert result is not None


@pytest.mark.asyncio
async def test_many_tools_single_pass_no_errors(captured_events):
    """The full multi-tool spec lists in one pass with context on all and no errors."""
    server = create_community_openapi_server()
    track(server, "test_project", _options())

    with patch.object(v3_middleware, "write_to_log") as mock_log:
        async with create_community_test_client(server) as client:
            tools = await client.list_tools()

    injected = [
        t for t in tools
        if t.name != "get_more_tools"
        and "context" in t.inputSchema.get("properties", {})
    ]
    assert len(injected) >= len(OPENAPI_TOOL_NAMES)
    assert not any(
        c.args and "Error copying tool" in str(c.args[0])
        for c in mock_log.call_args_list
    )


@pytest.mark.asyncio
async def test_openapi_tool_not_deepcopyable():
    """Precondition guard: OpenAPI tools hold non-deepcopyable runtime state."""
    server = create_community_openapi_server()
    tool = (await server.list_tools())[0]
    with pytest.raises(TypeError, match="cannot pickle '_thread.RLock' object"):
        copy.deepcopy(tool)
