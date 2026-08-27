"""Context-parameter injection on the official SDK, end to end.

One thing about v2 changes what this file used to assert: every tool now also
receives the `session_id` handle, so the injected property order is
`customer params, session_id, context`.

`context` stays REQUIRED, as it was in 1.x. Nothing server-side rejects a call
that omits it — a schema-validating client refusing to send one is the whole
enforcement mechanism, and without it agents quietly stop supplying intent.
`session_id` is required the same way, with `start` as its explicit first-call
value; an absent value still mints, so a stale schema never errors.
"""

import time
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import DEFAULT_CONTEXT_DESCRIPTION
from agentcat.modules.event_queue import EventQueue, set_event_queue

from .test_utils.client import create_test_client
from .test_utils.delivery import delivered_arguments_for
from .test_utils.todo_server import create_todo_server


async def _tools(server):
    async with create_test_client(server) as client:
        return (await client.list_tools()).tools


def _named(tools, name):
    return next(t for t in tools if t.name == name)


class TestToolContext:
    """Test tool context functionality."""

    @pytest.mark.asyncio
    async def test_context_parameter_injection_enabled(self):
        """Context is added — optional — when enable_tool_call_context=True."""
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        for tool in await _tools(server):
            if tool.name == "get_more_tools":
                continue
            context_schema = tool.inputSchema["properties"]["context"]
            assert context_schema["type"] == "string"
            assert context_schema["description"] == DEFAULT_CONTEXT_DESCRIPTION
            # Required, as in 1.x: a strict client refusing to send a call
            # without it is the only thing that makes agents supply intent.
            assert "context" in tool.inputSchema["required"]
            # ...and so is session_id, whose copy names start as the value
            # that asks to be minted one.
            assert "session_id" in tool.inputSchema["required"]

    @pytest.mark.asyncio
    async def test_context_parameter_not_injected_when_disabled(self):
        """Context is NOT added when enable_tool_call_context=False."""
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=False))

        for tool in await _tools(server):
            if tool.name != "get_more_tools":  # its own context is not ours
                assert "context" not in tool.inputSchema.get("properties", {})
                assert "context" not in tool.inputSchema.get("required", [])
            # Handles are independent of the context parameter.
            assert "session_id" in tool.inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_schema_with_existing_properties(self):
        """Existing properties survive, and the injected ones follow them."""
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        add_todo = _named(await _tools(server), "add_todo")
        properties = list(add_todo.inputSchema["properties"])
        assert properties == ["text", "session_id", "context"]

    @pytest.mark.asyncio
    async def test_schema_with_no_input_schema(self):
        """A parameterless tool still gets a usable schema with context."""
        mcp = FastMCP("test-server")

        @mcp.tool()
        def simple_tool():
            """A tool with no parameters."""
            return "success"

        track(mcp, "test_project", AgentCatOptions(enable_tool_call_context=True))

        simple = _named(await _tools(mcp), "simple_tool")
        assert simple.inputSchema is not None
        assert "context" in simple.inputSchema["properties"]
        assert simple.inputSchema["required"] == ["session_id", "context"]

    @pytest.mark.asyncio
    async def test_schema_with_empty_properties(self):
        """Context lands in an empty properties object."""
        mcp = FastMCP("test-server")

        @mcp.tool()
        def empty_tool():
            """Tool with empty schema."""
            return "success"

        track(mcp, "test_project", AgentCatOptions(enable_tool_call_context=True))

        empty = _named(await _tools(mcp), "empty_tool")
        assert list(empty.inputSchema["properties"]) == ["session_id", "context"]

    @pytest.mark.asyncio
    async def test_schema_with_existing_required_fields(self):
        """A tool's own required fields survive, with context appended."""
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        add_todo = _named(await _tools(server), "add_todo")
        assert add_todo.inputSchema["required"] == ["text", "session_id", "context"]

    @pytest.mark.asyncio
    async def test_schema_with_no_required_fields(self):
        """A tool with no required fields gains one holding only context."""
        mcp = FastMCP("test-server")

        @mcp.tool()
        def optional_params_tool(param1: str = "default"):
            """Tool with optional parameters."""
            return f"Result: {param1}"

        track(mcp, "test_project", AgentCatOptions(enable_tool_call_context=True))

        tool = _named(await _tools(mcp), "optional_params_tool")
        assert tool.inputSchema["required"] == ["session_id", "context"]

        # With the context pass off, the handle pass still requires the
        # session_id it injected — requiredness rides injection exactly.
        untouched = FastMCP("test-server-2")

        @untouched.tool()
        def other_tool(param1: str = "default"):
            """Tool with optional parameters."""
            return f"Result: {param1}"

        track(
            untouched, "test_project", AgentCatOptions(enable_tool_call_context=False)
        )
        listed = _named(await _tools(untouched), "other_tool")
        assert listed.inputSchema.get("required", []) == ["session_id"]

    @pytest.mark.asyncio
    async def test_server_with_no_tools(self):
        """A server with no tools lists only get_more_tools."""
        mcp = FastMCP("empty-server")
        track(mcp, "test_project", AgentCatOptions(enable_tool_call_context=True))

        tools = await _tools(mcp)
        assert [t.name for t in tools] == ["get_more_tools"]

    @pytest.mark.asyncio
    async def test_get_more_tools_exclusion_with_context(self):
        """get_more_tools keeps its own bespoke context, not the injected one."""
        server = create_todo_server()
        track(
            server,
            "test_project",
            AgentCatOptions(enable_report_missing=True, enable_tool_call_context=True),
        )

        tools = await _tools(server)
        get_more_tools = _named(tools, "get_more_tools")
        context_schema = get_more_tools.inputSchema["properties"]["context"]
        assert context_schema["description"] != DEFAULT_CONTEXT_DESCRIPTION
        assert get_more_tools.inputSchema["required"] == ["context", "session_id"]

        for tool in tools:
            if tool.name == "get_more_tools":
                continue
            assert (
                tool.inputSchema["properties"]["context"]["description"]
                == DEFAULT_CONTEXT_DESCRIPTION
            )

    @pytest.mark.asyncio
    async def test_complex_nested_schema(self):
        """Complex nested parameters are preserved alongside the injection."""
        mcp = FastMCP("test-server")

        @mcp.tool()
        def complex_tool(
            user: dict[str, str], settings: dict[str, dict[str, int]], tags: list[str]
        ):
            """Tool with complex nested parameters."""
            return "success"

        track(mcp, "test_project", AgentCatOptions(enable_tool_call_context=True))

        tool = _named(await _tools(mcp), "complex_tool")
        properties = tool.inputSchema["properties"]
        assert {"user", "settings", "tags"} <= set(properties)
        assert "context" in properties

    @pytest.mark.asyncio
    async def test_schema_with_validation_rules(self):
        """Pydantic-derived constraints survive injection."""
        from typing import Annotated

        from pydantic import Field

        mcp = FastMCP("test-server")

        @mcp.tool()
        def validated_tool(age: Annotated[int, Field(ge=0, le=150)], email: str):
            """Tool with validation."""
            return "success"

        track(mcp, "test_project", AgentCatOptions(enable_tool_call_context=True))

        tool = _named(await _tools(mcp), "validated_tool")
        age_schema = tool.inputSchema["properties"]["age"]
        assert age_schema["type"] == "integer"
        assert (
            age_schema.get("minimum") == 0
            or age_schema.get("exclusiveMinimum") == -1
        )
        assert (
            age_schema.get("maximum") == 150
            or age_schema.get("exclusiveMaximum") == 151
        )
        assert "context" in tool.inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_tool_with_existing_context_parameter(self):
        """A tool that already declares `context` keeps its own, untouched."""
        mcp = FastMCP("test-server")

        @mcp.tool()
        def tool_with_context(context: str, data: str):
            """Tool that already has a context parameter."""
            return f"Original context: {context}, data: {data}"

        track(mcp, "test_project", AgentCatOptions(enable_tool_call_context=True))

        tool = _named(await _tools(mcp), "tool_with_context")
        context_schema = tool.inputSchema["properties"]["context"]
        assert context_schema.get("description") != DEFAULT_CONTEXT_DESCRIPTION
        # Still the customer's own required parameter.
        assert "context" in tool.inputSchema["required"]

        # ...and it reaches the tool body, because AgentCat never injected it.
        async with create_test_client(mcp) as client:
            result = await client.call_tool(
                "tool_with_context", {"context": "mine", "data": "d"}
            )
        assert "Original context: mine" in result.content[0].text

    @pytest.mark.asyncio
    async def test_schema_with_allof_anyof_oneof(self):
        """Composition inside a property does not block injection."""
        mcp = FastMCP("test-server")

        @mcp.tool()
        def composed_tool(data: str | int, required_field: str):
            """Tool with schema composition."""
            return f"Data: {data}, Required: {required_field}"

        track(mcp, "test_project", AgentCatOptions(enable_tool_call_context=True))

        tool = _named(await _tools(mcp), "composed_tool")
        data_schema = tool.inputSchema["properties"]["data"]
        assert (
            "anyOf" in data_schema
            or "oneOf" in data_schema
            or data_schema.get("type") == ["string", "integer"]
        )
        assert "context" in tool.inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_top_level_composed_schema_is_skipped(self):
        """A oneOf/allOf/anyOf schema has no single properties bag to extend."""
        from mcp.server.lowlevel import Server
        from mcp.types import Tool

        server = Server("composed-server")

        @server.list_tools()
        async def list_tools():
            return [
                Tool(
                    name="composed_root",
                    description="Top-level composition.",
                    inputSchema={
                        "anyOf": [
                            {"type": "object", "properties": {"a": {"type": "string"}}},
                            {"type": "object", "properties": {"b": {"type": "string"}}},
                        ]
                    },
                )
            ]

        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        tool = _named(await _tools(server), "composed_root")
        assert "properties" not in tool.inputSchema
        assert "anyOf" in tool.inputSchema

    @pytest.mark.asyncio
    async def test_tool_call_with_valid_context(self):
        """Calling a tool with a context argument succeeds."""
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        async with create_test_client(server) as client:
            result = await client.call_tool(
                "add_todo",
                {
                    "text": "Test todo item",
                    "context": "Adding a test todo to verify context handling",
                },
            )

        assert "Added todo" in result.content[0].text

    @pytest.mark.asyncio
    async def test_tool_call_without_context_still_succeeds(self):
        """Context is optional: omitting it must not fail the call."""
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        async with create_test_client(server) as client:
            result = await client.call_tool("add_todo", {"text": "Test todo item"})

        assert "Added todo" in result.content[0].text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "context",
        [
            "",
            None,
            "This is a very long context. " * 100,
            "Testing with emojis 🚀🎉 and special chars: ñáéíóú",
        ],
        ids=["empty", "null", "long", "unicode"],
    )
    async def test_tool_call_with_edge_case_context(self, context):
        """Every context shape is stripped before the tool body sees it.

        Read at the tool manager: `add_todo(text: str)` is a typed body, and
        this SDK's manager drops an undeclared argument silently, so "the call
        succeeded" is not evidence that `context` was removed.
        """
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        async with create_test_client(server) as client:
            result = await client.call_tool(
                "add_todo", {"text": "Test todo", "context": context}
            )

        assert result.isError is False
        assert "Added todo" in result.content[0].text
        assert delivered_arguments_for(server, "add_todo") == [{"text": "Test todo"}]

    @pytest.mark.asyncio
    async def test_original_functionality_preserved(self):
        """A whole tool workflow behaves identically under tracking."""
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        async with create_test_client(server) as client:
            await client.call_tool(
                "add_todo", {"text": "First todo", "context": "Adding first item"}
            )
            await client.call_tool(
                "add_todo", {"text": "Second todo", "context": "Adding second item"}
            )

            list_result = await client.call_tool(
                "list_todos", {"context": "Listing all todos to verify they were added"}
            )
            assert "First todo" in list_result.content[0].text
            assert "Second todo" in list_result.content[0].text

            complete_result = await client.call_tool(
                "complete_todo", {"id": 1, "context": "Completing the first todo"}
            )
            assert "Completed todo" in complete_result.content[0].text

    @pytest.mark.asyncio
    async def test_context_not_passed_to_original_handler(self):
        """`context` is gone from what the tool layer is handed.

        A successful call proves nothing on its own — the tool manager drops
        arguments `add_todo` never declared without raising — so the evidence
        is the argument dict the manager itself received.
        """
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        async with create_test_client(server) as client:
            result = await client.call_tool(
                "add_todo",
                {"text": "test data", "context": "This context should be stripped"},
            )

        assert result.isError is False
        assert "Added todo" in result.content[0].text
        assert delivered_arguments_for(server, "add_todo") == [{"text": "test data"}]

    @pytest.mark.asyncio
    async def test_multiple_track_calls(self):
        """The most recent track() call's options are the ones in force."""
        server = create_todo_server()
        track(server, "project1", AgentCatOptions(enable_tool_call_context=False))
        track(server, "project2", AgentCatOptions(enable_tool_call_context=True))

        for tool in await _tools(server):
            if tool.name != "get_more_tools":
                assert "context" in tool.inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_changing_options_between_calls(self):
        """Re-tracking with context off removes it — v1 could not undo an injection."""
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))
        assert "context" in _named(await _tools(server), "add_todo").inputSchema[
            "properties"
        ]

        track(server, "test_project", AgentCatOptions(enable_tool_call_context=False))
        assert (
            "context"
            not in _named(await _tools(server), "add_todo").inputSchema["properties"]
        )

    @pytest.mark.asyncio
    async def test_error_handling_graceful_fallback(self):
        """Tools remain listable and callable regardless."""
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        async with create_test_client(server) as client:
            tools_result = await client.list_tools()
            assert len(tools_result.tools) > 0
            result = await client.call_tool("list_todos", {"context": "Listing todos"})
            assert result.content

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "description",
        [
            "Explain your reasoning for using this tool",
            "",
            "Why are you using this? 🤔 Include: quotes\"', newlines\n, tabs\t, etc.",
            "This is a very detailed description. " * 50,
        ],
        ids=["custom", "empty", "special-characters", "very-long"],
    )
    async def test_custom_context_description(self, description):
        """Whatever the customer sets is what the agent sees, verbatim."""
        server = create_todo_server()
        track(
            server,
            "test_project",
            AgentCatOptions(
                enable_tool_call_context=True, custom_context_description=description
            ),
        )

        for tool in await _tools(server):
            if tool.name == "get_more_tools":
                continue
            context_schema = tool.inputSchema["properties"]["context"]
            assert context_schema["description"] == description

    @pytest.mark.asyncio
    async def test_default_context_description(self):
        """The default description is used when none is specified."""
        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tool_call_context=True))

        add_todo = _named(await _tools(server), "add_todo")
        assert (
            add_todo.inputSchema["properties"]["context"]["description"]
            == DEFAULT_CONTEXT_DESCRIPTION
        )

    @pytest.mark.asyncio
    async def test_custom_context_description_with_multiple_tools(self):
        """One description, applied consistently across every tool."""
        mcp = FastMCP("test-server")

        @mcp.tool()
        def tool1(param: str):
            """First tool."""
            return f"Tool 1: {param}"

        @mcp.tool()
        def tool2(value: int):
            """Second tool."""
            return f"Tool 2: {value}"

        @mcp.tool()
        def tool3():
            """Third tool with no params."""
            return "Tool 3"

        custom_desc = "Custom context for all tools"
        track(
            mcp,
            "test_project",
            AgentCatOptions(
                enable_tool_call_context=True, custom_context_description=custom_desc
            ),
        )

        for tool in await _tools(mcp):
            if tool.name in ("tool1", "tool2", "tool3"):
                assert (
                    tool.inputSchema["properties"]["context"]["description"]
                    == custom_desc
                )

    @pytest.mark.asyncio
    async def test_custom_context_description_change_between_tracks(self):
        """The latest track() wins outright."""
        server = create_todo_server()
        track(
            server,
            "test_project",
            AgentCatOptions(
                enable_tool_call_context=True,
                custom_context_description="First description",
            ),
        )
        track(
            server,
            "test_project",
            AgentCatOptions(
                enable_tool_call_context=True,
                custom_context_description="Second description",
            ),
        )

        add_todo = _named(await _tools(server), "add_todo")
        assert (
            add_todo.inputSchema["properties"]["context"]["description"]
            == "Second description"
        )

    @pytest.mark.asyncio
    async def test_custom_context_with_tool_call(self):
        """A custom description does not change call behavior."""
        custom_desc = "Provide detailed reasoning for this action"
        server = create_todo_server()
        track(
            server,
            "test_project",
            AgentCatOptions(
                enable_tool_call_context=True, custom_context_description=custom_desc
            ),
        )

        async with create_test_client(server) as client:
            tools_result = await client.list_tools()
            add_todo = _named(tools_result.tools, "add_todo")
            assert (
                add_todo.inputSchema["properties"]["context"]["description"]
                == custom_desc
            )

            result = await client.call_tool(
                "add_todo",
                {
                    "text": "Test with custom description",
                    "context": "Adding todo to test custom context description feature",
                },
            )
            assert "Added todo" in result.content[0].text


