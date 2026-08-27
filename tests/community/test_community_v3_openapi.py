"""End-to-end tests for AgentCat against OpenAPI-generated FastMCP servers.

These drive the real middleware dispatch (``tools/list`` and ``tools/call``)
through a FastMCP ``Client`` against tools that hold a live ``httpx.AsyncClient``
(mock transport, no network). This is the coverage that was missing when the
``copy.deepcopy(tool)`` regression (PR #38) shipped: the rest of the suite only
ever exercises plain function tools, which deep-copy cleanly.
"""

import copy

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.adapters import community
from agentcat.modules.constants import SESSION_ID_PARAM

from ..test_utils import sid
from ..test_utils.community_client import create_community_test_client
from ..test_utils.community_openapi_server import (
    HAS_FASTMCP_V3,
    OPENAPI_TOOL_NAMES,
    create_community_openapi_server,
)

pytestmark = pytest.mark.skipif(
    not HAS_FASTMCP_V3,
    reason="Requires FastMCP v3+ (OpenAPI provider)",
)

CONTEXT_DESC = "Why are you making this tool call?"


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


def _options(**overrides) -> AgentCatOptions:
    base = {"enable_tracing": True, "custom_context_description": CONTEXT_DESC}
    base.update(overrides)
    return AgentCatOptions(**base)


def _tool_call_events(events, name=None):
    out = [e for e in events if e.event_type == "mcp:tools/call"]
    return [e for e in out if name is None or e.resource_name == name]


async def test_list_tools_injects_handles_and_context(capture):
    """Every OpenAPI tool exposes the injected parameters to the client."""
    server = create_community_openapi_server()
    track(server, "test_project", _options())

    async with create_community_test_client(server) as client:
        tools = await client.list_tools()

    by_name = {t.name: t for t in tools}
    for name in OPENAPI_TOOL_NAMES:
        assert name in by_name, f"{name} missing from list_tools"
        schema = by_name[name].inputSchema
        props = schema.get("properties", {})
        assert SESSION_ID_PARAM in props, f"session_id not injected into {name}"
        assert "context" in props, f"context not injected into {name}"
        assert props["context"]["description"] == CONTEXT_DESC
        # session_id is never required — omitting it is the minting signal —
        # but context is, as it was in 1.x.
        assert SESSION_ID_PARAM not in schema.get("required", [])
        assert "context" in schema["required"]


async def test_no_copy_error_logged(capture, monkeypatch):
    """A tools/list against OpenAPI tools must not log any injection failure."""
    logged: list[str] = []
    monkeypatch.setattr(community, "write_to_log", logged.append)

    server = create_community_openapi_server()
    track(server, "test_project", _options())

    async with create_community_test_client(server) as client:
        await client.list_tools()

    failures = [line for line in logged if "injection failed" in line]
    assert failures == [], f"unexpected injection failures logged: {failures}"


async def test_original_tools_not_mutated(capture):
    """Injection must not mutate the server's cached tools across repeated lists."""
    server = create_community_openapi_server()
    track(server, "test_project", _options())

    async def raw_severity_props():
        # run_middleware=False returns the server's cached tools, un-injected.
        raw = {t.name: t for t in await server.list_tools(run_middleware=False)}
        return (raw["get_severity"].parameters or {}).get("properties", {})

    # The server's own cached tool never gains an injected param.
    assert "context" not in await raw_severity_props()

    async with create_community_test_client(server) as client:
        first = {t.name: t.inputSchema for t in await client.list_tools()}
        second = {t.name: t.inputSchema for t in await client.list_tools()}

    # Client sees the injection...
    assert "context" in first["get_severity"]["properties"]
    # ...repeated calls are stable...
    assert first == second
    # ...and the cached originals remain clean after the middleware ran.
    assert "context" not in await raw_severity_props()


async def test_call_tool_strips_injected_params_and_captures_intent(capture):
    """Injected params are captured on the event and stripped before the
    downstream HTTP call."""
    requests: list = []
    server = create_community_openapi_server(record_requests=requests)
    track(server, "test_project", _options())

    async with create_community_test_client(server) as client:
        await client.call_tool(
            "get_severity",
            {
                "id": "42",
                "context": "investigating an outage",
                SESSION_ID_PARAM: sid("openapi"),
            },
        )

    calls = _tool_call_events(capture, "get_severity")
    assert calls, "no tools/call event captured"
    event = calls[-1]
    assert event.user_intent == "investigating an outage"
    assert event.session_id == sid("openapi")
    # The event records the RAW arguments the agent sent.
    assert event.parameters["arguments"]["context"] == "investigating an outage"
    # Neither the intent nor the handle may reach the customer's backend.
    assert requests, "downstream request was not recorded"
    assert not any(b"investigating an outage" in (r.content or b"") for r in requests)
    assert not any("investigating an outage" in str(r.url) for r in requests)
    assert not any(sid("openapi") in str(r.url) for r in requests)


async def test_call_tool_error_captured(capture):
    """A failing OpenAPI HTTP call surfaces to the client and is captured."""
    from fastmcp.exceptions import ToolError

    server = create_community_openapi_server()
    track(server, "test_project", _options())

    async with create_community_test_client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("boom", {})

    boom = _tool_call_events(capture, "boom")
    assert boom, "no tools/call event captured for boom"
    assert boom[-1].is_error is True
    assert boom[-1].error is not None
    assert boom[-1].error["platform"] == "python"


async def test_get_more_tools_alongside_openapi(capture):
    """get_more_tools coexists with OpenAPI tools and keeps its own context."""
    server = create_community_openapi_server()
    track(server, "test_project", _options(enable_report_missing=True))

    async with create_community_test_client(server) as client:
        by_name = {t.name: t for t in await client.list_tools()}
        assert "get_more_tools" in by_name
        # get_more_tools carries its own context arg by design; other tools get
        # one injected.
        assert "context" in by_name["get_severity"].inputSchema.get("properties", {})
        assert by_name["get_more_tools"].inputSchema["required"] == [
            "context",
            "session_id",
        ]
        result = await client.call_tool(
            "get_more_tools", {"context": "need more tools"}
        )
        assert "Unfortunately" in "".join(
            c.text for c in result.content if hasattr(c, "text")
        )

    assert _tool_call_events(capture, "get_more_tools")


async def test_tools_call_event_response_is_json_safe(capture):
    """The captured event's response must survive the generated API client."""
    import json

    from agentcat_api.api_client import ApiClient

    server = create_community_openapi_server()
    track(server, "test_project", _options())

    async with create_community_test_client(server) as client:
        await client.call_tool("get_severity", {"id": "7"})

    event = _tool_call_events(capture, "get_severity")[-1]
    assert isinstance(event.response, dict)
    json.dumps(event.response)
    ApiClient.sanitize_for_serialization(ApiClient(), event.response)


async def test_openapi_tool_not_deepcopyable():
    """Precondition guard: OpenAPI tools hold non-deepcopyable runtime state."""
    server = create_community_openapi_server()
    tool = (await server.list_tools())[0]
    with pytest.raises(TypeError, match="cannot pickle '_thread.RLock' object"):
        copy.deepcopy(tool)
