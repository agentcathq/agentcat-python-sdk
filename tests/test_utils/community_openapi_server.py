"""Factories for FastMCP v3 servers whose tools hold live runtime state.

These build the classes of tool that carry non-deepcopyable runtime state and
therefore broke context injection before PR #38:

- ``OpenAPITool`` (from ``FastMCP.from_openapi``) holds a live ``httpx.AsyncClient``
  (which contains a ``threading.RLock``).
- ``ProxyTool`` (from ``create_proxy``) holds a client factory.
- ``FastMCPProviderTool`` (from a mounted sub-server) holds a live server reference.

All are driven fully in-process: the OpenAPI server uses an ``httpx.MockTransport``
so tool calls return canned responses with no network. Mirrors the flag / factory
shape of ``community_todo_server.py``.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Lazy, guarded imports so importing this module never fails the no-FastMCP CI job
# or the FastMCP v2 compatibility matrix. Callers gate on HAS_FASTMCP_V3.
try:
    import httpx
    import fastmcp
    from fastmcp import FastMCP as CommunityFastMCP
    from fastmcp.server.providers.openapi import MCPType, RouteMap

    HAS_FASTMCP_V3 = int(fastmcp.__version__.split(".")[0]) >= 3
except Exception:  # pragma: no cover - import guard
    httpx = None  # type: ignore
    CommunityFastMCP = None  # type: ignore
    MCPType = RouteMap = None  # type: ignore
    HAS_FASTMCP_V3 = False


# A Rootly-flavored spec with many distinct operationIds so list_tools returns a
# realistic multi-tool set. Every route is forced to a TOOL below. The ``boom``
# endpoint is used to exercise the HTTP-error capture path.
OPENAPI_SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "rootly", "version": "1"},
    "paths": {
        "/severities": {
            "get": {"operationId": "list_severities", "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "create_severity", "responses": {"200": {"description": "ok"}}},
        },
        "/severities/{id}": {
            "get": {
                "operationId": "get_severity",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/schedules": {
            "get": {"operationId": "list_schedules", "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "create_schedule", "responses": {"200": {"description": "ok"}}},
        },
        "/schedules/{id}": {
            "get": {
                "operationId": "get_schedule",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/incidents": {
            "get": {"operationId": "list_incidents", "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "create_incident", "responses": {"200": {"description": "ok"}}},
        },
        "/incidents/{id}": {
            "get": {
                "operationId": "get_incident",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/teams": {
            "get": {"operationId": "list_teams", "responses": {"200": {"description": "ok"}}},
        },
        "/users": {
            "get": {"operationId": "list_users", "responses": {"200": {"description": "ok"}}},
        },
        "/alerts": {
            "get": {"operationId": "list_alerts", "responses": {"200": {"description": "ok"}}},
        },
        "/boom": {
            "get": {"operationId": "boom", "responses": {"500": {"description": "boom"}}},
        },
    },
}

# Names generated from the operationIds above, excluding ``boom`` (error path).
OPENAPI_TOOL_NAMES = [
    "list_severities", "create_severity", "get_severity",
    "list_schedules", "create_schedule", "get_schedule",
    "list_incidents", "create_incident", "get_incident",
    "list_teams", "list_users", "list_alerts",
]


def _require_v3() -> None:
    if not HAS_FASTMCP_V3:
        raise ImportError("FastMCP v3+ is required for these factories.")


def create_community_openapi_server(record_requests: list | None = None) -> "FastMCP":
    """Build an OpenAPI-generated FastMCP v3 server backed by a mock transport.

    Args:
        record_requests: if provided, every outbound ``httpx.Request`` is appended
            to it, so tests can assert the downstream request never carried the
            injected ``context`` intent.

    Returns:
        A FastMCP server whose tools are ``OpenAPITool`` instances holding a live
        ``httpx.AsyncClient`` (mock transport).
    """
    _require_v3()

    def handler(request: "httpx.Request") -> "httpx.Response":
        if record_requests is not None:
            record_requests.append(request)
        if request.url.path == "/boom":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"ok": True, "path": str(request.url.path)})

    client = httpx.AsyncClient(
        base_url="http://testapi", transport=httpx.MockTransport(handler)
    )
    return CommunityFastMCP.from_openapi(
        openapi_spec=OPENAPI_SPEC,
        client=client,
        name="rootly",
        route_maps=[RouteMap(mcp_type=MCPType.TOOL)],
    )


def create_community_proxy_server() -> "FastMCP":
    """Build a proxy FastMCP v3 server whose tools are ``ProxyTool`` instances."""
    _require_v3()

    backend = CommunityFastMCP("proxy-backend")

    @backend.tool
    def ping(text: str) -> str:
        """Echo the given text."""
        return text

    @backend.tool
    def pong(text: str) -> str:
        """Echo the given text back."""
        return text

    try:
        from fastmcp.server import create_proxy

        return create_proxy(backend, name="proxy")
    except Exception:  # pragma: no cover - older v3 API fallback
        return CommunityFastMCP.as_proxy(backend, name="proxy")


def create_community_mounted_server() -> "FastMCP":
    """Build a parent server with a mounted sub-server (``FastMCPProviderTool``)."""
    _require_v3()

    parent = CommunityFastMCP("parent")
    sub = CommunityFastMCP("sub")

    @sub.tool
    def sub_action(value: str) -> str:
        """A tool provided by the mounted sub-server."""
        return value

    @sub.tool
    def sub_query(value: str) -> str:
        """Another tool provided by the mounted sub-server."""
        return value

    parent.mount(sub, namespace="sub")
    return parent
