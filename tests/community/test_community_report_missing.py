"""Test report_missing functionality with community FastMCP."""

import pytest
from unittest.mock import MagicMock
import time

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


class TestCommunityReportMissing:
    """Test report_missing functionality with community FastMCP."""

    @pytest.mark.asyncio
    async def test_report_missing_tool_injection(self):
        """Test that report_missing tool is properly injected when enabled."""
        server = create_community_todo_server()

        # Track the server with report_missing enabled
        options = AgentCatOptions(enable_report_missing=True)
        track(server, "test_project", options)

        # Use client to list all tools and verify report_missing is injected
        async with create_community_test_client(server) as client:
            # List all tools on the server
            tools_result = await client.list_tools()

            # Get tool names
            tool_names = [tool.name for tool in tools_result]

            # Verify original tools are present
            assert "add_todo" in tool_names
            assert "list_todos" in tool_names
            assert "complete_todo" in tool_names

            # Verify report_missing tool was injected
            assert "get_more_tools" in tool_names

    @pytest.mark.asyncio
    async def test_report_missing_tool_schema(self):
        """Test that get_more_tools has context as a required string, not anyOf."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_report_missing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            get_more_tools = next(t for t in tools_result if t.name == "get_more_tools")
            schema = get_more_tools.inputSchema

            # context must be a simple {"type": "string"}, not anyOf/oneOf
            context_prop = schema["properties"]["context"]
            assert context_prop["type"] == "string", (
                f"Expected context type 'string', got: {context_prop}"
            )
            assert "anyOf" not in context_prop, (
                f"context should not use anyOf: {context_prop}"
            )
            assert "default" not in context_prop, (
                f"context should not have a default: {context_prop}"
            )

            # context must be required
            assert "context" in schema.get("required", []), (
                f"context should be required, got required={schema.get('required')}"
            )

    @pytest.mark.asyncio
    async def test_report_missing_disabled_by_default(self):
        """Verify tool is NOT injected when enable_report_missing=False."""
        server = create_community_todo_server()

        # Track with report_missing disabled
        options = AgentCatOptions(enable_report_missing=False)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()
            tool_names = [tool.name for tool in tools_result]

            # Verify report_missing is NOT present
            assert "get_more_tools" not in tool_names
            # But original tools should still be there
            assert "add_todo" in tool_names

    @pytest.mark.asyncio
    async def test_report_missing_tool_call_success(self):
        """Call report_missing tool and verify it executes successfully."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_report_missing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            result = await client.call_tool(
                "get_more_tools",
                {"context": "Need a tool to translate text between languages"},
            )

            # Verify successful response
            result_str = str(result)
            assert "Unfortunately" in result_str

    @pytest.mark.asyncio
    async def test_report_missing_with_valid_params(self):
        """Test with various valid parameters."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_report_missing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Test with different valid parameters
            test_cases = [
                {"context": "database_query"},
                {"context": "send_email"},
                {"context": "generate_chart"},
            ]

            for params in test_cases:
                result = await client.call_tool("get_more_tools", params)
                result_str = str(result)
                assert "Unfortunately" in result_str

    @pytest.mark.asyncio
    async def test_report_missing_with_missing_params(self):
        """Test error handling when required parameters are missing."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_report_missing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Test with missing context - should raise a validation error
            # since context is a required parameter
            with pytest.raises(Exception, match="(?i)required"):
                await client.call_tool("get_more_tools", {})

            # Test with valid context
            result = await client.call_tool("get_more_tools", {"context": "test_tool"})
            result_str = str(result)
            assert "Unfortunately" in result_str

    @pytest.mark.asyncio
    async def test_report_missing_with_other_tools(self):
        """Verify report_missing doesn't interfere with existing server tools."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_report_missing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # First use a regular tool
            add_result = await client.call_tool("add_todo", {"text": "Test todo item"})
            assert "Added todo" in str(add_result)

            # Then use report_missing
            report_result = await client.call_tool(
                "get_more_tools", {"context": "Delete a todo item"}
            )
            assert "Unfortunately" in str(report_result)

            # Verify the original tool still works
            list_result = await client.call_tool("list_todos", {})
            assert "Test todo item" in str(list_result)

    @pytest.mark.asyncio
    async def test_multiple_report_missing_calls(self):
        """Test calling report_missing multiple times in succession."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_report_missing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Call report_missing multiple times
            tools_to_report = [
                ("tool1", "Description 1"),
                ("tool2", "Description 2"),
                ("tool3", "Description 3"),
            ]

            for tool_name, description in tools_to_report:
                result = await client.call_tool(
                    "get_more_tools",
                    {"context": f"{tool_name}: {description}"},
                )
                # Each call should work identically
                result_str = str(result)
                assert "Unfortunately" in result_str

    @pytest.mark.asyncio
    async def test_report_missing_with_context_enabled(self):
        """Test interaction when both report_missing and tool_context are enabled."""
        server = create_community_todo_server()
        options = AgentCatOptions(
            enable_report_missing=True, enable_tool_call_context=True
        )
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            tools_result = await client.list_tools()

            # Find the report_missing tool
            report_missing_tool = None
            other_tool = None
            for tool in tools_result:
                if tool.name == "get_more_tools":
                    report_missing_tool = tool
                elif tool.name == "add_todo":
                    other_tool = tool

            assert report_missing_tool is not None
            assert other_tool is not None

            # Verify get_more_tools has its own context parameter
            assert "context" in report_missing_tool.inputSchema.get("properties", {})

            # Other tools should also have context
            assert "context" in other_tool.inputSchema.get("properties", {})

    @pytest.mark.asyncio
    async def test_report_missing_with_null_values(self):
        """Test with null/None values for parameters."""
        server = create_community_todo_server()
        options = AgentCatOptions(enable_report_missing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Test with None context - should raise a validation error
            # since context is required as a string
            with pytest.raises(Exception, match="(?i)string"):
                await client.call_tool("get_more_tools", {"context": None})

    @pytest.mark.asyncio
    async def test_report_missing_publishes_event(self):
        """Verify that calling report_missing tool publishes an event to the queue."""
        from mcpcat.modules.event_queue import EventQueue, set_event_queue

        # Create a mock API client
        mock_api_client = MagicMock()
        mock_api_client.publish_event = MagicMock(return_value=None)

        # Create a new EventQueue with our mock
        test_queue = EventQueue(api_client=mock_api_client)

        # Replace the global event queue
        set_event_queue(test_queue)

        try:
            server = create_community_todo_server()
            options = AgentCatOptions(enable_report_missing=True, enable_tracing=True)
            track(server, "test_project", options)

            async with create_community_test_client(server) as client:
                # Call the report_missing tool
                await client.call_tool(
                    "get_more_tools",
                    {"context": "Need to resize images to different dimensions"},
                )

                # Give the event queue worker thread time to process
                time.sleep(1.0)

                # Verify that publish_event was called
                assert mock_api_client.publish_event.called
                assert (
                    mock_api_client.publish_event.call_count >= 1
                )  # At least one call

                # Find the tool call event
                tool_call_event = None
                for call in mock_api_client.publish_event.call_args_list:
                    event = call[1]["publish_event_request"]
                    if (
                        event.event_type == "mcp:tools/call"
                        and event.resource_name == "get_more_tools"
                    ):
                        tool_call_event = event
                        break

                assert tool_call_event is not None, (
                    "No get_more_tools tool call event found"
                )

                # Verify event properties
                assert tool_call_event.project_id == "test_project"

                # Verify the arguments contain our input
                assert (
                    tool_call_event.parameters["arguments"]["context"]
                    == "Need to resize images to different dimensions"
                )

                # Verify user_intent was captured from the context parameter
                assert tool_call_event.user_intent == "Need to resize images to different dimensions"

        finally:
            # Clean up: restore original event queue
            from mcpcat.modules.event_queue import EventQueue, set_event_queue

            set_event_queue(EventQueue())

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_publish_multiple_events(self):
        """Verify that multiple tool calls result in multiple events being published."""
        from mcpcat.modules.event_queue import EventQueue, set_event_queue

        # Create a mock API client
        mock_api_client = MagicMock()
        mock_api_client.publish_event = MagicMock(return_value=None)

        # Create a new EventQueue with our mock
        test_queue = EventQueue(api_client=mock_api_client)

        # Replace the global event queue
        set_event_queue(test_queue)

        try:
            server = create_community_todo_server()
            options = AgentCatOptions(enable_report_missing=True, enable_tracing=True)
            track(server, "test_project", options)

            async with create_community_test_client(server) as client:
                # Call report_missing tool
                await client.call_tool(
                    "get_more_tools",
                    {"context": "Need a tool to translate text between languages"},
                )

                # Call a regular tool
                await client.call_tool("add_todo", {"text": "Test todo item"})

                # Call get_more_tools again
                await client.call_tool(
                    "get_more_tools",
                    {"context": "Need a tool to translate text between languages"},
                )

                # Allow time for processing
                time.sleep(1.0)

                # Should have at least 3 tool call events (plus initialize and list_tools events)
                assert mock_api_client.publish_event.call_count >= 3

                # Get all published events
                events = [
                    call[1]["publish_event_request"]
                    for call in mock_api_client.publish_event.call_args_list
                ]

                # Filter to just tool call events
                tool_events = [e for e in events if e.event_type == "mcp:tools/call"]

                # Should have exactly 3 tool calls
                assert len(tool_events) == 3

                # Verify event types and tool names (order not guaranteed due to concurrent processing)
                tool_names = [e.resource_name for e in tool_events]
                assert tool_names.count("get_more_tools") == 2
                assert tool_names.count("add_todo") == 1

                # Find events by resource name for detailed verification
                get_more_tools_events = [
                    e for e in tool_events if e.resource_name == "get_more_tools"
                ]
                add_todo_events = [
                    e for e in tool_events if e.resource_name == "add_todo"
                ]

                # Verify get_more_tools events
                for event in get_more_tools_events:
                    assert (
                        event.parameters["arguments"]["context"]
                        == "Need a tool to translate text between languages"
                    )
                    assert (
                        event.user_intent
                        == "Need a tool to translate text between languages"
                    )

                # Verify add_todo event
                assert len(add_todo_events) == 1
                assert (
                    add_todo_events[0].parameters["arguments"]["text"]
                    == "Test todo item"
                )

        finally:
            # Clean up: restore original event queue
            from mcpcat.modules.event_queue import EventQueue, set_event_queue

            set_event_queue(EventQueue())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])