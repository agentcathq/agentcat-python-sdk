"""Regression guard across every FastMCP v3 tool type that holds runtime state.

The ``copy.deepcopy(tool)`` regression (PR #38) was reported for OpenAPI tools,
but the same failure mode applies to any tool that references live runtime state:

- ``OpenAPITool``        -> holds an ``httpx.AsyncClient`` (threading.RLock)
- ``ProxyTool``          -> holds a client factory
- ``FastMCPProviderTool`` -> holds a live sub-server reference

This suite asserts the two subclass-agnostic invariants that matter for all of
them: after ``track()``, a client ``tools/list`` sees the injected ``context``
param on every tool, and no "Error copying tool" is ever logged.
"""

from unittest.mock import patch

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.overrides.community_v3 import middleware as v3_middleware

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
@pytest.mark.asyncio
async def test_context_injected_without_copy_errors(factory):
    server = _build(factory)
    track(
        server,
        "test_project",
        AgentCatOptions(enable_tracing=True, custom_context_description="Why?"),
    )

    with patch.object(v3_middleware, "write_to_log") as mock_log:
        async with create_community_test_client(server) as client:
            tools = await client.list_tools()

    assert tools, "server exposed no tools"
    for tool in tools:
        if tool.name == "get_more_tools":
            continue
        props = tool.inputSchema.get("properties", {})
        assert "context" in props, f"context not injected into {tool.name}"
        assert "context" in tool.inputSchema.get("required", [])

    copy_errors = [
        c.args[0]
        for c in mock_log.call_args_list
        if c.args and "Error copying tool" in str(c.args[0])
    ]
    assert copy_errors == [], f"copy failures logged: {copy_errors}"
