"""Community FastMCP v2 Streamable-HTTP harness.

v2 is the legacy FastMCP architecture (ToolManager-based). Skips gracefully
when v2 is not installed — the typical dev venv has v3 installed, and v2
would conflict with v3 (same package name, different majors).

The harness uses v2's `streamable_http_app()` (or fallbacks) on a uvicorn
thread, mirroring the official-SDK pattern.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Tuple

import pytest

import mcpcat
from mcpcat import AgentCatOptions
from mcpcat.modules.compatibility import (
    is_community_fastmcp_v2,
    is_community_fastmcp_v3,
)

from tests.e2e._helpers import find_free_port, wait_for_port

try:
    from fastmcp import FastMCP as CommunityFastMCP

    HAS_FASTMCP = True
except ImportError:
    CommunityFastMCP = None  # type: ignore
    HAS_FASTMCP = False


def _create_v2_todo_server() -> Any:
    if CommunityFastMCP is None:
        raise RuntimeError("fastmcp not installed")
    mcp = CommunityFastMCP("v2-todo")

    @mcp.tool()
    def add_todo(text: str, context: str = "") -> str:
        return f'Added: "{text}"'

    return mcp


def _default_options_factory() -> AgentCatOptions:
    return AgentCatOptions(enable_tracing=True)


@pytest.fixture(scope="module")
def v2_http_server(request) -> Tuple[str, Any]:
    if not HAS_FASTMCP:
        pytest.skip("fastmcp not installed")

    server = _create_v2_todo_server()
    if is_community_fastmcp_v3(server):
        pytest.skip(
            "installed fastmcp is v3, not v2 — v2 e2e tests require fastmcp<3"
        )
    if not is_community_fastmcp_v2(server):
        pytest.skip("server is not detected as community FastMCP v2")

    options_factory: Callable[[], AgentCatOptions] = getattr(
        request.module, "MCPCAT_OPTIONS_FACTORY", _default_options_factory
    )
    options = options_factory()
    mcpcat.track(server, "test_project", options)

    import uvicorn

    # v2 may expose either streamable_http_app() or http_app(transport=...).
    # Try the conventional names; fall back if needed.
    if hasattr(server, "streamable_http_app"):
        app = server.streamable_http_app()
    elif hasattr(server, "http_app"):
        app = server.http_app(transport="streamable-http")
    else:
        pytest.skip(
            "v2 server has no recognized streamable_http_app/http_app method"
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

    url = f"http://127.0.0.1:{port}/mcp"
    yield url, server

    uv_server.should_exit = True
    thread.join(timeout=5.0)
