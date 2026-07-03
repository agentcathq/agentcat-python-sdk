"""Test tool context functionality with community FastMCP."""

import pytest

from mcpcat import AgentCatOptions, track
from mcpcat.modules.constants import DEFAULT_CONTEXT_DESCRIPTION

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


class TestCommunityToolContext:
    """Test tool context functionality with community FastMCP."""

    @pytest.mark.asyncio
    async def test_context_parameter_injection_enabled(self):
        """Test that context parameter is added when enable_tool_call_context=True."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # Check each tool (except get_more_tools if present)
            for tool in tools_result:
                if tool.name == "get_more_tools":
                    continue

                # Verify context parameter exists
                assert "context" in tool.inputSchema["properties"]

                # Verify context is required
                assert "context" in tool.inputSchema["required"]

                # Verify context schema properties
                context_schema = tool.inputSchema["properties"]["context"]
                assert context_schema["type"] == "string"
                assert (
                    context_schema["description"]
                    == DEFAULT_CONTEXT_DESCRIPTION
                )

    @pytest.mark.asyncio
    async def test_context_parameter_not_injected_when_disabled(self):
        """Test that context parameter is NOT added when enable_tool_call_context=False."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=False)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            for tool in tools_result:
                if tool.name == "get_more_tools":
                    continue

                # Verify context parameter does NOT exist
                assert "context" not in tool.inputSchema.get("properties", {})

                # Verify context is NOT in required
                assert "context" not in tool.inputSchema.get("required", [])

    @pytest.mark.asyncio
    async def test_schema_with_existing_properties(self):
        """Test with tools that have existing inputSchema and properties."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # Find add_todo which has existing schema
            add_todo_tool = next(t for t in tools_result if t.name == "add_todo")

            # Verify original properties still exist
            assert "text" in add_todo_tool.inputSchema["properties"]

            # Verify context was added
            assert "context" in add_todo_tool.inputSchema["properties"]
            assert "context" in add_todo_tool.inputSchema["required"]

    @pytest.mark.asyncio
    async def test_schema_with_no_input_schema(self):
        """Test with tools that have no inputSchema."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        # Create a custom server with a tool that has no parameters
        server = FastMCP("test-server")

        @server.tool
        def simple_tool():
            """A tool with no parameters."""
            return "success"

        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()
            simple_tool_def = next(
                t for t in tools_result if t.name == "simple_tool"
            )

            # Verify inputSchema was created
            assert simple_tool_def.inputSchema is not None
            assert "properties" in simple_tool_def.inputSchema
            assert "context" in simple_tool_def.inputSchema["properties"]
            assert "required" in simple_tool_def.inputSchema
            assert "context" in simple_tool_def.inputSchema["required"]

    @pytest.mark.asyncio
    async def test_schema_with_empty_properties(self):
        """Test with tools that have empty properties object."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        server = FastMCP("test-server")

        # Create a tool with function that has no parameters
        @server.tool
        def empty_tool():
            """Tool with empty schema."""
            return "success"

        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()
            empty_tool = next(t for t in tools_result if t.name == "empty_tool")

            # Verify context was added to empty properties
            assert "context" in empty_tool.inputSchema["properties"]
            assert len(empty_tool.inputSchema["properties"]) >= 1

    @pytest.mark.asyncio
    async def test_schema_with_existing_required_fields(self):
        """Test with tools that already have required fields."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # add_todo has 'text' as required
            add_todo_tool = next(t for t in tools_result if t.name == "add_todo")

            # Verify both original and context are required
            assert "text" in add_todo_tool.inputSchema["required"]
            assert "context" in add_todo_tool.inputSchema["required"]
            assert len(add_todo_tool.inputSchema["required"]) >= 2

    @pytest.mark.asyncio
    async def test_tool_call_with_valid_context(self):
        """Test calling a tool with valid context parameter."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Call tool with context
            result = await client.call_tool(
                "add_todo",
                {
                    "text": "Test todo item",
                    "context": "Adding a test todo to verify context handling",
                },
            )

            # Should succeed
            assert "Added todo" in str(result)

    @pytest.mark.asyncio
    async def test_tool_call_without_context_still_works(self):
        """Test that tool calls without context still work (context is stripped)."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # The implementation strips context before passing to handler
            result = await client.call_tool(
                "add_todo",
                {"text": "Test todo item"},  # Missing context
            )

            # The call should succeed because context is stripped before passing to handler
            assert "Added todo" in str(result)

    @pytest.mark.asyncio
    async def test_tool_call_with_empty_context(self):
        """Test calling a tool with empty string context."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Call with empty context - should still work
            result = await client.call_tool(
                "add_todo",
                {
                    "text": "Test todo",
                    "context": "",  # Empty but present
                },
            )

            assert "Added todo" in str(result)

    @pytest.mark.asyncio
    async def test_get_more_tools_exclusion_with_context(self):
        """Test that get_more_tools doesn't get context when both features are enabled."""
        server = create_community_todo_server()
        options = AgentCatOptions(
            enable_report_missing=True, enable_tool_call_context=True
        )
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # Find get_more_tools tool
            get_more_tools_tool = next(
                t for t in tools_result if t.name == "get_more_tools"
            )

            # Verify it has context parameter (it's special and keeps its own context param)
            assert "context" in get_more_tools_tool.inputSchema.get("properties", {})

            # Verify other tools DO have context
            other_tools = [t for t in tools_result if t.name != "get_more_tools"]
            for tool in other_tools:
                assert "context" in tool.inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_tool_with_existing_context_parameter(self):
        """Test that existing context parameter is overwritten with MCPCat's version."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        server = FastMCP("test-server")

        @server.tool
        def tool_with_context(context: str, data: str):
            """Tool that already has a context parameter."""
            return f"Original context: {context}, data: {data}"

        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()
            tool = next(t for t in tools_result if t.name == "tool_with_context")

            # Verify context exists
            assert "context" in tool.inputSchema["properties"]

            # Check if it has MCPCat's description
            context_schema = tool.inputSchema["properties"]["context"]
            assert (
                context_schema.get("description")
                == DEFAULT_CONTEXT_DESCRIPTION
            )

            # Should still be in required
            assert "context" in tool.inputSchema["required"]

    @pytest.mark.asyncio
    async def test_original_functionality_preserved(self):
        """Verify that original tool functionality remains intact with context."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Add multiple todos
            await client.call_tool(
                "add_todo", {"text": "First todo", "context": "Adding first item"}
            )
            await client.call_tool(
                "add_todo", {"text": "Second todo", "context": "Adding second item"}
            )

            # List todos
            list_result = await client.call_tool(
                "list_todos", {"context": "Listing all todos to verify they were added"}
            )

            # Verify both todos are present
            result_str = str(list_result)
            assert "First todo" in result_str
            assert "Second todo" in result_str

            # Complete a todo
            complete_result = await client.call_tool(
                "complete_todo", {"id": 1, "context": "Completing the first todo"}
            )

            assert "Completed todo" in str(complete_result)

    @pytest.mark.asyncio
    async def test_context_not_passed_to_original_handler(self):
        """Verify that context parameter is stripped before passing to original handler."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Call with context
            result = await client.call_tool(
                "add_todo",
                {"text": "test data", "context": "This context should be stripped"},
            )

            # The call should succeed, proving context was stripped
            # (otherwise it would fail since add_todo doesn't accept context param)
            assert "Added todo" in str(result)

    @pytest.mark.asyncio
    async def test_dynamically_added_tool_gets_context(self):
        """Test that tools added after tracking get context parameter."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        server = FastMCP("test-server")

        # Track with context enabled
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        # Add tool AFTER tracking
        @server.tool
        def late_tool(value: str):
            """Tool added after tracking."""
            return f"Processed: {value}"

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()
            late_tool_def = next(t for t in tools_result if t.name == "late_tool")

            # Should have context parameter
            assert "context" in late_tool_def.inputSchema["properties"]
            assert "context" in late_tool_def.inputSchema["required"]

            # Test calling it
            result = await client.call_tool(
                "late_tool",
                {"value": "test", "context": "Testing late-added tool"}
            )
            assert "Processed: test" in str(result)

    @pytest.mark.asyncio
    async def test_custom_context_description(self):
        """Test that custom context description is correctly applied in community FastMCP."""
        server = create_community_todo_server()
        custom_description = "Explain why you need to use this tool"
        options = AgentCatOptions(
            enable_tool_call_context=True,
            custom_context_description=custom_description
        )
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # Check each tool (except get_more_tools)
            for tool in tools_result:
                if tool.name == "get_more_tools":
                    continue

                # Verify context parameter has custom description
                context_schema = tool.inputSchema["properties"]["context"]
                assert context_schema["description"] == custom_description

    @pytest.mark.asyncio
    async def test_custom_context_description_empty_string(self):
        """Test edge case with empty string custom description in community FastMCP."""
        server = create_community_todo_server()
        options = AgentCatOptions(
            enable_tool_call_context=True,
            custom_context_description=""
        )
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # Find a tool to test
            add_todo_tool = next(t for t in tools_result if t.name == "add_todo")

            # Verify context exists with empty description
            context_schema = add_todo_tool.inputSchema["properties"]["context"]
            assert context_schema["description"] == ""
            assert context_schema["type"] == "string"

    @pytest.mark.asyncio
    async def test_custom_context_description_special_characters(self):
        """Test custom description with special characters and Unicode in community FastMCP."""
        server = create_community_todo_server()
        special_description = "Why use this? 🚀 Include: 'quotes', \"double\", newlines\n, tabs\t."
        options = AgentCatOptions(
            enable_tool_call_context=True,
            custom_context_description=special_description
        )
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # Verify special characters are preserved
            add_todo_tool = next(t for t in tools_result if t.name == "add_todo")
            context_schema = add_todo_tool.inputSchema["properties"]["context"]
            assert context_schema["description"] == special_description

    @pytest.mark.asyncio
    async def test_custom_context_description_very_long(self):
        """Test with a very long description string in community FastMCP."""
        server = create_community_todo_server()
        # Create a very long description
        long_description = "This is a very detailed description for the context. " * 50
        options = AgentCatOptions(
            enable_tool_call_context=True,
            custom_context_description=long_description
        )
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # Verify long description is preserved
            add_todo_tool = next(t for t in tools_result if t.name == "add_todo")
            context_schema = add_todo_tool.inputSchema["properties"]["context"]
            assert context_schema["description"] == long_description
            assert len(context_schema["description"]) > 1000

    @pytest.mark.asyncio
    async def test_default_context_description(self):
        """Verify the default description is used when not specified in community FastMCP."""
        server = create_community_todo_server()
        # Don't specify custom_context_description, should use default
        options = AgentCatOptions(enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # Check for default description
            add_todo_tool = next(t for t in tools_result if t.name == "add_todo")
            context_schema = add_todo_tool.inputSchema["properties"]["context"]
            assert context_schema["description"] == DEFAULT_CONTEXT_DESCRIPTION

    @pytest.mark.asyncio
    async def test_custom_context_description_with_multiple_tools(self):
        """Test that custom description is applied to all tools consistently in community FastMCP."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        server = FastMCP("test-server")

        @server.tool
        def tool1(param: str):
            """First tool."""
            return f"Tool 1: {param}"

        @server.tool
        def tool2(value: int):
            """Second tool."""
            return f"Tool 2: {value}"

        @server.tool
        def tool3():
            """Third tool with no params."""
            return "Tool 3"

        custom_desc = "Custom community context for all tools"
        options = AgentCatOptions(
            enable_tool_call_context=True,
            custom_context_description=custom_desc
        )
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # All tools should have the same custom context description
            for tool in tools_result:
                if tool.name in ["tool1", "tool2", "tool3"]:
                    assert "context" in tool.inputSchema["properties"]
                    assert tool.inputSchema["properties"]["context"]["description"] == custom_desc

    @pytest.mark.asyncio
    async def test_custom_context_with_tool_call(self):
        """Test tool calls work correctly with custom context description in community FastMCP."""
        server = create_community_todo_server()
        custom_desc = "Provide reasoning for this action in the community server"
        options = AgentCatOptions(
            enable_tool_call_context=True,
            custom_context_description=custom_desc
        )
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Verify the custom description is set
            tools_result = await client.list_tools()
            add_todo_tool = next(t for t in tools_result if t.name == "add_todo")
            assert add_todo_tool.inputSchema["properties"]["context"]["description"] == custom_desc

            # Call the tool with context
            result = await client.call_tool(
                "add_todo",
                {
                    "text": "Test with custom description in community",
                    "context": "Adding todo to test custom context description in community FastMCP"
                }
            )

            # Should succeed
            assert "Added todo" in str(result)

    @pytest.mark.asyncio
    async def test_custom_context_with_dynamically_added_tool(self):
        """Test that dynamically added tools get custom context description in community FastMCP."""
        if not HAS_COMMUNITY_FASTMCP:
            pytest.skip("Community FastMCP not available")

        from fastmcp import FastMCP

        server = FastMCP("test-server")

        # Track with custom context description
        custom_desc = "Dynamic tool custom context"
        options = AgentCatOptions(
            enable_tool_call_context=True,
            custom_context_description=custom_desc
        )
        track(server, "test_project", options)

        # Add tool AFTER tracking
        @server.tool
        def dynamic_tool(data: str):
            """Tool added after tracking with custom context."""
            return f"Dynamic result: {data}"

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()
            dynamic_tool_def = next(t for t in tools_result if t.name == "dynamic_tool")

            # Should have context parameter with custom description
            assert "context" in dynamic_tool_def.inputSchema["properties"]
            assert dynamic_tool_def.inputSchema["properties"]["context"]["description"] == custom_desc

            # Test calling it
            result = await client.call_tool(
                "dynamic_tool",
                {"data": "test", "context": "Using dynamic tool with custom context"}
            )
            assert "Dynamic result: test" in str(result)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])