"""Tools registered at any point in a server's life are tracked.

v1 achieved this by monkey-patching FastMCP's ToolManager. v2 intercepts one
level down, at the protocol handlers, and the SDK's own `tools/list` handler
reads the live tool manager — so late registrations are picked up for free and
every assertion here runs through a real client rather than a FastMCP-level
method call.
"""

from typing import Any

import pytest
from mcp import Tool
from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from agentcat import track
from agentcat.modules.internal import get_server_tracking_data, reset_all_tracking_data
from agentcat.types import AgentCatOptions

from .test_utils.client import create_test_client
from .test_utils.delivery import record_delivered_arguments


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


class TestDynamicTracking:
    """Test suite for dynamic tool tracking."""

    @pytest.fixture(autouse=True)
    def setup(self):
        reset_all_tracking_data()
        yield
        reset_all_tracking_data()

    @pytest.fixture
    def fastmcp_server(self):
        return FastMCP("test-server")

    @pytest.fixture
    def lowlevel_server(self):
        return Server("test-server")

    @pytest.mark.asyncio
    async def test_early_registration_is_listed_and_traced(
        self, fastmcp_server, capture
    ):
        """A tool registered before track() works and publishes an event."""

        @fastmcp_server.tool()
        def early_tool(x: int) -> str:
            return str(x)

        track(fastmcp_server, "test-project")

        async with create_test_client(fastmcp_server) as client:
            listed = await client.list_tools()
            assert "early_tool" in [t.name for t in listed.tools]

            result = await client.call_tool("early_tool", {"x": 42})
            # content[0] is AgentCat's task mint-back, which every v2 result
            # carries in front; the tool's own output follows it.
            assert result.content[1].text == "42"

        assert [e.resource_name for e in capture] == ["early_tool"]
        assert get_server_tracking_data(fastmcp_server) is not None

    @pytest.mark.asyncio
    async def test_late_registration_is_listed_and_traced(
        self, fastmcp_server, capture
    ):
        """A tool registered AFTER track() is picked up with no re-tracking."""
        track(fastmcp_server, "test-project")

        @fastmcp_server.tool()
        def late_tool(x: int) -> str:
            return str(x)

        async with create_test_client(fastmcp_server) as client:
            listed = await client.list_tools()
            late = next(t for t in listed.tools if t.name == "late_tool")
            # Late arrivals get the same injection as anything registered early.
            properties = list(late.inputSchema["properties"])
            assert properties == ["x", "session_id", "context"]

            result = await client.call_tool(
                "late_tool", {"x": 123, "context": "late registration"}
            )
            assert result.content[1].text == "123"

        assert [e.resource_name for e in capture] == ["late_tool"]
        assert capture[0].user_intent == "late registration"

    @pytest.mark.asyncio
    async def test_late_registration_without_a_prior_list_still_strips(
        self, fastmcp_server, capture
    ):
        """A call that lands before any tools/list rebuilds the strip registry."""
        track(fastmcp_server, "test-project")

        @fastmcp_server.tool()
        def rebuilt_tool(x: int) -> str:
            return f"Result: {x}"

        # The proof that the rebuild happened has to be read at the manager:
        # `rebuilt_tool` is typed, and an un-stripped `context` would be dropped
        # there silently rather than failing the call.
        seen: list[tuple[str, dict]] = []
        record_delivered_arguments(fastmcp_server._tool_manager, seen)

        async with create_test_client(fastmcp_server) as client:
            # No list_tools first: the registry has to be rebuilt on demand.
            result = await client.call_tool(
                "rebuilt_tool", {"x": 42, "context": "no listing yet"}
            )

        assert result.isError is False
        assert result.content[1].text == "Result: 42"
        assert seen == [("rebuilt_tool", {"x": 42})]
        # The EVENT still carries the raw pre-strip arguments, by design.
        assert capture[0].parameters["arguments"]["context"] == "no listing yet"

    @pytest.mark.asyncio
    async def test_report_missing_tool_answers(self, fastmcp_server, capture):
        """get_more_tools is advertised and answers, without being registered."""
        track(
            fastmcp_server,
            "test-project",
            AgentCatOptions(enable_report_missing=True),
        )

        async with create_test_client(fastmcp_server) as client:
            listed = await client.list_tools()
            assert [t.name for t in listed.tools] == ["get_more_tools"]

            result = await client.call_tool(
                "get_more_tools", {"context": "Need a tool to translate text"}
            )
            text = "".join(c.text for c in result.content if hasattr(c, "text"))
            assert "Unfortunately" in text
            assert "tool list" in text.lower()

        # It never reaches the customer's tool manager, so it cannot collide
        # with a tool they register later.
        registered = [t.name for t in await fastmcp_server.list_tools()]
        assert "get_more_tools" not in registered

    @pytest.mark.asyncio
    async def test_lowlevel_server_tracking(self, lowlevel_server, capture):
        """A bare lowlevel Server gets the same treatment as FastMCP."""

        @lowlevel_server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="lowlevel_tool",
                    description="A low-level tool",
                    inputSchema={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                )
            ]

        @lowlevel_server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[Any]:
            if name == "lowlevel_tool":
                # A lowlevel handler is handed the raw dict, so unlike a typed
                # FastMCP body it CAN police its own arguments — and it must:
                # the tool's declared schema has no `additionalProperties:
                # false`, so the SDK's jsonschema pass accepts extras happily
                # and an un-stripped `agent_id` would slip through unnoticed.
                unexpected = sorted(set(arguments) - {"value"})
                if unexpected:
                    raise ValueError(f"unexpected arguments: {unexpected}")
                return [
                    TextContent(
                        type="text",
                        text=f"Low-level result: {arguments.get('value', 'default')}",
                    )
                ]
            raise ValueError(f"Unknown tool: {name}")

        track(
            lowlevel_server,
            "test-project",
            AgentCatOptions(enable_agent_tracking=True),
        )

        async with create_test_client(lowlevel_server) as client:
            listed = await client.list_tools()
            tool = next(t for t in listed.tools if t.name == "lowlevel_tool")
            assert list(tool.inputSchema["properties"]) == [
                "value",
                "session_id",
                "agent_id",
                "context",
            ]
            # Every injected param is required — session_id included, with
            # `start` as its explicit first-call value.
            assert tool.inputSchema["required"] == [
                "session_id",
                "agent_id",
                "context",
            ]

            # The handler above rejects anything but `value`, so this call
            # only succeeds if both injected parameters were stripped.
            result = await client.call_tool(
                "lowlevel_tool",
                {"value": "test123", "agent_id": "a|b|c", "context": "why"},
            )
            assert result.isError is False, result.content
            assert result.content[1].text == "Low-level result: test123"

        assert capture[0].tags["agentcat_agent_id"] == "a|b|c"
        assert get_server_tracking_data(lowlevel_server) is not None

    @pytest.mark.asyncio
    async def test_multiple_servers_isolation(self, capture):
        """Two tracked servers keep separate tools, options and projects."""
        server1 = FastMCP("server1")
        server2 = FastMCP("server2")

        track(server1, "project1", AgentCatOptions(enable_report_missing=False))
        track(server2, "project2", AgentCatOptions(enable_report_missing=True))

        @server1.tool()
        def server1_tool(x: int) -> str:
            return f"Server1: {x}"

        @server2.tool()
        def server2_tool(x: int) -> str:
            return f"Server2: {x}"

        async with create_test_client(server1) as client:
            assert [t.name for t in (await client.list_tools()).tools] == [
                "server1_tool"
            ]
            result = await client.call_tool("server1_tool", {"x": 10})
            assert result.content[1].text == "Server1: 10"
            assert (await client.call_tool("server2_tool", {"x": 1})).isError is True

        async with create_test_client(server2) as client:
            assert sorted(t.name for t in (await client.list_tools()).tools) == [
                "get_more_tools",
                "server2_tool",
            ]
            result = await client.call_tool("server2_tool", {"x": 20})
            assert result.content[1].text == "Server2: 20"

        assert get_server_tracking_data(server1).project_id == "project1"
        assert get_server_tracking_data(server2).project_id == "project2"
        assert {e.project_id for e in capture} == {"project1", "project2"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
