"""Tests for the dynamic tracking system."""

import asyncio
from datetime import datetime
from typing import Any, List

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server import Server
from mcp import Tool

from agentcat import track
from agentcat.types import AgentCatOptions
from agentcat.modules.internal import (
    get_server_tracking_data,
    reset_all_tracking_data,
    get_tool_timeline,
)


class TestDynamicTracking:
    """Test suite for dynamic tool tracking."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset the tracker before each test."""
        reset_all_tracking_data()
        yield
        reset_all_tracking_data()

    @pytest.fixture
    def fastmcp_server(self):
        """Create a FastMCP server instance."""
        return FastMCP("test-server")

    @pytest.fixture
    def lowlevel_server(self):
        """Create a low-level MCP server instance."""
        return Server("test-server")

    @pytest.mark.asyncio
    async def test_dynamic_tracking_fastmcp_early_registration(self, fastmcp_server):
        """Test that tools registered before track() are tracked and work correctly."""

        # Register tools before tracking
        @fastmcp_server.tool()
        def early_tool(x: int) -> str:
            return str(x)

        # Enable tracking (dynamic mode is now always on)
        track(fastmcp_server, "test-project")

        # Test the tool actually works
        result, _ = await fastmcp_server.call_tool("early_tool", {"x": 42})
        assert result[0].text == "42", f"Expected '42', got {result[0].text}"

        # Also test with different value
        result2, _ = await fastmcp_server.call_tool("early_tool", {"x": 999})
        assert result2[0].text == "999", f"Expected '999', got {result2[0].text}"

        # Verify tool is tracked
        data = get_server_tracking_data(fastmcp_server)
        assert data and "early_tool" in data.tool_registry
        assert data.tool_registry["early_tool"].tracked

    @pytest.mark.asyncio
    async def test_dynamic_tracking_fastmcp_late_registration(self, fastmcp_server):
        """Test that tools registered after track() are tracked with dynamic mode and work correctly."""
        # Enable tracking first (dynamic mode is now always on)
        track(fastmcp_server, "test-project")

        # Register tool after tracking
        @fastmcp_server.tool()
        def late_tool(x: int) -> str:
            return str(x)

        # Test the tool actually works
        result, _ = await fastmcp_server.call_tool("late_tool", {"x": 123})
        assert result[0].text == "123", f"Expected '123', got {result[0].text}"

        # Test with another value
        result2, _ = await fastmcp_server.call_tool("late_tool", {"x": -456})
        assert result2[0].text == "-456", f"Expected '-456', got {result2[0].text}"

        # Verify tool is tracked
        data = get_server_tracking_data(fastmcp_server)
        assert data and "late_tool" in data.tool_registry
        assert data.tool_registry["late_tool"].tracked

    @pytest.mark.asyncio
    async def test_late_registration_always_tracked(self, fastmcp_server):
        """Test that late registrations are always tracked and function correctly."""
        # Enable tracking
        track(fastmcp_server, "test-project")

        # Register tool after tracking
        @fastmcp_server.tool()
        def late_tool_always_tracked(x: int) -> str:
            return str(x)

        # Test the tool works correctly
        result, _ = await fastmcp_server.call_tool("late_tool_always_tracked", {"x": 777})
        assert result[0].text == "777", f"Expected '777', got {result[0].text}"

        # Test with zero
        result2, _ = await fastmcp_server.call_tool("late_tool_always_tracked", {"x": 0})
        assert result2[0].text == "0", f"Expected '0', got {result2[0].text}"

        # Check that it's tracked
        data = get_server_tracking_data(fastmcp_server)
        assert data and "late_tool_always_tracked" in data.tool_registry
        assert data.tool_registry["late_tool_always_tracked"].tracked

    @pytest.mark.asyncio
    async def test_dynamic_tool_execution_tracking(self, fastmcp_server):
        """Test that dynamically added tools are tracked during execution and return correct results."""
        # Enable tracking (dynamic is now always on)
        track(fastmcp_server, "test-project")

        # Add tool after tracking
        @fastmcp_server.tool()
        async def dynamic_tool(x: int) -> str:
            return f"Result: {x}"

        # Call the tool and verify result
        result, _ = await fastmcp_server.call_tool("dynamic_tool", {"x": 42})
        assert result[0].text == "Result: 42", f"Expected 'Result: 42', got {result[0].text}"

        # Test with different value
        result2, _ = await fastmcp_server.call_tool("dynamic_tool", {"x": 100})
        assert result2[0].text == "Result: 100", f"Expected 'Result: 100', got {result2[0].text}"

        # Test with negative value
        result3, _ = await fastmcp_server.call_tool("dynamic_tool", {"x": -5})
        assert result3[0].text == "Result: -5", f"Expected 'Result: -5', got {result3[0].text}"

        # Verify tracking
        data = get_server_tracking_data(fastmcp_server)
        assert data and "dynamic_tool" in data.tool_registry
        assert "dynamic_tool" in data.wrapped_tools
        assert data.tool_registry["dynamic_tool"].tracked

    @pytest.mark.asyncio
    async def test_tool_timeline(self, fastmcp_server):
        """Test tool registration timeline tracking and that both tools work."""

        # Register first tool
        @fastmcp_server.tool()
        def tool1(x: int) -> str:
            return str(x)

        # Enable tracking
        options = AgentCatOptions()
        track(fastmcp_server, "test-project", options)

        # Register second tool
        @fastmcp_server.tool()
        def tool2(x: int) -> str:
            return str(x * 2)  # Different logic to distinguish

        # Test both tools work correctly
        result1, _ = await fastmcp_server.call_tool("tool1", {"x": 5})
        assert result1[0].text == "5", f"tool1: Expected '5', got {result1[0].text}"

        result2, _ = await fastmcp_server.call_tool("tool2", {"x": 5})
        assert result2[0].text == "10", f"tool2: Expected '10', got {result2[0].text}"

        # Test with different values
        result3, _ = await fastmcp_server.call_tool("tool1", {"x": 100})
        assert result3[0].text == "100", f"tool1: Expected '100', got {result3[0].text}"

        result4, _ = await fastmcp_server.call_tool("tool2", {"x": 100})
        assert result4[0].text == "200", f"tool2: Expected '200', got {result4[0].text}"

        # Get timeline
        timeline = get_tool_timeline(fastmcp_server)

        # Should have both tools in timeline
        tool_names = [t["name"] for t in timeline]
        assert "tool1" in tool_names
        assert "tool2" in tool_names

        # Timeline should be sorted by registration time
        for i in range(1, len(timeline)):
            assert timeline[i]["registered_at"] >= timeline[i - 1]["registered_at"]

    @pytest.mark.asyncio
    async def test_context_injection_with_dynamic_tracking(self, fastmcp_server):
        """Test that context injection works with dynamic tracking and tool still functions."""
        # Enable tracking with context
        options = AgentCatOptions(enable_tool_call_context=True)
        track(fastmcp_server, "test-project", options)

        # Add tool after tracking
        @fastmcp_server.tool()
        def context_tool(x: int) -> str:
            return str(x * 3)  # Multiply by 3 to verify logic

        # Test the tool works with context parameter
        result, _ = await fastmcp_server.call_tool(
            "context_tool",
            {"x": 7, "context": "Testing context injection"}
        )
        assert result[0].text == "21", f"Expected '21', got {result[0].text}"

        # Test without context (should still work as context is stripped)
        result2, _ = await fastmcp_server.call_tool("context_tool", {"x": 10})
        assert result2[0].text == "30", f"Expected '30', got {result2[0].text}"

        # Test with empty context
        result3, _ = await fastmcp_server.call_tool(
            "context_tool",
            {"x": 4, "context": ""}
        )
        assert result3[0].text == "12", f"Expected '12', got {result3[0].text}"

        # List tools should show context parameter
        tools = await fastmcp_server.list_tools()

        # Find our tool
        context_tool_def = next((t for t in tools if t.name == "context_tool"), None)
        assert context_tool_def is not None

        # Should have context in parameters
        if hasattr(context_tool_def, "inputSchema"):
            schema = context_tool_def.inputSchema
        else:
            schema = context_tool_def.parameters

        if schema and "properties" in schema:
            assert "context" in schema["properties"]

    @pytest.mark.asyncio
    async def test_report_missing_tool_with_dynamic_tracking(self, fastmcp_server):
        """Test that the get_more_tools tool is added with dynamic tracking and works correctly."""
        # Enable tracking with report_missing
        options = AgentCatOptions(enable_report_missing=True)
        track(fastmcp_server, "test-project", options)

        # List tools
        tools = await fastmcp_server.list_tools()

        # Should include get_more_tools
        tool_names = [t.name for t in tools]
        assert "get_more_tools" in tool_names

        # Test calling get_more_tools
        result, _ = await fastmcp_server.call_tool(
            "get_more_tools",
            {"context": "Need a tool to translate text"}
        )
        # Should return the standard "Unfortunately" message
        result_text = result[0].text if result else ""
        assert "Unfortunately" in result_text, f"Expected 'Unfortunately' in result, got: {result_text}"
        assert "tool list" in result_text.lower(), f"Expected 'tool list' in result, got: {result_text}"

        # Test with empty context
        result2, _ = await fastmcp_server.call_tool("get_more_tools", {"context": ""})
        result2_text = result2[0].text if result2 else ""
        assert "Unfortunately" in result2_text, f"Expected 'Unfortunately' in result, got: {result2_text}"

        # Test with missing context parameter - should raise validation error
        # since context is a required parameter
        with pytest.raises(Exception, match="(?i)required"):
            await fastmcp_server.call_tool("get_more_tools", {})

    @pytest.mark.asyncio
    async def test_lowlevel_server_dynamic_tracking(self, lowlevel_server):
        """Test dynamic tracking with low-level server and verify tool execution."""

        # Define tool handler
        @lowlevel_server.list_tools()
        async def list_tools() -> List[Tool]:
            return [
                Tool(
                    name="lowlevel_tool",
                    description="A low-level tool",
                    inputSchema={"type": "object", "properties": {"value": {"type": "string"}}},
                )
            ]

        @lowlevel_server.call_tool()
        async def call_tool(name: str, arguments: dict) -> List[Any]:
            if name == "lowlevel_tool":
                value = arguments.get("value", "default")
                return [{"type": "text", "text": f"Low-level result: {value}"}]
            raise ValueError(f"Unknown tool: {name}")

        # Enable dynamic tracking
        options = AgentCatOptions()
        track(lowlevel_server, "test-project", options)

        # List tools to trigger tracking
        tools = await list_tools()
        assert len(tools) == 1
        assert tools[0].name == "lowlevel_tool"

        # Test tool execution
        result = await call_tool("lowlevel_tool", {"value": "test123"})
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "Low-level result: test123"

        # Test with empty arguments
        result2 = await call_tool("lowlevel_tool", {})
        assert result2[0]["type"] == "text"
        assert result2[0]["text"] == "Low-level result: default"

        # Test unknown tool raises error
        with pytest.raises(ValueError, match="Unknown tool: nonexistent"):
            await call_tool("nonexistent", {})

        # Verify tracking setup
        data = get_server_tracking_data(lowlevel_server)
        assert data and data.tracker_initialized

    @pytest.mark.asyncio
    async def test_multiple_servers_isolation(self):
        """Test that multiple servers can be tracked independently and both function correctly."""
        server1 = FastMCP("server1")
        server2 = FastMCP("server2")

        # Track both servers
        options = AgentCatOptions()
        track(server1, "project1", options)
        track(server2, "project2", options)

        # Add tools to each server
        @server1.tool()
        def server1_tool(x: int) -> str:
            return f"Server1: {x}"

        @server2.tool()
        def server2_tool(x: int) -> str:
            return f"Server2: {x}"

        # Test server1 tool works correctly
        result1, _ = await server1.call_tool("server1_tool", {"x": 10})
        assert result1[0].text == "Server1: 10", f"Expected 'Server1: 10', got {result1[0].text}"

        result1b, _ = await server1.call_tool("server1_tool", {"x": 25})
        assert result1b[0].text == "Server1: 25", f"Expected 'Server1: 25', got {result1b[0].text}"

        # Test server2 tool works correctly
        result2, _ = await server2.call_tool("server2_tool", {"x": 20})
        assert result2[0].text == "Server2: 20", f"Expected 'Server2: 20', got {result2[0].text}"

        result2b, _ = await server2.call_tool("server2_tool", {"x": 50})
        assert result2b[0].text == "Server2: 50", f"Expected 'Server2: 50', got {result2b[0].text}"

        # Verify tools don't cross-contaminate (server1 shouldn't have server2's tool)
        with pytest.raises(Exception):  # Should raise some error when tool not found
            await server1.call_tool("server2_tool", {"x": 1})

        with pytest.raises(Exception):  # Should raise some error when tool not found
            await server2.call_tool("server1_tool", {"x": 1})

        # Verify both tools are tracked separately
        data1 = get_server_tracking_data(server1)
        data2 = get_server_tracking_data(server2)
        assert data1 and "server1_tool" in data1.tool_registry
        assert data2 and "server2_tool" in data2.tool_registry
        # NOTE: There's currently cross-contamination between servers
        # This is a known issue where tools from different servers
        # can appear in each other's registries


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
