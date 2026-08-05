"""Uvicorn-in-thread harness for the modern official MCP SDK (mcp 2.x).

The legacy sibling (`tests/e2e/official/conftest.py`) boots a FastMCP v1 todo
server; this one boots either 2.x flavor over the same real Streamable-HTTP
transport, so the assertions cover the wire path a customer actually deploys —
headers, session ids, the per-request `_meta` envelope — none of which the
in-process client exercises.

A test module declares `AGENTCAT_OPTIONS_FACTORY` (callable returning
`AgentCatOptions`) and `SERVER_FACTORY` (callable returning the server to
track) at module scope, and `STATELESS_HTTP = True` to be served by a stateless
app — a different code path, where the transport builds a fresh session per
REQUEST and nothing survives between calls. Module-scoped: one boot per test
file, not per test.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest
import uvicorn

import agentcat
from agentcat import AgentCatOptions
from tests.e2e._helpers import find_free_port, wait_for_port
from tests.test_utils.modern_server import create_lowlevel_todo_server


def _default_options_factory() -> AgentCatOptions:
    return AgentCatOptions(enable_tracing=True)


@pytest.fixture(scope="module")
def modern_http_server(request) -> tuple[str, Any]:
    """Boot a Streamable-HTTP MCP server for the test module.

    Yields:
        (url, server) — the Streamable-HTTP URL and the tracked server.
    """
    options_factory: Callable[[], AgentCatOptions] = getattr(
        request.module, "AGENTCAT_OPTIONS_FACTORY", _default_options_factory
    )
    server_factory: Callable[[], Any] = getattr(
        request.module, "SERVER_FACTORY", create_lowlevel_todo_server
    )
    server = server_factory()
    agentcat.track(server, "test_project", options_factory())

    app = server.streamable_http_app(
        stateless_http=getattr(request.module, "STATELESS_HTTP", False)
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

    yield f"http://127.0.0.1:{port}/mcp", server

    uv_server.should_exit = True
    thread.join(timeout=5.0)
