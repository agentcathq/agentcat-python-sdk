"""Regression tests for context injection in the FastMCP v3 middleware.

Root cause reproduced here: OpenAPI-generated FastMCP v3 tools hold a reference
to an ``httpx.AsyncClient``, which contains a ``threading.RLock``. The old
implementation did ``copy.deepcopy(tool)`` to inject the ``context`` parameter,
which raised ``TypeError: cannot pickle '_thread.RLock' object`` for every such
tool on every ``tools/list`` — silently dropping context injection and flooding
diagnostics with errors (observed for proj_3E07PMEFqZoF9sc6QeWvoaNbpet).
"""

import asyncio
from datetime import datetime, timezone

import copy
import pytest

from agentcat.modules.overrides.community_v3 import middleware as v3_middleware
from agentcat.modules.overrides.community_v3.middleware import AgentCatMiddleware
from agentcat.types import AgentCatData, AgentCatOptions, SessionInfo

# The community_v3 middleware and FastMCP.from_openapi are FastMCP v3+ only. Skip
# this module entirely when FastMCP is absent (test-without-fastmcp job) or on the
# v2 compatibility matrix, without importing fastmcp at module top level.
try:
    import httpx
    import fastmcp
    from fastmcp import FastMCP

    HAS_FASTMCP_V3 = int(fastmcp.__version__.split(".")[0]) >= 3
except Exception:  # pragma: no cover - import guard
    HAS_FASTMCP_V3 = False

pytestmark = pytest.mark.skipif(
    not HAS_FASTMCP_V3,
    reason="Requires FastMCP v3+ (community_v3 OpenAPI middleware path)",
)


def _make_data() -> AgentCatData:
    return AgentCatData(
        project_id="test_project",
        session_id="test_session",
        session_info=SessionInfo(client_name="TestClient", client_version="1.0.0"),
        last_activity=datetime.now(timezone.utc),
        options=AgentCatOptions(custom_context_description="Why are you doing this?"),
    )


def _openapi_tool():
    """An OpenAPI-generated tool holding an httpx client (non-deepcopyable)."""
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "rootly", "version": "1"},
        "paths": {
            "/severities": {
                "get": {
                    "operationId": "list_severities",
                    "summary": "List severities",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    client = httpx.AsyncClient(base_url="https://example.com")
    server = FastMCP.from_openapi(openapi_spec=spec, client=client, name="rootly")
    tools = server.list_tools()
    if asyncio.iscoroutine(tools):
        tools = asyncio.run(tools)
    return server, tools[0]


def test_openapi_tool_is_not_deepcopyable():
    """Guard: confirms the repro condition (deepcopy raises on the RLock)."""
    _server, tool = _openapi_tool()
    with pytest.raises(TypeError, match="cannot pickle '_thread.RLock' object"):
        copy.deepcopy(tool)


def test_context_injected_into_openapi_tool():
    """Context injection must succeed even for non-deepcopyable tools."""
    server, tool = _openapi_tool()
    middleware = AgentCatMiddleware(_make_data(), server)

    result = middleware._inject_context_into_tools([tool])

    assert len(result) == 1
    params = result[0].parameters
    assert "context" in params["properties"], "context was not injected"
    assert "context" in params["required"]
    assert (
        params["properties"]["context"]["description"] == "Why are you doing this?"
    )


def test_original_tool_not_mutated():
    """Injection must not mutate the server's original tool object."""
    server, tool = _openapi_tool()
    middleware = AgentCatMiddleware(_make_data(), server)

    middleware._inject_context_into_tools([tool])

    assert "context" not in (tool.parameters or {}).get("properties", {})


def test_copy_failure_logs_fastmcp_version(monkeypatch):
    """If a tool still can't be copied, the log names the fastmcp version."""
    from importlib.metadata import version

    server, tool = _openapi_tool()
    middleware = AgentCatMiddleware(_make_data(), server)

    def _boom(*args, **kwargs):
        raise RuntimeError("copy blew up")

    monkeypatch.setattr(v3_middleware.copy, "deepcopy", _boom)

    logged: list[str] = []
    monkeypatch.setattr(v3_middleware, "write_to_log", lambda msg: logged.append(msg))

    result = middleware._inject_context_into_tools([tool])

    assert result == [tool]  # falls back to the untouched original
    assert len(logged) == 1
    assert f"fastmcp {version('fastmcp')}" in logged[0]
    assert "list_severities" in logged[0]
