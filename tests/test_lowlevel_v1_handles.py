"""End-to-end handle behavior on the official MCP SDK 1.x lowlevel adapter.

Everything here drives a real `ClientSession` against a real server, so the
assertions cover the whole wire path: schema injection on `tools/list`, the
stripped arguments the customer's tool actually receives, the mint-back the
agent sees, and the single `mcp:tools/call` event AgentCat publishes.

The strip is read at the TOOL MANAGER (`tests.test_utils.delivery`), never
inside a tool body: this SDK's manager drops an argument the signature does not
name without complaint, so a body-level recorder would report the same thing
with the strip disabled.

`create_todo_server()` returns an official `FastMCP`, which v2 tracks through
its `_mcp_server` — the same adapter a bare lowlevel `Server` gets.
"""

import json

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import (
    AGENTCAT_TAG_AGENT_ID,
    AGENTCAT_TAG_AGENT_SOURCE,
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_INSTRUCTIONS_KEY,
)
from agentcat.modules.handles import derive_session_id

from .test_utils import NEEDS_STRUCTURED_OUTPUT, read_only_hint, sid
from .test_utils.client import create_test_client
from .test_utils.delivery import delivered_arguments_for, record_delivered_arguments
from .test_utils.todo_server import create_todo_server

MINT_BACK_HEADER = "[MCP INSTRUCTIONS]: session_id issued."


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


@pytest.fixture
def log_sink():
    """Everything `write_to_log` tees to diagnostics, as the collector sees it."""
    from agentcat.modules import logging as agentcat_logging

    previous = agentcat_logging._diagnostics_sink
    lines: list[str] = []
    agentcat_logging.set_diagnostics_sink(lines.append)
    yield lines
    agentcat_logging.set_diagnostics_sink(previous)


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


def _call_events(capture) -> list:
    return [e for e in capture if e.event_type == "mcp:tools/call"]


async def test_prompted_mode_end_to_end(capture):
    server = create_todo_server()
    track(server, "proj_test", AgentCatOptions())

    async with create_test_client(server) as client:
        listed = await client.list_tools()
        add = next(t for t in listed.tools if t.name == "add_todo")
        assert list(add.inputSchema["properties"])[-2:] == ["session_id", "context"]
        assert "session_id" not in add.inputSchema.get("required", [])
        # session_id is the one injected param that is never required — omitting
        # it is the minting signal. `context` is required, which is the only
        # thing that makes agents supply intent at all.
        assert "context" in add.inputSchema["required"]
        assert any(t.name == "get_more_tools" for t in listed.tools)

        r1 = await client.call_tool(
            "add_todo",
            {
                "text": "hi",
                "context": "Adding a todo item for the user's task list to track work",
            },
        )
        text = _text(r1)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id=")[1].split(" ")[0]
        assert minted.startswith("ses_")

        r2 = await client.call_tool("add_todo", {"text": "again", "session_id": minted})
        assert MINT_BACK_HEADER not in _text(r2)

    call_events = _call_events(capture)
    # v2 publishes tools/call and nothing else: no initialize, no tools/list,
    # no agentcat:identify.
    assert {e.event_type for e in capture} == {"mcp:tools/call"}
    assert len(call_events) == 2
    assert call_events[0].session_id == minted == call_events[1].session_id
    # The event records the call as the agent made it: raw, unstripped.
    assert call_events[0].parameters["arguments"]["context"]
    assert call_events[1].parameters["arguments"]["session_id"] == minted
    # ...and the customer's result, undecorated.
    assert call_events[0].response is not None
    assert "Added todo" in json.dumps(call_events[0].response)
    assert "[MCP INSTRUCTIONS]" not in json.dumps(call_events[0].response)
    assert call_events[0].tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"
    assert call_events[1].tags[AGENTCAT_TAG_SESSION_SOURCE] == "supplied"
    assert call_events[0].user_intent.startswith("Adding a todo item")


