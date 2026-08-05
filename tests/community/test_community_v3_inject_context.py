"""Regression guard: the community adapter never deep-copies a customer Tool.

Root cause reproduced here: OpenAPI-generated FastMCP tools hold a reference to
an ``httpx.AsyncClient``, which contains a ``threading.RLock``. A middleware
that did ``copy.deepcopy(tool)`` to inject parameters raised ``TypeError: cannot
pickle '_thread.RLock' object`` for every such tool on every ``tools/list`` —
silently dropping injection and flooding diagnostics with errors (observed for
proj_3E07PMEFqZoF9sc6QeWvoaNbpet).

The v2 adapter copies only the schema dicts and rebuilds each tool with
``model_copy(update=...)``, so the invariant is: injection succeeds on a tool
the interpreter refuses to deep-copy, and the customer's original is untouched.
"""

import copy

import pytest

from agentcat.modules.constants import CONTEXT_PARAM, SESSION_ID_PARAM
from agentcat.types import AgentCatData, AgentCatOptions

# FastMCP.from_openapi is FastMCP v3+ only. Skip this module entirely when
# FastMCP is absent (test-without-fastmcp job), without importing it at module
# top level.
try:
    import fastmcp
    import httpx
    from fastmcp import FastMCP

    HAS_FASTMCP_V3 = int(fastmcp.__version__.split(".")[0]) >= 3
except Exception:  # pragma: no cover - import guard
    HAS_FASTMCP_V3 = False

pytestmark = pytest.mark.skipif(
    not HAS_FASTMCP_V3,
    reason="Requires FastMCP v3+ (community OpenAPI provider)",
)


def _make_data() -> AgentCatData:
    return AgentCatData(
        project_id="test_project",
        options=AgentCatOptions(custom_context_description="Why are you doing this?"),
    )


def _openapi_server():
    """A FastMCP server whose only tool holds a live httpx client."""
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "acme", "version": "1"},
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
    return FastMCP.from_openapi(openapi_spec=spec, client=client, name="acme")


async def _openapi_tool():
    """An OpenAPI-generated tool holding an httpx client (non-deepcopyable)."""
    server = _openapi_server()
    return server, (await server.list_tools())[0]


async def _inject(server, tools):
    """Run the adapter's tools/list hook over a fixed tool list."""
    from agentcat.modules.adapters.community import ERA_V3, AgentCatMiddleware

    middleware = AgentCatMiddleware(_make_data(), server, ERA_V3)

    async def call_next(_context):
        return tools

    return await middleware.on_list_tools(_Context(), call_next)


class _Context:
    """The two attributes the tools/list hook reads off a MiddlewareContext."""

    method = "tools/list"
    message = None
    fastmcp_context = None


async def test_openapi_tool_is_not_deepcopyable():
    """Guard: confirms the repro condition (deepcopy raises on the RLock)."""
    _server, tool = await _openapi_tool()
    with pytest.raises(TypeError, match="cannot pickle '_thread.RLock' object"):
        copy.deepcopy(tool)


async def test_parameters_injected_into_an_openapi_tool():
    """Injection must succeed even for non-deepcopyable tools."""
    server, tool = await _openapi_tool()

    result = await _inject(server, [tool])

    assert len(result) == 1
    params = result[0].parameters
    assert SESSION_ID_PARAM in params["properties"], "session_id was not injected"
    assert CONTEXT_PARAM in params["properties"], "context was not injected"
    assert (
        params["properties"][CONTEXT_PARAM]["description"] == "Why are you doing this?"
    )
    # Required, as in 1.x: a schema-validating client refusing to send a call
    # without it is the only enforcement an injected parameter has.
    assert CONTEXT_PARAM in params["required"]


async def test_original_tool_not_mutated():
    """Injection must not mutate the server's original tool object."""
    server, tool = await _openapi_tool()
    before = copy.deepcopy(tool.parameters or {})

    result = await _inject(server, [tool])

    assert result[0] is not tool
    assert (tool.parameters or {}) == before


async def test_a_failed_injection_serves_the_customers_list(monkeypatch):
    """If the pipeline blows up, the client still gets the customer's tools."""
    from agentcat.modules.adapters import community

    def boom(*args, **kwargs):
        raise RuntimeError("injection exploded")

    monkeypatch.setattr(community, "build_injected_schemas", boom)

    server, tool = await _openapi_tool()
    assert await _inject(server, [tool]) == [tool]
