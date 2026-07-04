"""Test client utilities for AgentCat tests."""

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

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