@NEEDS_STRUCTURED_OUTPUT
async def test_structured_mint_back_mirrors_into_structured_content(capture):
    """A tool with an outputSchema gets `_mcp_instructions` mirrored in, and its
    schema declares the field so schema-validating clients still accept it."""
    server = create_todo_server()
    track(server, "proj_test", AgentCatOptions())

    async with create_test_client(server) as client:
        listed = await client.list_tools()
        add = next(t for t in listed.tools if t.name == "add_todo")
        assert MCP_INSTRUCTIONS_KEY in add.outputSchema["properties"]

        result = await client.call_tool("add_todo", {"text": "structured"})
        mint = result.structuredContent[MCP_INSTRUCTIONS_KEY]
        assert mint["session_id"].startswith("ses_")
        assert mint["session_id"] == _call_events(capture)[0].session_id
        # The customer's own structured payload survives untouched.
        assert result.structuredContent["result"].startswith("Added todo")


async def test_handler_sees_stripped_args_and_customer_result_untouched(capture):
    """The injected params never reach the tool body, and what the tool returned
    is exactly what the agent gets back (minus AgentCat's trailing block).

    `seen` is filled at the tool manager, not inside `probe`. A typed body can
    only ever report the parameters it declared, and this manager drops an
    undeclared argument SILENTLY rather than raising — so a recorder in the
    body would read `{"text": "payload"}` whether or not the strip ran. See
    `tests.test_utils.delivery`.
    """
    seen: list[tuple[str, dict]] = []
    mcp = FastMCP("probe-server")

    @mcp.tool()
    def probe(text: str) -> str:
        """A plain typed tool; it cannot police its own arguments."""
        return f"probe:{text}"

    record_delivered_arguments(mcp._tool_manager, seen)
    track(mcp, "proj_test")

    async with create_test_client(mcp) as client:
        await client.list_tools()
        result = await client.call_tool(
            "probe",
            {"text": "payload", "session_id": sid("supplied"), "context": "why"},
        )

    assert result.isError is False, _text(result)
    assert seen == [("probe", {"text": "payload"})]
    assert result.content[0].text == "probe:payload"
    # session_id was supplied, so nothing is minted back and nothing is appended.
    assert len(result.content) == 1
    assert _call_events(capture)[0].session_id == sid("supplied")


async def test_get_more_tools_keeps_its_own_context_and_publishes(capture):
    server = create_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_test_client(server) as client:
        listed = await client.list_tools()
        gmt = next(t for t in listed.tools if t.name == "get_more_tools")
        # Its bespoke `context` is a real parameter: still required, still
        # described by the tool's own copy — and handles ride alongside.
        assert gmt.inputSchema["required"] == ["context"]
        assert "session_id" in gmt.inputSchema["properties"]
        assert read_only_hint(gmt) is True

        result = await client.call_tool(
            "get_more_tools", {"context": "I need a tool to send emails"}
        )
        assert "Unfortunately" in _text(result)

    event = _call_events(capture)[0]
    assert event.resource_name == "get_more_tools"
    assert event.user_intent == "I need a tool to send emails"
    assert event.parameters["arguments"]["context"] == "I need a tool to send emails"


async def test_agent_tracking_injection(capture):
    server = create_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_agent_tracking=True))

    async with create_test_client(server) as client:
        listed = await client.list_tools()
        add = next(t for t in listed.tools if t.name == "add_todo")
        assert list(add.inputSchema["properties"])[-3:] == [
            "session_id",
            "agent_id",
            "context",
        ]
        assert "agent_id" in add.inputSchema["required"]
        assert "session_id" not in add.inputSchema["required"]

        result = await client.call_tool(
            "add_todo", {"text": "with agent", "agent_id": "opus|claude-code|k3n9x"}
        )
        assert result.isError is False, _text(result)

    event = _call_events(capture)[0]
    assert event.tags[AGENTCAT_TAG_AGENT_ID] == "opus|claude-code|k3n9x"
    assert event.tags[AGENTCAT_TAG_AGENT_SOURCE] == "supplied"