class TestGetMoreToolsContextSchema:
    """Test that get_more_tools has a proper context parameter schema."""

    @pytest.fixture
    async def get_more_tools_def(self):
        server = create_todo_server()
        track(
            server,
            "test_project",
            AgentCatOptions(enable_report_missing=True, enable_tool_call_context=True),
        )
        return _named(await _tools(server), "get_more_tools")

    @pytest.mark.asyncio
    async def test_get_more_tools_context_has_string_type(self, get_more_tools_def):
        """It should be a simple string, not a union type."""
        context_schema = get_more_tools_def.inputSchema["properties"]["context"]
        assert "anyOf" not in context_schema, context_schema
        assert context_schema.get("type") == "string", context_schema

    @pytest.mark.asyncio
    async def test_get_more_tools_context_has_description(self, get_more_tools_def):
        """It should carry a meaningful description."""
        context_schema = get_more_tools_def.inputSchema["properties"]["context"]
        assert len(context_schema["description"]) > 10

    @pytest.mark.asyncio
    async def test_get_more_tools_context_is_required(self, get_more_tools_def):
        """Its bespoke context is a real parameter, so it stays required."""
        assert "context" in get_more_tools_def.inputSchema.get("required", [])


class TestUserIntentCaptureInEvents:
    """user_intent is captured from the context argument on published events."""

    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        from agentcat.modules.event_queue import event_queue as original_queue

        yield
        set_event_queue(original_queue)

    @pytest.mark.asyncio
    async def test_get_more_tools_captures_user_intent_in_event(self):
        mock_api_client = MagicMock()
        captured_events = []
        mock_api_client.publish_event = MagicMock(
            side_effect=lambda publish_event_request, **kwargs: captured_events.append(
                publish_event_request
            )
        )
        set_event_queue(EventQueue(api_client=mock_api_client))

        server = create_todo_server()
        track(
            server,
            "test_project",
            AgentCatOptions(
                enable_tracing=True,
                enable_report_missing=True,
                enable_tool_call_context=True,
            ),
        )

        async with create_test_client(server) as client:
            await client.call_tool(
                "get_more_tools", {"context": "I need a tool to send emails"}
            )
            time.sleep(1.0)

        tool_events = [
            e
            for e in captured_events
            if e.event_type == "mcp:tools/call"
            and e.resource_name == "get_more_tools"
        ]
        assert len(tool_events) == 1
        assert tool_events[0].user_intent == "I need a tool to send emails"
