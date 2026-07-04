"""Test MCP Version Compatibility."""

import pytest

from agentcat.modules.compatibility import is_compatible_server
from mcp import ClientSession

from .test_utils.client import create_test_client
from .test_utils.todo_server import create_todo_server


class TestMCPVersionCompatibility:
    """Test MCP Version Compatibility."""

    def test_compatible_with_currently_installed_mcp_version(self):
        """Should be compatible with currently installed MCP version."""
        # Create a new server instance
        server = create_todo_server()

        # Test compatibility using is_compatible_server
        result = is_compatible_server(server)
        assert result is True

    @pytest.mark.asyncio
    async def test_tool_call_via_client(self):
        """Test making a tool call using the client helper."""
        # Create a new server instance
        server = create_todo_server()
        async with create_test_client(server) as client:
            result = await client.call_tool("add_todo", {"text": "Test todo item"})

            # Call the add_todo tool via client
            assert result.content[0].text == 'Added todo: "Test todo item" with ID 1'

            # Verify by listing todos
            result = await client.call_tool("list_todos")
            assert "1: Test todo item ○" in result.content[0].text