async def test_hook_mode(capture):
    server = create_todo_server()
    track(
        server,
        "proj_test",
        AgentCatOptions(resolve_session_id=lambda request, extra: "cust-1"),
    )

    async with create_test_client(server) as client:
        listed = await client.list_tools()
        assert listed.tools
        for tool in listed.tools:
            assert "session_id" not in tool.inputSchema.get("properties", {})

        r1 = await client.call_tool("add_todo", {"text": "hook one"})
        r2 = await client.call_tool("add_todo", {"text": "hook two"})
        assert "[MCP INSTRUCTIONS]" not in _text(r1)
        assert "[MCP INSTRUCTIONS]" not in _text(r2)
        # `getattr`, not attribute access: `structuredContent` is a real field
        # from mcp 1.10 and an unset extra before it, and pydantic raises
        # AttributeError for an extra that was never assigned. The claim here
        # is absence either way.
        assert MCP_INSTRUCTIONS_KEY not in (
            getattr(r1, "structuredContent", None) or {}
        )

    call_events = _call_events(capture)
    expected = derive_session_id("cust-1", "proj_test")
    assert [e.session_id for e in call_events] == [expected, expected]
    assert call_events[0].tags[AGENTCAT_TAG_SESSION_SOURCE] == "hook"


async def test_tracing_disabled_strips_but_publishes_nothing(capture):
    server = create_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_tracing=False))

    async with create_test_client(server) as client:
        listed = await client.list_tools()
        add = next(t for t in listed.tools if t.name == "add_todo")
        # No handles when tracing is off, but the context parameter is
        # independent — so it must still be stripped before the tool runs.
        assert "session_id" not in add.inputSchema["properties"]
        assert "context" in add.inputSchema["properties"]

        result = await client.call_tool(
            "add_todo", {"text": "quiet", "context": "no tracing"}
        )
        assert result.isError is False, _text(result)
        assert "[MCP INSTRUCTIONS]" not in _text(result)

    # Read at the tool manager: `add_todo` is a typed body, which cannot show
    # an argument that arrived and was dropped on the way in.
    assert delivered_arguments_for(server, "add_todo") == [{"text": "quiet"}]
    assert capture == []


async def test_customer_tool_schema_is_never_mutated(capture):
    """Injection works on deep copies: the server's own tool definitions and a
    second, untracked server built the same way stay identical."""
    server = create_todo_server()
    reference = create_todo_server()
    track(server, "proj_test")

    async with create_test_client(server) as client:
        await client.list_tools()

    tracked = {t.name: t.inputSchema for t in await server.list_tools()}
    untracked = {t.name: t.inputSchema for t in await reference.list_tools()}
    assert tracked == untracked


async def test_error_result_still_carries_the_handle(capture):
    server = create_todo_server()
    track(server, "proj_test")

    async with create_test_client(server) as client:
        await client.list_tools()
        result = await client.call_tool("complete_todo", {"id": 999})

    assert result.isError is True
    # A retry after a failure has to carry the same task, so error results
    # decorate on identical terms.
    assert MINT_BACK_HEADER in _text(result)
    event = _call_events(capture)[0]
    assert event.is_error is True
    assert event.session_id.startswith("ses_")
    # The SDK flattened the exception before this wrapper saw it, so the type
    # comes from the inner tap (Task 13.5) rather than from the result — and
    # the customer's own ValueError is the recorded cause.
    assert "Todo with ID 999 not found" in event.error["message"]
    assert event.error["type"] == "ToolError"
    assert event.error["chained_errors"][0]["type"] == "ValueError"
    assert event.error["platform"] == "python"


async def test_customer_get_more_tools_is_never_hijacked(capture):
    """A customer tool that happens to be named `get_more_tools` keeps running.

    We refuse to advertise a second one; we must equally refuse to answer for
    the one they wrote (spec §12 — nothing may alter customer tool behavior).
    """
    mcp = FastMCP("collision-server")

    @mcp.tool()
    def get_more_tools(context: str) -> str:
        """The customer's own tool, which happens to share our name."""
        return f"customer answered: {context}"

    track(mcp, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_test_client(mcp) as client:
        listed = await client.list_tools()
        # Exactly one, and it is theirs — ours is not advertised alongside it.
        assert [t.name for t in listed.tools] == ["get_more_tools"]
        assert listed.tools[0].description.startswith("The customer's own tool")

        result = await client.call_tool("get_more_tools", {"context": "mine"})

    assert result.isError is False, _text(result)
    assert "customer answered: mine" in _text(result)
    assert "Unfortunately" not in _text(result)
    # Still tracked like any other tool call.
    assert _call_events(capture)[0].resource_name == "get_more_tools"


async def test_customer_get_more_tools_survives_a_call_before_any_listing(capture):
    """The ownership question is settled by the same rebuild that restores the
    strip registry, so a first-ever call is not hijacked either."""
    mcp = FastMCP("collision-server")

    @mcp.tool()
    def get_more_tools(context: str) -> str:
        """The customer's own tool."""
        return f"customer answered: {context}"

    track(mcp, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_test_client(mcp) as client:
        # No list_tools first.
        result = await client.call_tool("get_more_tools", {"context": "mine"})

    assert "customer answered: mine" in _text(result)


async def test_customer_get_more_tools_survives_a_server_with_no_listing(capture):
    """Nothing advertised the tool, so we may not answer for it.

    A server that exposes no `tools/list` at all never runs the pass that would
    tell us who owns the name. The gate is stated positively — "AgentCat
    advertised it" — so an absent listing keeps the customer's handler.
    """
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolRequest, CallToolRequestParams

    server = Server("no-listing-server")

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        return [TextContent(type="text", text=f"customer answered: {name}")]

    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))
    handler = server.request_handlers[CallToolRequest]
    result = await handler(
        CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="get_more_tools", arguments={"context": "mine"}
            ),
        )
    )

    assert "customer answered: get_more_tools" in _text(result.root)
    assert "Unfortunately" not in _text(result.root)


