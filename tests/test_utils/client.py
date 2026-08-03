"""Test client utilities for AgentCat tests (official MCP SDK 1.x).

Import-safe under mcp 2.x, which removed both symbols below; see the note in
`todo_server.py`. `test_utils.modern_server.create_modern_client` is the 2.x
counterpart.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from mcp import ClientSession

try:
    from mcp.shared.memory import create_connected_server_and_client_session
except ImportError:  # mcp 2.x
    create_connected_server_and_client_session = None

try:
    from mcp.server import FastMCP

    HAS_FASTMCP = True
except ImportError:
    FastMCP = None
    HAS_FASTMCP = False


@asynccontextmanager
async def create_test_client(server: Any) -> AsyncGenerator[ClientSession, None]:
    """Create a test client for the given server.

    This creates a properly connected MCP client/server pair with full
    request context support, similar to how a real MCP connection works.

    Note: MCP v1.2.0 doesn't support passing client_info to create_connected_server_and_client_session.
    The client_info parameter is kept for compatibility but ignored.

    Usage:
        server = create_todo_server()
        track(server, options)

        async with create_test_client(server) as client:
            result = await client.call_tool("add_todo", {"text": "Test"})
    """
    if create_connected_server_and_client_session is None:
        raise ImportError(
            "mcp.shared.memory.create_connected_server_and_client_session was "
            "removed in mcp 2.x. Use test_utils.modern_server.create_modern_client."
        )

    # MCP v1.2.0 doesn't support client_info parameter
    # Default client name is "mcp" and version is "0.1.0"

    # Handle both FastMCP and low-level Server
    if hasattr(server, "_mcp_server"):
        # FastMCP server
        async with create_connected_server_and_client_session(
            server=server._mcp_server
        ) as client:
            yield client
    else:
        # Low-level Server
        async with create_connected_server_and_client_session(server=server) as client:
            yield client
