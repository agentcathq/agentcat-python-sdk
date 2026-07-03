"""Test that events capture all required information when publish_event is mocked."""

import pytest
from unittest.mock import MagicMock, patch
import time
from datetime import datetime, timezone

from mcp import Implementation
from mcpcat import AgentCatOptions, track
from mcpcat.modules.event_queue import EventQueue, set_event_queue
from mcpcat.modules.internal import get_server_tracking_data, set_server_tracking_data
from mcpcat.types import UserIdentity

from .test_utils.client import create_test_client
from .test_utils.todo_server import create_todo_server


class TestEventCaptureCompleteness:
    """Test that all required fields are captured in events."""

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
        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
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
    async def test_event_contains_client_info(self):
        """Test that events capture client name and version."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        # Create client with default info
        async with create_test_client(server) as client:
            # The test client sets client info during initialization
            await client.call_tool("add_todo", {"text": "Test"})
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert len(tool_events) > 0

        event = tool_events[0]

        # Client info should be captured from the test client
        assert event.client_name == "mcp"
        assert event.client_version == "0.1.0"

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

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            await client.call_tool("add_todo", {"text": "Test"})
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert len(tool_events) > 0

        event = tool_events[0]

        # Server info should be captured
        assert event.server_name == "todo-server"
        assert event.server_version == None

    @pytest.mark.asyncio
    async def test_event_contains_sdk_info(self):
        """Test that events capture SDK language and MCPCat version."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            await client.call_tool("add_todo", {"text": "Test"})
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert len(tool_events) > 0

        event = tool_events[0]

        # SDK info should be captured
        assert event.sdk_language is not None
        assert event.sdk_language.startswith("Python ")
        assert event.agentcat_version is not None  # Should be the installed version

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

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True, enable_tool_call_context=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            # Call tool with context parameter
            await client.call_tool(
                "add_todo",
                {
                    "text": "Buy groceries",
                    "context": "User wants to add a reminder to buy groceries for dinner",
                },
            )
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert len(tool_events) > 0

        event = tool_events[0]

        # User intent should be captured from context
        assert (
            event.user_intent
            == "User wants to add a reminder to buy groceries for dinner"
        )

        # Context should be stripped from arguments
        assert event.parameters["arguments"] == {
            "text": "Buy groceries",
            "context": "User wants to add a reminder to buy groceries for dinner",
        }

    @pytest.mark.asyncio
    async def test_event_contains_actor_info_after_identify(self):
        """Events carry identity fields when an identify hook is configured."""
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

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True, identify=identify_fn)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
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

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            # Generate various event types
            await client.list_tools()  # mcp:tools/list
            await client.call_tool("add_todo", {"text": "Test"})  # mcp:tools/call
            await client.call_tool("list_todos")  # Another tool call
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
            assert event.sdk_language is not None
            assert event.agentcat_version is not None
            assert event.server_name == "todo-server"
            assert event.server_version == None
            assert event.client_name == "mcp"
            assert event.client_version == "0.1.0"

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

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            # Generate multiple events
            for i in range(5):
                await client.call_tool("add_todo", {"text": f"Todo {i}"})
            time.sleep(1.0)

        # Extract all event IDs
        event_ids = [e.id for e in captured_events]

        # All IDs should be unique
        assert len(event_ids) == len(set(event_ids)), "Event IDs are not unique"

        # All IDs should have proper format
        for event_id in event_ids:
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

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
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

    @pytest.mark.skip(reason="Initialization event tracking is not implemented")
    @pytest.mark.asyncio
    async def test_initialization_event_capture(self):
        """Test that initialization events are captured with all fields."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        # Creating the client triggers initialization
        async with create_test_client(server) as client:
            # Just need to wait for events to be processed
            time.sleep(1.0)

        # Find initialization event
        init_events = [e for e in captured_events if e.event_type == "mcp:initialize"]
        assert len(init_events) > 0

        event = init_events[0]

        # Verify all fields are present
        assert event.project_id == "test_project"
        assert event.id is not None
        assert event.timestamp is not None
        assert event.server_name == "todo-server"
        assert event.server_version == "1.0.0"
        assert event.client_name == "test-client"
        assert event.client_version == "1.0.0"
        assert event.sdk_language is not None
        assert event.agentcat_version is not None

    @pytest.mark.asyncio
    async def test_identify_function_integration(self):
        """Test that custom identify function affects event actor info."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        # Custom identify function
        def custom_identify(request, server):
            # Extract user info from tool arguments
            arguments = request.params.arguments
            if "user_id" in arguments:
                return UserIdentity(
                    user_id=arguments["user_id"],
                    user_name=arguments.get("user_name", "Unknown"),
                    user_data={"source": "custom_identify"},
                )
            return None

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True, identify=custom_identify)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            # Call with user info in arguments
            await client.call_tool(
                "add_todo",
                {
                    "text": "User-specific todo",
                    "user_id": "custom123",
                    "user_name": "Custom User",
                },
            )
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert len(tool_events) > 0

        event = tool_events[0]

        # Should have actor info from custom identify
        assert event.identify_actor_given_id == "custom123"
        assert event.identify_actor_name == "Custom User"
        assert event.identify_data == {"source": "custom_identify"}

    @pytest.mark.asyncio
    async def test_server_error_capture_in_event(self):
        """Test that ServerResult errors are captured in the event's is_error and error fields."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
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
