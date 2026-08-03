"""FastMCP v4 Streamable-HTTP harness.

The 4.x sibling of `tests/e2e/community_v3/conftest.py`: a community FastMCP 4
server on `mcp.http_app()`, mounted on a random uvicorn port, with tests
connecting through `fastmcp.Client(StreamableHttpTransport(url))`.

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

    HAS_FASTMCP_V4 = True
except ImportError:  # pragma: no cover - import guard
    FastMCP = None  # type: ignore
    HAS_FASTMCP_V4 = False


def _create_v4_todo_server() -> Any:
    if FastMCP is None:  # pragma: no cover - import guard
        raise RuntimeError("fastmcp v4 is not installed; cannot run v4 e2e tests")
    mcp = FastMCP("v4-todo-server")

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
def v4_http_server(request) -> tuple[str, Any]:
    if not HAS_FASTMCP_V4:  # pragma: no cover - import guard
        pytest.skip("fastmcp v4 not installed")

    server = _create_v4_todo_server()
    if detect_server(server).flavor is not ServerFlavor.COMMUNITY_V4:
        pytest.skip("installed fastmcp is not v4")

    options_factory: Callable[[], AgentCatOptions] = getattr(
        request.module, "AGENTCAT_OPTIONS_FACTORY", _default_options_factory
    )
    agentcat.track(server, "test_project", options_factory())

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
        wait_for_port(port, timeout=10.0)
    except TimeoutError:  # pragma: no cover - boot failure
        uv_server.should_exit = True
        thread.join(timeout=2.0)
        raise

    url = f"http://127.0.0.1:{port}/mcp/"
    yield url, server

    uv_server.should_exit = True
    thread.join(timeout=5.0)
