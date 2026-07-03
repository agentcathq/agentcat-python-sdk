"""Basic tests for Community FastMCP integration with MCPCat."""

import pytest

from mcpcat import AgentCatOptions, track

from ..test_utils.community_client import create_community_test_client
from ..test_utils.community_todo_server import (
    HAS_COMMUNITY_FASTMCP,
    create_community_todo_server,
)

# Skip all tests if community FastMCP is not available
pytestmark = pytest.mark.skipif(
    not HAS_COMMUNITY_FASTMCP,
    reason="Community FastMCP not available. Install with: pip install fastmcp"
)


class TestCommunityFastMCPBasics:
    """Test basic Community FastMCP functionality."""

    @pytest.mark.asyncio
    async def test_create_server(self):
        """Test creating a community FastMCP server."""
        server = create_community_todo_server()
        assert server.name == "todo-server"
        # v2 has _mcp_server, v3 has _local_provider
        assert hasattr(server, "_mcp_server") or hasattr(server, "_local_provider")

    @pytest.mark.asyncio
    async def test_tool_registration(self):
        """Test that tools are registered correctly."""
        server = create_community_todo_server()

        # v2: get_tools() returns dict, v3: list_tools() returns list
        if hasattr(server, "list_tools"):
            # v3 API
            tools = await server.list_tools()
            tool_names = [t.name for t in tools]
        elif hasattr(server, "get_tools"):
            # v2 API
            tools = await server.get_tools()
            tool_names = list(tools.keys())
        else:
            raise AssertionError("Server has no tool listing method")

        assert "add_todo" in tool_names
        assert "list_todos" in tool_names
        assert "complete_todo" in tool_names

    @pytest.mark.asyncio
    async def test_is_community_fastmcp_server(self):
        """Test is_community_fastmcp_server identifies community FastMCP."""
        from mcpcat.modules.compatibility import (
            is_community_fastmcp_server,
            is_compatible_server,
            is_official_fastmcp_server,
        )

        server = create_community_todo_server()

        # Should be identified as community FastMCP
        assert is_community_fastmcp_server(server) is True, (
            "Server should be identified as community FastMCP"
        )

        # Should NOT be identified as official FastMCP
        assert is_official_fastmcp_server(server) is False, (
            "Server should NOT be identified as official FastMCP"
        )

        # Should be compatible with MCPCat
        assert is_compatible_server(server) is True, (
            "Server should be compatible with MCPCat"
        )


    @pytest.mark.asyncio
    async def test_tool_execution(self):
        """Test executing tools through community client."""
        server = create_community_todo_server()

        async with create_community_test_client(server) as client:
            # Add a todo
            result = await client.call_tool("add_todo", {"text": "Test todo"})
            assert "Added todo" in str(result)
            assert "Test todo" in str(result)

            # List todos
            result = await client.call_tool("list_todos", {})
            assert "Test todo" in str(result)
            assert "○" in str(result)  # Not completed

            # Complete the todo
            result = await client.call_tool("complete_todo", {"id": 1})
            assert "Completed todo" in str(result)

            # List todos again to verify completion
            result = await client.call_tool("list_todos", {})
            assert "Test todo" in str(result)
            assert "✓" in str(result)  # Completed


class TestCommunityFastMCPWithMCPCat:
    """Test Community FastMCP integration with MCPCat tracking."""

    @pytest.mark.asyncio
    async def test_mcpcat_tracking_basic(self):
        """Test that MCPCat can track a community FastMCP server."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=False)

        # This will likely fail initially due to incompatibilities
        # but demonstrates the intended usage
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Test that tracking doesn't break basic functionality
            result = await client.call_tool("add_todo", {"text": "MCPCat test"})
            assert "Added todo" in str(result)

    @pytest.mark.asyncio
    async def test_mcpcat_tracking_with_context(self):
        """Test MCPCat context injection with community FastMCP."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=True)

        # Track the server with context enabled
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # List tools to check if context was added
            tools = await client.list_tools()

            # Check if context parameter was injected
            # This is expected to fail, demonstrating incompatibility
            for tool in tools:
                if tool.name in ["add_todo", "list_todos", "complete_todo"]:
                    # Community FastMCP might handle schemas differently
                    schema = (
                        getattr(tool, "inputSchema", None)
                        or getattr(tool, "input_schema", None)
                    )
                    assert schema is not None, (
                        f"Tool {tool.name} has no input schema"
                    )
                    assert "properties" in schema, (
                        f"Tool {tool.name} schema has no properties"
                    )

                    # This assertion will fail, showing that MCPCat's context injection
                    # doesn't work with community FastMCP
                    assert "context" in schema["properties"], (
                        f"Tool {tool.name} is missing 'context' parameter. "
                        f"Properties found: {list(schema['properties'].keys())}"
                    )

    @pytest.mark.asyncio
    async def test_multiple_operations(self):
        """Test multiple todo operations in sequence."""
        server = create_community_todo_server()

        async with create_community_test_client(server) as client:
            # Add multiple todos
            await client.call_tool("add_todo", {"text": "First todo"})
            await client.call_tool("add_todo", {"text": "Second todo"})
            await client.call_tool("add_todo", {"text": "Third todo"})

            # List all todos
            result = await client.call_tool("list_todos", {})
            result_str = str(result)

            assert "First todo" in result_str
            assert "Second todo" in result_str
            assert "Third todo" in result_str

            # Complete middle todo
            await client.call_tool("complete_todo", {"id": 2})

            # Verify only the middle one is completed
            result = await client.call_tool("list_todos", {})
            lines = str(result).split("\n")

            # These assertions assume the result format
            # They might need adjustment based on actual output
            for line in lines:
                if "Second todo" in line:
                    assert "✓" in line
                elif "First todo" in line or "Third todo" in line:
                    assert "○" in line