async def test_retracking_updates_options_without_double_wrapping(capture):
    server = create_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_report_missing=False))
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_test_client(server) as client:
        listed = await client.list_tools()
        assert [t.name for t in listed.tools].count("get_more_tools") == 1
        add = next(t for t in listed.tools if t.name == "add_todo")
        assert list(add.inputSchema["properties"]) == ["text", "session_id", "context"]

        # A second, stacked wrapper would inject session_id in the inner pass and
        # then skip it in the outer one — leaving it out of the strip registry
        # and handing the customer's tool a parameter it never declared.
        result = await client.call_tool(
            "add_todo", {"text": "retracked", "session_id": sid("retrack")}
        )
        assert result.isError is False, _text(result)

    assert len(_call_events(capture)) == 1
    assert _call_events(capture)[0].session_id == sid("retrack")


async def test_request_arguments_are_never_mutated_in_place():
    """The v1 path popped `context` off the caller's own arguments dict, which
    corrupted concurrent retries. Stripping must always clone first."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    server = create_todo_server()
    track(server, "proj_test")
    handler = server._mcp_server.request_handlers[CallToolRequest]

    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="add_todo",
            arguments={"text": "hi", "context": "why", "session_id": sid("supplied")},
        ),
    )
    stored = request.params.arguments
    result = await handler(request)

    assert result.root.isError is False, result.root.content
    # The very dict the request holds, still untouched — the handler ran against
    # a clone.
    assert request.params.arguments is stored
    assert stored == {"text": "hi", "context": "why", "session_id": sid("supplied")}


def test_a_tracked_server_is_collectable_once_the_customer_drops_it():
    """Nothing AgentCat holds may outlive the server (changelog §6.8).

    A module-level `WeakKeyDictionary` does not give you this for free: the
    wrappers filed there close over the server, so the value keeps its own weak
    key alive and every tracked server becomes immortal.
    """
    import gc
    import weakref

    from mcp.server.lowlevel import Server

    alive = []
    for _ in range(3):
        server = Server("collectable")
        _register_todo_handlers(server)
        track(server, "proj_test")
        alive.append(weakref.ref(server))
        del server

    gc.collect()
    assert [ref() for ref in alive] == [None, None, None]


def test_a_tracked_fastmcp_is_collectable_with_its_lowlevel_server():
    """The adapter is installed on `_mcp_server`, so both halves have to go."""
    import gc
    import weakref

    server = create_todo_server()
    track(server, "proj_test")
    facade, lowlevel = weakref.ref(server), weakref.ref(server._mcp_server)
    del server

    gc.collect()
    assert (facade(), lowlevel()) == (None, None)


def _register_todo_handlers(server) -> None:
    """The customer's own `@server.list_tools()` / `@server.call_tool()` pair."""
    from mcp.types import Tool

    @server.list_tools()
    async def list_tools() -> list:
        return [
            Tool(
                name="add_todo",
                description="Add a new todo item.",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        unexpected = sorted(set(arguments) - {"text"})
        if unexpected:
            raise ValueError(f"unexpected arguments: {unexpected}")
        return [TextContent(type="text", text=f"Added todo: {arguments['text']}")]


async def test_handlers_registered_after_track_are_still_wrapped(capture):
    """`track()` on a server with no tools handlers yet still takes effect.

    Registration on this generation writes straight into `request_handlers`, so
    without a patched decorator the customer's later `@server.call_tool()`
    would simply overwrite our wrapper — a fresh `Server()` tracked at import
    time would go untracked forever.
    """
    from mcp.server.lowlevel import Server

    server = Server("late-server")
    track(server, "proj_test")
    _register_todo_handlers(server)

    async with create_test_client(server) as client:
        listed = await client.list_tools()
        add = next(t for t in listed.tools if t.name == "add_todo")
        assert "session_id" in add.inputSchema["properties"]
        result = await client.call_tool("add_todo", {"text": "late"})
        assert result.isError is False, _text(result)
        assert MINT_BACK_HEADER in _text(result)

    assert len(_call_events(capture)) == 1


async def test_reregistering_a_handler_after_track_rewraps_it(capture):
    """A customer who replaces their `call_tool` handler after `track()` does
    not silently lose tracking."""
    from mcp.server.lowlevel import Server

    server = Server("replaced-server")
    _register_todo_handlers(server)
    track(server, "proj_test")

    @server.call_tool()
    async def replacement(name: str, arguments: dict):
        unexpected = sorted(set(arguments) - {"text"})
        if unexpected:
            raise ValueError(f"unexpected arguments: {unexpected}")
        return [TextContent(type="text", text=f"replaced: {arguments['text']}")]

    async with create_test_client(server) as client:
        await client.list_tools()
        result = await client.call_tool("add_todo", {"text": "again"})
        assert result.isError is False, _text(result)
        assert "replaced: again" in _text(result)
        assert MINT_BACK_HEADER in _text(result)

    assert len(_call_events(capture)) == 1


async def test_re_arm_is_not_stacked_by_a_second_track(capture):
    """Two `track()` calls leave one wrapper, not two, even on a handler
    registered after both."""
    from mcp.server.lowlevel import Server

    server = Server("double-tracked")
    track(server, "proj_test")
    track(server, "proj_test")
    _register_todo_handlers(server)

    async with create_test_client(server) as client:
        listed = await client.list_tools()
        add = next(t for t in listed.tools if t.name == "add_todo")
        # A stacked pair would inject session_id twice or leave it unstrippable.
        assert list(add.inputSchema["properties"]) == ["text", "session_id", "context"]
        result = await client.call_tool("add_todo", {"text": "twice"})
        assert result.isError is False, _text(result)

    assert len(_call_events(capture)) == 1


async def test_intermediate_mrtr_round_is_tagged_but_never_decorated(capture):
    """A round that asks the client for more input is not the completing round,
    so it carries no mint-back — text or structured.

    The round is registered straight into `request_handlers` rather than
    through `@server.call_tool()`: returning a `CallToolResult` from the
    decorated function is only honored from mcp 1.19 (PR #1459), and below it
    the SDK wraps the model itself as content and fails its own validation.
    `request_handlers` takes a `ServerResult` on every 1.x, and `resultType`
    rides along as an extra field because `CallToolResult` is extra="allow".
    """
    from mcp.server.lowlevel import Server
    from mcp.types import (
        CallToolRequest,
        CallToolRequestParams,
        CallToolResult,
        ServerResult,
        Tool,
    )

    server = Server("mrtr-server")

    @server.list_tools()
    async def list_tools() -> list:
        return [
            Tool(
                name="ask",
                description="Needs another round.",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    async def call_tool(req):
        return ServerResult(
            CallToolResult(
                content=[TextContent(type="text", text="need more")],
                resultType="input_required",
            )
        )

    server.request_handlers[CallToolRequest] = call_tool

    track(server, "proj_test")
    handler = server.request_handlers[CallToolRequest]
    result = await handler(
        CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name="ask", arguments={}),
        )
    )

    assert [block.text for block in result.root.content] == ["need more"]
    # See the note in `test_hook_mode`: absence is the claim, and below mcp
    # 1.10 an unmirrored `structuredContent` is an unset extra rather than a
    # field holding None.
    assert getattr(result.root, "structuredContent", None) is None
    event = _call_events(capture)[0]
    assert event.tags["agentcat_mrtr"] == "input_required"
    assert event.session_id.startswith("ses_")


def test_track_never_raises():
    sentinel = object()
    assert track(sentinel, None) is sentinel
    assert track(sentinel, "proj_test") is sentinel
    assert track(None, "proj_test") is None
    assert track(object(), "proj_test", AgentCatOptions(enable_tracing=False))
    # An options object of the wrong shape used to blow up on attribute access.
    assert track(sentinel, "proj_test", {"debug_mode": True}) is sentinel
    # Missing project_id with no exporters used to raise ValueError.
    server = create_todo_server()
    assert track(server, None) is server


def test_unrecognized_shape_logs_the_fingerprint_beacon(log_sink):
    """The unknown-shape path feeds the diagnostics sink a probe fingerprint —
    that is the fleet-drift beacon (changelog 6.7)."""
    track(object(), "proj_test")

    beacons = [line for line in log_sink if "fingerprint=" in line]
    assert beacons, log_sink
    assert "Unrecognized" in beacons[0]
    assert "has_request_handlers" in beacons[0]


def test_community_fastmcp_v2_is_reported_unsupported(log_sink):
    """A FastMCP 2.x-shaped object is refused with actionable copy, not tracked."""

    class FastMCPV2Shape:
        def __init__(self) -> None:
            self._mcp_server = object()
            self._tool_manager = object()

    FastMCPV2Shape.__module__ = "fastmcp.server.server"
    server = FastMCPV2Shape()

    assert track(server, "proj_test") is server
    assert any("agentcat<2" in line for line in log_sink), log_sink


def test_the_installed_official_sdk_classifies_as_a_lowlevel_v1_flavor():
    """Every other detection test builds doubles from what we *believe* each
    generation looks like. This one asks the SDK actually installed here, so an
    upstream shape change surfaces as a failure rather than as a silently
    untracked fleet.

    Replaces tests/test_mcp_version_compatibility.py, whose `is_compatible_server`
    gate the flavor classifier subsumes.
    """
    from mcp.server import Server

    from agentcat.modules.detection import ServerFlavor, detect_server

    fastmcp = create_todo_server()
    detected = detect_server(fastmcp)
    assert detected.flavor is ServerFlavor.OFFICIAL_FASTMCP_V1
    assert detected.lowlevel is fastmcp._mcp_server

    lowlevel = Server("bare")
    detected = detect_server(lowlevel)
    assert detected.flavor is ServerFlavor.LOWLEVEL_V1
    assert detected.lowlevel is lowlevel


async def test_wrapped_list_preserves_meta_and_extra_result_fields(capture):
    """Audit finding 11: the v1 tools/list rebuild used to reconstruct
    `ListToolsResult(tools=..., nextCursor=...)`, silently dropping `_meta`
    and any extra fields a raw list handler set. The result must now be a
    `model_copy` of the customer's own object — injection applied, everything
    else intact (mcp 1.x `Result` is extra='allow')."""
    from mcp.server.lowlevel import Server
    from mcp.types import ListToolsRequest, ListToolsResult, ServerResult, Tool

    server = Server("meta-server")

    async def list_tools(req):
        return ServerResult(
            ListToolsResult(
                tools=[
                    Tool(
                        name="echo",
                        description="Echo the text back.",
                        inputSchema={
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    )
                ],
                nextCursor="page-2",
                # The aliased spelling: this mcp generation does not populate
                # the `meta` field by name, only via its `_meta` alias.
                **{"_meta": {"total": 41}},
                cacheHint="warm",  # an extra field, allowed by Result
            )
        )

    server.request_handlers[ListToolsRequest] = list_tools

    track(server, "proj_test", AgentCatOptions())
    handler = server.request_handlers[ListToolsRequest]
    result = await handler(ListToolsRequest(method="tools/list"))

    listed = result.root
    # Injection happened...
    echo = next(t for t in listed.tools if t.name == "echo")
    assert "session_id" in echo.inputSchema["properties"]
    # ...and nothing the customer's handler set was dropped.
    assert listed.nextCursor == "page-2"
    assert listed.meta == {"total": 41}
    assert getattr(listed, "cacheHint", None) == "warm"
