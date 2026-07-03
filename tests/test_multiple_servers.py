"""Test multiple server tracking and isolation."""

import asyncio
import pytest
from unittest.mock import MagicMock
import time

from mcpcat import AgentCatOptions, track
from mcpcat.modules.event_queue import EventQueue, set_event_queue

from .test_utils.client import create_test_client
from .test_utils.todo_server import create_todo_server


class TestMultipleServers:
    """Test that multiple servers can be tracked independently."""

    @pytest.mark.asyncio
    async def test_multiple_servers_with_different_options(self):
        """Test that multiple servers can have different tracking options."""
        # Create mock API client to capture events
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)
        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        # Create three independent servers
        server1 = create_todo_server()
        server2 = create_todo_server()
        server3 = create_todo_server()

        # Track each with different options
        options1 = AgentCatOptions(
            enable_tracing=True,
            enable_tool_call_context=True,
            enable_report_missing=True,
        )
        track(server1, "project1", options1)

        options2 = AgentCatOptions(
            enable_tracing=True,
            enable_tool_call_context=False,  # Different from server1
            enable_report_missing=False,  # Different from server1
        )
        track(server2, "project2", options2)

        options3 = AgentCatOptions(
            enable_tracing=False,  # No tracing at all
            enable_tool_call_context=True,
            enable_report_missing=True,
        )
        track(server3, "project3", options3)

        # Test server1: should have context and get_more_tools
        async with create_test_client(server1) as client1:
            tools1 = await client1.list_tools()
            tool_names1 = [t.name for t in tools1.tools]

            # Should have get_more_tools
            assert "get_more_tools" in tool_names1

            # Should have context in tool parameters
            add_todo_tool1 = next(t for t in tools1.tools if t.name == "add_todo")
            assert "context" in add_todo_tool1.inputSchema["properties"]
            assert "context" in add_todo_tool1.inputSchema["required"]

        # Test server2: should NOT have context or get_more_tools
        async with create_test_client(server2) as client2:
            tools2 = await client2.list_tools()
            tool_names2 = [t.name for t in tools2.tools]

            # Should NOT have get_more_tools
            assert "get_more_tools" not in tool_names2

            # Should NOT have context in tool parameters
            add_todo_tool2 = next(t for t in tools2.tools if t.name == "add_todo")
            assert "context" not in add_todo_tool2.inputSchema.get("properties", {})

        # Test server3: should have context and get_more_tools but no tracing
        async with create_test_client(server3) as client3:
            tools3 = await client3.list_tools()
            tool_names3 = [t.name for t in tools3.tools]

            # Should have get_more_tools
            assert "get_more_tools" in tool_names3

            # Should have context in tool parameters
            add_todo_tool3 = next(t for t in tools3.tools if t.name == "add_todo")
            assert "context" in add_todo_tool3.inputSchema["properties"]

        # Clear events before testing
        captured_events.clear()

        # Make tool calls to each server
        async with create_test_client(server1) as client1:
            await client1.call_tool(
                "add_todo", {"text": "Server1 todo", "context": "Testing server1"}
            )

        async with create_test_client(server2) as client2:
            await client2.call_tool(
                "add_todo",
                {
                    "text": "Server2 todo"
                    # No context since it's disabled
                },
            )

        async with create_test_client(server3) as client3:
            await client3.call_tool(
                "add_todo", {"text": "Server3 todo", "context": "Testing server3"}
            )

        # Wait for events to be processed
        time.sleep(1.0)

        # Filter tool call events
        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]

        # Server1 and Server2 should have events (tracing enabled)
        # Server3 should NOT have events (tracing disabled)
        project_ids = [e.project_id for e in tool_events]
        assert "project1" in project_ids
        assert "project2" in project_ids
        assert "project3" not in project_ids

        # Check user_intent extraction
        server1_events = [e for e in tool_events if e.project_id == "project1"]
        server2_events = [e for e in tool_events if e.project_id == "project2"]

        # Server1 should have user_intent (context enabled)
        assert len(server1_events) > 0
        assert server1_events[0].user_intent == "Testing server1"

        # Server2 should NOT have user_intent (context disabled)
        assert len(server2_events) > 0
        assert server2_events[0].user_intent is None

    @pytest.mark.asyncio
    async def test_server_options_update_on_retrack(self):
        """Test that re-tracking a server updates its options."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)
        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server = create_todo_server()

        # First track with context disabled
        options1 = AgentCatOptions(
            enable_tool_call_context=False, enable_report_missing=False
        )
        track(server, "test_project", options1)

        async with create_test_client(server) as client:
            tools = await client.list_tools()
            add_todo = next(t for t in tools.tools if t.name == "add_todo")

            # Should NOT have context
            assert "context" not in add_todo.inputSchema.get("properties", {})

            # Should NOT have get_more_tools
            tool_names = [t.name for t in tools.tools]
            assert "get_more_tools" not in tool_names

        # Re-track with context enabled
        options2 = AgentCatOptions(
            enable_tool_call_context=True, enable_report_missing=True
        )
        track(server, "test_project", options2)

        async with create_test_client(server) as client:
            tools = await client.list_tools()
            add_todo = next(t for t in tools.tools if t.name == "add_todo")

            # Should now have context
            assert "context" in add_todo.inputSchema["properties"]
            assert "context" in add_todo.inputSchema["required"]

            # Should now have get_more_tools
            tool_names = [t.name for t in tools.tools]
            assert "get_more_tools" in tool_names

    @pytest.mark.asyncio
    async def test_concurrent_server_operations(self):
        """Test that multiple servers can handle concurrent operations."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)
        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        # Create multiple servers
        servers = [create_todo_server() for _ in range(5)]

        # Track each with different project IDs
        for i, server in enumerate(servers):
            options = AgentCatOptions(
                enable_tracing=True,
                enable_tool_call_context=(i % 2 == 0),  # Alternate context
                enable_report_missing=(i % 3 == 0),  # Every third has report_missing
            )
            track(server, f"project_{i}", options)

        # Create tasks for concurrent operations
        async def operate_on_server(server, index):
            async with create_test_client(server) as client:
                # Add multiple todos
                for j in range(3):
                    args = {"text": f"Server{index} Todo{j}"}
                    if index % 2 == 0:  # Has context
                        args["context"] = f"Context for server{index}"

                    await client.call_tool("add_todo", args)

                # List todos
                await client.call_tool("list_todos")

                # Complete a todo if any exist
                await client.call_tool("complete_todo", {"id": 1})

        # Run all operations concurrently
        tasks = [operate_on_server(server, i) for i, server in enumerate(servers)]
        await asyncio.gather(*tasks)

        # Wait for events
        time.sleep(1.5)

        # Verify events from all servers
        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]

        # Each server should have 5 tool calls (3 add_todo + 1 list_todos + 1 complete_todo)
        for i in range(5):
            project_events = [e for e in tool_events if e.project_id == f"project_{i}"]
            assert len(project_events) == 5, (
                f"Server {i} should have 5 events, got {len(project_events)}"
            )

            # Check context based on server index
            for event in project_events:
                if event.resource_name == "add_todo":
                    if i % 2 == 0:
                        assert event.user_intent == f"Context for server{i}"
                    else:
                        assert event.user_intent is None

    @pytest.mark.asyncio
    async def test_server_isolation_with_late_tools(self):
        """Test that late-registered tools are tracked per server."""
        server1 = create_todo_server()
        server2 = create_todo_server()

        # Track both servers with different options
        options1 = AgentCatOptions(enable_report_missing=True)
        options2 = AgentCatOptions(enable_report_missing=False)

        track(server1, "project1", options1)
        track(server2, "project2", options2)

        # Add a late tool to server1 only
        @server1.tool()
        def server1_special_tool(message: str) -> str:
            return f"Server1: {message}"

        # Add a different late tool to server2
        @server2.tool()
        def server2_special_tool(data: str) -> str:
            return f"Server2: {data}"

        # Check server1 tools
        async with create_test_client(server1) as client1:
            tools1 = await client1.list_tools()
            tool_names1 = [t.name for t in tools1.tools]

            assert "server1_special_tool" in tool_names1
            assert "server2_special_tool" not in tool_names1
            assert "get_more_tools" in tool_names1  # Has report_missing

        # Check server2 tools
        async with create_test_client(server2) as client2:
            tools2 = await client2.list_tools()
            tool_names2 = [t.name for t in tools2.tools]

            assert "server2_special_tool" in tool_names2
            assert "server1_special_tool" not in tool_names2
            assert "get_more_tools" not in tool_names2  # No report_missing

    @pytest.mark.asyncio
    async def test_custom_identify_per_server(self):
        """Test that custom identify functions work per server."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)
        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        server1 = create_todo_server()
        server2 = create_todo_server()

        # Custom identify for server1
        def identify1(request, server):
            from mcpcat.types import UserIdentity

            if hasattr(request, "params") and hasattr(request.params, "arguments"):
                args = request.params.arguments
                if "user" in args:
                    return UserIdentity(
                        user_id=f"s1_{args['user']}",
                        user_name=f"Server1 User {args['user']}",
                        user_data={"source": "server1"},
                    )
            return None

        # Different custom identify for server2
        def identify2(request, server):
            from mcpcat.types import UserIdentity

            if hasattr(request, "params") and hasattr(request.params, "arguments"):
                args = request.params.arguments
                if "client_id" in args:
                    return UserIdentity(
                        user_id=f"s2_{args['client_id']}",
                        user_name=f"Server2 Client {args['client_id']}",
                        user_data={"source": "server2"},
                    )
            return None

        options1 = AgentCatOptions(enable_tracing=True, identify=identify1)
        options2 = AgentCatOptions(enable_tracing=True, identify=identify2)

        track(server1, "project1", options1)
        track(server2, "project2", options2)

        # Call tools with different identity patterns
        async with create_test_client(server1) as client1:
            await client1.call_tool(
                "add_todo", {"text": "Todo for user", "user": "alice"}
            )

        async with create_test_client(server2) as client2:
            await client2.call_tool(
                "add_todo", {"text": "Todo for client", "client_id": "bob"}
            )

        time.sleep(1.0)

        # Check events have correct identity info
        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]

        server1_events = [e for e in tool_events if e.project_id == "project1"]
        server2_events = [e for e in tool_events if e.project_id == "project2"]

        assert len(server1_events) > 0
        assert server1_events[0].identify_actor_given_id == "s1_alice"
        assert server1_events[0].identify_actor_name == "Server1 User alice"

        assert len(server2_events) > 0
        assert server2_events[0].identify_actor_given_id == "s2_bob"
        assert server2_events[0].identify_actor_name == "Server2 Client bob"


# Cleanup after tests
@pytest.fixture(autouse=True)
def cleanup_event_queue():
    """Reset event queue after each test."""
    yield
    # Reset to default event queue
    from mcpcat.modules.event_queue import EventQueue, set_event_queue

    set_event_queue(EventQueue())
