"""Test .track() timing flexibility with community FastMCP."""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.internal import (
    get_server_tracking_data,
    reset_all_tracking_data,
)

from ..test_utils.community_client import create_community_test_client
from ..test_utils.community_todo_server import (
    HAS_COMMUNITY_FASTMCP,
    get_lowlevel_server,
)

# Skip all tests if community FastMCP is not available
pytestmark = pytest.mark.skipif(
    not HAS_COMMUNITY_FASTMCP,
    reason="Community FastMCP not available. Install with: pip install fastmcp"
)


class TestCommunityTrackingTiming:
    """Test that .track() works when called at different stages of server setup."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset the tracker before each test."""
        reset_all_tracking_data()
        yield
        reset_all_tracking_data()

    @pytest.mark.asyncio
    async def test_track_empty_server_then_add_tools(self):
        """Test tracking a server with NO tools, then adding tools later."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        # Create empty server
        server = FastMCP("empty-server")

        # Track the empty server first (disable report_missing to truly have no tools)
        options = AgentCatOptions(enable_report_missing=False)
        track(server, "test-project", options)

        # Verify tracking is initialized even with no tools
        data = get_server_tracking_data(get_lowlevel_server(server))
        assert data is not None
        assert data.tracker_initialized
        assert len(data.tool_registry) == 0  # No tools yet

        # Now add tools AFTER tracking
        @server.tool
        def first_tool(x: int) -> str:
            return f"First: {x}"

        @server.tool
        def second_tool(x: int) -> str:
            return f"Second: {x * 2}"

        # Test that both tools work correctly
        async with create_community_test_client(server) as client:
            result1 = await client.call_tool("first_tool", {"x": 10})
            assert "First: 10" in str(result1), f"Expected 'First: 10', got {result1}"

            result2 = await client.call_tool("second_tool", {"x": 10})
            assert "Second: 20" in str(result2), f"Expected 'Second: 20', got {result2}"

        # Verify tools are tracked
        data = get_server_tracking_data(get_lowlevel_server(server))
        assert "first_tool" in data.tool_registry
        assert "second_tool" in data.tool_registry
        assert data.tool_registry["first_tool"].tracked
        assert data.tool_registry["second_tool"].tracked

    @pytest.mark.asyncio
    async def test_track_server_with_some_tools_then_add_more(self):
        """Test tracking a server with existing tools, then adding more tools."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        server = FastMCP("partial-server")

        # Add initial tools
        @server.tool
        def existing_tool1(x: int) -> str:
            return f"Existing1: {x}"

        @server.tool
        def existing_tool2(x: int) -> str:
            return f"Existing2: {x + 1}"

        # Track server with some tools (disable report_missing for cleaner counts)
        options = AgentCatOptions(enable_report_missing=False)
        track(server, "test-project", options)

        # Test initial tools work (registered when list_tools or call_tool invoked)
        async with create_community_test_client(server) as client:
            # First list tools to trigger registration
            await client.list_tools()

            # Verify initial tools are now tracked
            data = get_server_tracking_data(get_lowlevel_server(server))
            assert len(data.tool_registry) == 2
            assert "existing_tool1" in data.tool_registry
            assert "existing_tool2" in data.tool_registry
            result = await client.call_tool("existing_tool1", {"x": 5})
            assert "Existing1: 5" in str(result)

            result = await client.call_tool("existing_tool2", {"x": 5})
            assert "Existing2: 6" in str(result)

        # Add more tools after tracking
        @server.tool
        def new_tool1(x: int) -> str:
            return f"New1: {x * 3}"

        @server.tool
        def new_tool2(x: int) -> str:
            return f"New2: {x - 1}"

        # Test all tools work (both old and new)
        async with create_community_test_client(server) as client:
            # Test existing tools still work
            result = await client.call_tool("existing_tool1", {"x": 7})
            assert "Existing1: 7" in str(result)

            result = await client.call_tool("existing_tool2", {"x": 7})
            assert "Existing2: 8" in str(result)

            # Test new tools work
            result = await client.call_tool("new_tool1", {"x": 7})
            assert "New1: 21" in str(result), f"Expected 'New1: 21', got {result}"

            result = await client.call_tool("new_tool2", {"x": 7})
            assert "New2: 6" in str(result), f"Expected 'New2: 6', got {result}"

        # Verify all tools are tracked
        data = get_server_tracking_data(get_lowlevel_server(server))
        assert len(data.tool_registry) == 4
        for tool_name in ["existing_tool1", "existing_tool2", "new_tool1", "new_tool2"]:
            assert tool_name in data.tool_registry
            assert data.tool_registry[tool_name].tracked

    @pytest.mark.asyncio
    async def test_track_server_with_all_tools_already_added(self):
        """Test tracking a server after ALL its tools have been added."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        server = FastMCP("complete-server")

        # Add ALL tools before tracking
        @server.tool
        def tool_a(x: int) -> str:
            return f"A: {x}"

        @server.tool
        def tool_b(x: int) -> str:
            return f"B: {x * 2}"

        @server.tool
        def tool_c(x: int) -> str:
            return f"C: {x + 10}"

        @server.tool
        async def async_tool_d(x: int) -> str:
            return f"D: {x - 5}"

        # Track server AFTER all tools added (disable report_missing for cleaner counts)
        options = AgentCatOptions(enable_report_missing=False)
        track(server, "test-project", options)

        # Test all tools work correctly
        async with create_community_test_client(server) as client:
            result = await client.call_tool("tool_a", {"x": 15})
            assert "A: 15" in str(result), f"Expected 'A: 15', got {result}"

            result = await client.call_tool("tool_b", {"x": 15})
            assert "B: 30" in str(result), f"Expected 'B: 30', got {result}"

            result = await client.call_tool("tool_c", {"x": 15})
            assert "C: 25" in str(result), f"Expected 'C: 25', got {result}"

            result = await client.call_tool("async_tool_d", {"x": 15})
            assert "D: 10" in str(result), f"Expected 'D: 10', got {result}"

        # Verify all tools are tracked
        data = get_server_tracking_data(get_lowlevel_server(server))
        assert len(data.tool_registry) == 4
        for tool_name in ["tool_a", "tool_b", "tool_c", "async_tool_d"]:
            assert tool_name in data.tool_registry
            assert data.tool_registry[tool_name].tracked

    @pytest.mark.asyncio
    async def test_track_with_options_on_empty_server(self):
        """Test tracking an empty server with various options enabled."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        server = FastMCP("empty-with-options")

        # Track with options on empty server
        options = AgentCatOptions(
            enable_tool_call_context=True,
            enable_report_missing=True,
            enable_tracing=True
        )
        track(server, "test-project", options)

        # Verify tracking is initialized
        data = get_server_tracking_data(get_lowlevel_server(server))
        assert data is not None
        assert data.tracker_initialized

        # get_more_tools should be added due to enable_report_missing
        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()
            tool_names = [tool.name for tool in tools_result]
            assert "get_more_tools" in tool_names

            # Test that get_more_tools works
            result = await client.call_tool(
                "get_more_tools",
                {"context": "Need a tool for testing"}
            )
            assert "Unfortunately" in str(result)

        # Now add a tool and verify context injection works
        @server.tool
        def late_added_tool(value: str) -> str:
            return f"Value: {value}"

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # Find late_added_tool
            late_tool = next(
                (t for t in tools_result if t.name == "late_added_tool"),
                None
            )
            assert late_tool is not None

            # Verify context was injected (due to enable_tool_call_context)
            assert "context" in late_tool.inputSchema["properties"]
            assert "context" in late_tool.inputSchema["required"]

            # Test the tool works with context
            result = await client.call_tool(
                "late_added_tool",
                {"value": "test", "context": "Testing late added tool"}
            )
            assert "Value: test" in str(result)

    @pytest.mark.asyncio
    async def test_multiple_track_calls_on_same_server(self):
        """Test that calling track() multiple times on the same server is safe."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        server = FastMCP("multi-track-server")

        # First track call
        track(server, "project1")

        @server.tool
        def tool1(x: int) -> str:
            return f"Tool1: {x}"

        # Second track call with different project
        track(server, "project2")

        @server.tool
        def tool2(x: int) -> str:
            return f"Tool2: {x * 2}"

        # Third track call with options
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "project3", options)

        @server.tool
        def tool3(x: int) -> str:
            return f"Tool3: {x + 5}"

        # Test all tools work
        async with create_community_test_client(server) as client:
            result = await client.call_tool("tool1", {"x": 10})
            assert "Tool1: 10" in str(result)

            result = await client.call_tool("tool2", {"x": 10})
            assert "Tool2: 20" in str(result)

            result = await client.call_tool(
                "tool3", {"x": 10, "context": "Testing tool3"}
            )
            assert "Tool3: 15" in str(result)

        # Verify all tools are tracked
        data = get_server_tracking_data(get_lowlevel_server(server))
        assert "tool1" in data.tool_registry
        assert "tool2" in data.tool_registry
        assert "tool3" in data.tool_registry

    @pytest.mark.asyncio
    async def test_track_interleaved_with_tool_additions(self):
        """Test complex scenario: add tool, track, add tool, track again, add more."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        server = FastMCP("interleaved-server")

        # Add first tool
        @server.tool
        def step1_tool(x: int) -> str:
            return f"Step1: {x}"

        # First track
        track(server, "test-project")

        # Add second tool
        @server.tool
        def step2_tool(x: int) -> str:
            return f"Step2: {x * 2}"

        # Track again with options
        options = AgentCatOptions(enable_report_missing=True)
        track(server, "test-project", options)

        # Add third and fourth tools
        @server.tool
        def step3_tool(x: int) -> str:
            return f"Step3: {x + 1}"

        @server.tool
        async def step4_tool(x: int) -> str:
            return f"Step4: {x - 1}"

        # Test all tools work and verify functionality
        async with create_community_test_client(server) as client:
            # Test each tool
            result = await client.call_tool("step1_tool", {"x": 100})
            assert "Step1: 100" in str(result)

            result = await client.call_tool("step2_tool", {"x": 100})
            assert "Step2: 200" in str(result)

            result = await client.call_tool("step3_tool", {"x": 100})
            assert "Step3: 101" in str(result)

            result = await client.call_tool("step4_tool", {"x": 100})
            assert "Step4: 99" in str(result)

            # Also verify get_more_tools was added
            result = await client.call_tool(
                "get_more_tools",
                {"context": "Testing report missing"}
            )
            assert "Unfortunately" in str(result)

        # Verify all tools are tracked
        data = get_server_tracking_data(get_lowlevel_server(server))
        for tool_name in ["step1_tool", "step2_tool", "step3_tool", "step4_tool"]:
            assert tool_name in data.tool_registry
            assert data.tool_registry[tool_name].tracked
        assert "get_more_tools" in data.tool_registry


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
