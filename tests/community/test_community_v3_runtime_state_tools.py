"""Regression guard across every FastMCP tool type that holds runtime state.

The ``copy.deepcopy(tool)`` regression (PR #38) was reported for OpenAPI tools,
but the same failure mode applies to any tool that references live runtime state:

- ``OpenAPITool``         -> holds an ``httpx.AsyncClient`` (threading.RLock)
- ``ProxyTool``           -> holds a client factory
- ``FastMCPProviderTool`` -> holds a live sub-server reference

This suite asserts the subclass-agnostic invariants that matter for all of them:
after ``track()``, a client ``tools/list`` sees AgentCat's parameters on every
tool, nothing is logged as a failure, and a call round-trips with the injected
parameters stripped back off.
"""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.adapters import community
from agentcat.modules.constants import CONTEXT_PARAM, SESSION_ID_PARAM

from ..test_utils import sid
from ..test_utils.community_client import create_community_test_client
from ..test_utils.community_openapi_server import (
    HAS_FASTMCP_V3,
    create_community_mounted_server,
    create_community_openapi_server,
    create_community_proxy_server,
)

pytestmark = pytest.mark.skipif(
    not HAS_FASTMCP_V3,
    reason="Requires FastMCP v3+ (runtime-state tool types)",
)


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


def _build(factory):
    """Build a server from a factory, skipping if this fastmcp version can't."""
    try:
        return factory()
    except Exception as exc:  # pragma: no cover - version-dependent construction
        pytest.skip(f"{factory.__name__} unavailable on this FastMCP: {exc!r}")


@pytest.mark.parametrize(
    "factory",
    [
        create_community_openapi_server,
        create_community_proxy_server,
        create_community_mounted_server,
    ],
    ids=["openapi", "proxy", "mounted"],
)
async def test_parameters_injected_without_failures(factory, monkeypatch, capture):
    logged: list[str] = []
    monkeypatch.setattr(community, "write_to_log", logged.append)

    server = _build(factory)
    track(
        server,
        "test_project",
        AgentCatOptions(enable_tracing=True, custom_context_description="Why?"),
    )

    async with create_community_test_client(server) as client:
        tools = await client.list_tools()

    assert tools, "server exposed no tools"
    for tool in tools:
        props = tool.inputSchema.get("properties", {})
        assert SESSION_ID_PARAM in props, f"session_id not injected into {tool.name}"
        if tool.name != "get_more_tools":
            assert CONTEXT_PARAM in props, f"context not injected into {tool.name}"

    failures = [line for line in logged if "injection failed" in line]
    assert failures == [], f"injection failures logged: {failures}"


@pytest.mark.parametrize(
    "factory,tool_name,arguments",
    [
        (create_community_proxy_server, "ping", {"text": "hi"}),
        (create_community_mounted_server, "sub_sub_action", {"value": "hi"}),
    ],
    ids=["proxy", "mounted"],
)
async def test_call_round_trips_through_a_runtime_state_tool(
    factory, tool_name, arguments, capture
):
    """The injected params are stripped before a proxied/mounted tool runs."""
    server = _build(factory)
    track(server, "test_project", AgentCatOptions(enable_report_missing=False))

    async with create_community_test_client(server) as client:
        listed = {t.name for t in await client.list_tools()}
        if tool_name not in listed:  # pragma: no cover - naming differs by version
            pytest.skip(f"{tool_name} not exposed by this FastMCP: {sorted(listed)}")
        result = await client.call_tool(
            tool_name, {**arguments, SESSION_ID_PARAM: sid("runtime"), "context": "why"}
        )

    assert result.is_error is False
    assert "hi" in "".join(c.text for c in result.content if hasattr(c, "text"))
    events = [e for e in capture if e.event_type == "mcp:tools/call"]
    assert [e.session_id for e in events] == [sid("runtime")]
