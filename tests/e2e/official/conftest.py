"""Uvicorn-in-thread harness for the official MCP SDK.

A test module declares an `AGENTCAT_OPTIONS_FACTORY` (callable returning
`AgentCatOptions`) at module scope; `official_http_server` boots a fresh
FastMCP todo server for the module, calls `agentcat.track(...)` with those
options, mounts the server's Streamable-HTTP app, and yields the URL.

Module-scoped: one boot per test file, not per test.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Tuple

import pytest
import uvicorn

import agentcat
from agentcat import AgentCatOptions

from tests.e2e._helpers import find_free_port, wait_for_port
from tests.test_utils.todo_server import create_todo_server


def _default_options_factory() -> AgentCatOptions:
    return AgentCatOptions(enable_tracing=True)


@pytest.fixture(scope="module")
def official_http_server(request) -> Tuple[str, Any]:
    """Boot a Streamable-HTTP MCP server for the test module.

    Reads the module attribute `AGENTCAT_OPTIONS_FACTORY` (Callable[[], AgentCatOptions])
    if defined; otherwise uses tracing-only defaults.

    Yields:
        (url, server) — the Streamable-HTTP URL (e.g. "http://127.0.0.1:54321/mcp")
        and the FastMCP server instance under test.
    """
    options_factory: Callable[[], AgentCatOptions] = getattr(
        request.module, "AGENTCAT_OPTIONS_FACTORY", _default_options_factory
    )
    options = options_factory()
    server = create_todo_server()
    agentcat.track(server, "test_project", options)

    app = server.streamable_http_app()
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

    url = f"http://127.0.0.1:{port}/mcp"
    yield url, server

    uv_server.should_exit = True
    thread.join(timeout=5.0)
