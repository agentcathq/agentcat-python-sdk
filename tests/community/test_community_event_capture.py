"""Test event capture completeness with community FastMCP."""

import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from mcpcat import AgentCatOptions, track
from mcpcat.modules.event_queue import EventQueue, set_event_queue
from mcpcat.modules.internal import (
    get_server_tracking_data,
    set_server_tracking_data,
)
from mcpcat.types import UserIdentity

from ..test_utils.community_client import create_community_test_client
from ..test_utils.community_todo_server import (
    HAS_COMMUNITY_FASTMCP,
    create_community_todo_server,
    get_lowlevel_server,
)

# Skip all tests if community FastMCP is not available
pytestmark = pytest.mark.skipif(
    not HAS_COMMUNITY_FASTMCP,
    reason="Community FastMCP not available. Install with: pip install fastmcp"
)


class TestCommunityEventCapture:
    """Test that all required fields are captured in events with community FastMCP."""

    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Set up and tear down for each test."""
        # Store original event queue
        from mcpcat.modules.event_queue import event_queue as original_queue

        yield
        # Restore original event queue after test
        set_event_queue(original_queue)

    @pytest.mark.asyncio
    async def test_event_contains_all_basic_fields(self):
        """Test that events contain all basic required fields."""
        # Create a mock API client
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        # Create event queue with mock
        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        # Create and track server
        server = create_community_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Make a tool call to generate an event
            await client.call_tool("add_todo", {"text": "Test todo"})

            # Wait for event processing
            time.sleep(1.0)

        # Find the tool call event
        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert len(tool_events) > 0, "No tool call event captured"

        event = tool_events[0]

        # Verify all basic fields are present
        assert event.project_id == "test_project"
        assert event.event_type == "mcp:tools/call"
        assert event.resource_name == "add_todo"
        assert event.timestamp is not None
        assert isinstance(event.timestamp, datetime)
        assert event.duration is not None
        assert isinstance(event.duration, int)
        assert event.parameters is not None
        assert event.parameters.get("arguments") == {"text": "Test todo"}

        # Verify event has its own ID
        assert event.id is not None
        assert event.id.startswith("evt_")
        assert len(event.id) > 10  # Should be a proper KSUID

    @pytest.mark.asyncio
    async def test_event_contains_server_info(self):
        """Test that events capture server name and version."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_community_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            await client.call_tool("add_todo", {"text": "Test"})
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert len(tool_events) > 0

        event = tool_events[0]

        # Server info should be captured
        assert event.server_name == "todo-server"

    @pytest.mark.asyncio
    async def test_event_contains_user_intent_from_context(self):
        """Test that events capture user intent when tool context is enabled."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_community_todo_server()
        options = AgentCatOptions(enable_tracing=True, enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Call tool with context parameter
            await client.call_tool(
                "add_todo",
                {
                    "text": "Buy groceries",
                    "context": "User wants to add a reminder to buy groceries",
                },
            )
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert len(tool_events) > 0

        event = tool_events[0]

        # User intent should be captured from context
        assert event.user_intent == (
            "User wants to add a reminder to buy groceries"
        )

    @pytest.mark.asyncio
    async def test_event_contains_actor_info_after_identify(self):
        """Test that events contain actor information after identify is called."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        def identify_fn(request, context):
            return UserIdentity(
                user_id="user123",
                user_name="John Doe",
                user_data={"email": "john@example.com", "role": "admin"},
            )

        server = create_community_todo_server()
        options = AgentCatOptions(enable_tracing=True, identify=identify_fn)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            await client.call_tool("add_todo", {"text": "Test 1"})
            time.sleep(0.5)
            await client.call_tool("add_todo", {"text": "Test 2"})
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert len(tool_events) >= 2

        for event in tool_events[:2]:
            assert event.identify_actor_given_id == "user123"
            assert event.identify_actor_name == "John Doe"
            assert event.identify_data == {
                "email": "john@example.com",
                "role": "admin",
            }

    @pytest.mark.asyncio
    async def test_multiple_event_types_capture_all_fields(self):
        """Test that different event types all capture required fields."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_community_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Generate various event types
            await client.list_tools()  # mcp:tools/list
            await client.call_tool("add_todo", {"text": "Test"})  # mcp:tools/call
            await client.call_tool("list_todos", {})  # Another tool call
            time.sleep(1.0)

        # Check all captured events
        assert len(captured_events) >= 3

        # Verify each event has all required fields
        for event in captured_events:
            # Basic fields
            assert event.project_id == "test_project"
            assert event.event_type is not None
            assert event.timestamp is not None
            assert event.id is not None
            assert event.id.startswith("evt_")

            # Session info fields
            assert event.server_name == "todo-server"

    @pytest.mark.asyncio
    async def test_event_ids_are_unique(self):
        """Test that each event gets a unique ID."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_community_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Generate multiple events
            for i in range(5):
                await client.call_tool("add_todo", {"text": f"Todo {i}"})
            time.sleep(1.0)

        # Focus on tool call events
        tool_call_events = [
            e for e in captured_events
            if e.event_type == "mcp:tools/call"
        ]

        assert len(tool_call_events) >= 5, (
            f"Expected at least 5 tool call events, got {len(tool_call_events)}"
        )

        # Extract tool call event IDs
        tool_call_ids = [e.id for e in tool_call_events]

        # All tool call IDs should be unique
        assert len(tool_call_ids) == len(set(tool_call_ids)), (
            f"Tool call event IDs are not unique: {tool_call_ids}"
        )

        # All IDs should have proper format
        for event_id in tool_call_ids:
            assert event_id.startswith("evt_")
            assert len(event_id) > 10

    @pytest.mark.asyncio
    async def test_event_duration_is_calculated(self):
        """Test that event duration is properly calculated."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_community_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Add a small delay in the tool to ensure measurable duration
            await client.call_tool("add_todo", {"text": "Test with duration"})
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert len(tool_events) > 0

        event = tool_events[0]

        # Duration should be present and reasonable
        assert event.duration is not None
        assert isinstance(event.duration, int)
        assert event.duration >= 0  # Should be non-negative
        assert event.duration < 10000  # Should be less than 10 seconds

    @pytest.mark.asyncio
    async def test_server_error_capture_in_event(self):
        """Test that errors are captured in the event's is_error and error fields."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_community_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_community_test_client(server) as client:
            # Try to complete a non-existent todo to trigger an error
            try:
                await client.call_tool("complete_todo", {"id": 999})
            except Exception:
                # The client might raise an exception, but we're interested in the event
                pass

            time.sleep(1.0)

        # Find the tool call event for complete_todo
        tool_events = [
            e
            for e in captured_events
            if e.event_type == "mcp:tools/call" and e.resource_name == "complete_todo"
        ]
        assert len(tool_events) > 0, "No complete_todo tool call event captured"

        event = tool_events[0]

        # Verify error fields are populated
        assert event.is_error is True, "Event should be marked as error"
        assert event.error is not None, "Event should have error details"
        assert isinstance(event.error, dict), "Error should be a dictionary"
        assert "message" in event.error, "Error should have a message"
        assert "Todo with ID 999 not found" in event.error["message"], (
            "Error message should contain the ValueError message"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
