"""FastMCP v3 Streamable-HTTP harness.

Boots a community FastMCP v3 server with `mcp.http_app(transport='streamable-http')`
mounted on a random uvicorn port. Tests connect with
`fastmcp.Client(StreamableHttpTransport(url, headers=...))`.

A test module declares `STATELESS_HTTP = True` at module scope to be served by
a stateless app instead. That is a different code path, not a configuration
detail: a stateless server builds a fresh `ServerSession` per REQUEST, so every
call reaches the last rung of the client-identity ladder — the rung whose
per-connection filing this exists to hold.

Module-scoped: one boot per test file.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

import agentcat
from agentcat import AgentCatOptions
from tests.e2e._helpers import find_free_port, wait_for_port

try:
    from fastmcp import FastMCP

    from agentcat.modules.detection import ServerFlavor, detect_server
    HAS_FASTMCP_V3 = True
except ImportError:
    FastMCP = None  # type: ignore
    HAS_FASTMCP_V3 = False


def _create_v3_todo_server() -> Any:
    if FastMCP is None:
        raise RuntimeError("fastmcp v3 is not installed; cannot run v3 e2e tests")
    mcp = FastMCP("v3-todo-server")

    # No `context` parameter of their own: the one the tests send is AgentCat's
    # injected parameter, so the wire path covers injection and stripping.
    @mcp.tool
    def add_todo(text: str) -> str:
        return f'Added todo: "{text}"'

    @mcp.tool
    def list_todos() -> str:
        return "no todos"

    return mcp


def _default_options_factory() -> AgentCatOptions:
    return AgentCatOptions(enable_tracing=True)


@pytest.fixture(scope="module")
def v3_http_server(request) -> tuple[str, Any]:
    if not HAS_FASTMCP_V3:
        pytest.skip("fastmcp v3 not installed")

    server = _create_v3_todo_server()
    if detect_server(server).flavor is not ServerFlavor.COMMUNITY_V3:
        pytest.skip("installed fastmcp is not v3")

    options_factory: Callable[[], AgentCatOptions] = getattr(
        request.module, "AGENTCAT_OPTIONS_FACTORY", _default_options_factory
    )
    options = options_factory()
    agentcat.track(server, "test_project", options)

    import uvicorn

    app = server.http_app(
        transport="streamable-http",
        stateless_http=getattr(request.module, "STATELESS_HTTP", False),
    )
    port = find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    try:
        wait_for_port(port, timeout=5.0)
    except TimeoutError:
        uv_server.should_exit = True
        thread.join(timeout=2.0)
        raise

    url = f"http://127.0.0.1:{port}/mcp/"
    yield url, server

    uv_server.should_exit = True
    thread.join(timeout=5.0)
