"""End-to-end handle behavior on the official MCP SDK 2.x lowlevel adapter.

Everything here drives a real in-process `Client` against a real server, so the
assertions cover the whole wire path — params validation, the SDK's outbound
result sieve, and the per-version `serverInfo` stamp — rather than a
hand-called handler. What is asserted: schema injection on `tools/list`, the
stripped arguments the customer's tool actually receives, the mint-back the
agent sees, and the single `mcp:tools/call` event AgentCat publishes.

This is the first era where multi-round-trip tool calls are reachable, so the
`input_required` / `continuation` rounds here are real protocol behavior, not a
simulation.
"""

import copy
import dataclasses
import gc
import json
import weakref

import pytest
from mcp import types
from mcp.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import (
    AGENTCAT_TAG_AGENT_ID,
    AGENTCAT_TAG_AGENT_SOURCE,
    AGENTCAT_TAG_MRTR,
    AGENTCAT_TAG_PROTOCOL_VERSION,
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_INSTRUCTIONS_KEY,
)
from agentcat.modules.handles import derive_session_id

from .test_utils import sid
from .test_utils.delivery import delivered_arguments_for
from .test_utils.modern_server import (
    ADD_TODO_INPUT_SCHEMA,
    create_lowlevel_todo_server,
    create_mcpserver_todo_server,
    create_modern_client,
)

MINT_BACK_HEADER = "[MCP INSTRUCTIONS]: session_id issued."
MODERN = "2026-07-28"


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


def _tool(listed, name):
    return next(t for t in listed.tools if t.name == name)


# ── injection, mint-back, strip, event ───────────────────────────────────────


async def test_prompted_mode_end_to_end(capture):
    server = create_lowlevel_todo_server()
    track(server, "proj_test", AgentCatOptions())

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        add = _tool(listed, "add_todo")
        assert list(add.input_schema["properties"])[-2:] == ["session_id", "context"]
        # session_id is the one injected param that is never required — omitting
        # it is the minting signal. `context` is required, which is the only
        # thing that makes agents supply intent at all.
        assert "session_id" not in add.input_schema.get("required", [])
        assert "context" in add.input_schema["required"]
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


async def test_protocol_version_tag_comes_from_the_request(capture):
    """The 2026 wire carries the version in `_meta`; the handshake era falls
    back to `ctx.protocol_version`. Both must reach the event."""
    server = create_lowlevel_todo_server()
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        await client.call_tool("add_todo", {"text": "modern"})
    assert _call_events(capture)[-1].tags[AGENTCAT_TAG_PROTOCOL_VERSION] == MODERN

    async with create_modern_client(server, mode="legacy") as client:
        await client.call_tool("add_todo", {"text": "legacy"})
    version = _call_events(capture)[-1].tags[AGENTCAT_TAG_PROTOCOL_VERSION]
    assert version and version != MODERN


async def test_client_identity_ladder(capture):
    """Envelope `_meta` on the modern wire; the handshake `client_info` the
    session kept on the legacy one."""
    server = create_lowlevel_todo_server()
    track(server, "proj_test")
    info = types.Implementation(name="MyAgent", version="1.2.3")

    for mode in (MODERN, "legacy"):
        async with create_modern_client(server, mode=mode, client_info=info) as client:
            await client.call_tool("add_todo", {"text": mode})
        event = _call_events(capture)[-1]
        assert (event.client_name, event.client_version) == ("MyAgent", "1.2.3"), mode


async def test_structured_mint_back_mirrors_into_structured_content(capture):
    """A tool with an output schema gets `_mcp_instructions` mirrored in, and
    its schema declares the field so schema-validating clients still accept
    it."""
    server = create_lowlevel_todo_server()
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        assert MCP_INSTRUCTIONS_KEY in _tool(listed, "add_todo").output_schema[
            "properties"
        ]

        result = await client.call_tool("add_todo", {"text": "structured"})
        mint = result.structured_content[MCP_INSTRUCTIONS_KEY]
        assert mint["session_id"].startswith("ses_")
        assert mint["session_id"] == _call_events(capture)[0].session_id
        # The customer's own structured payload survives untouched.
        assert result.structured_content["result"].startswith("Added todo")


async def test_handler_sees_stripped_args_and_customer_result_untouched(capture):
    """The injected params never reach the tool body, and what the tool returned
    is exactly what the agent gets back (minus AgentCat's trailing block)."""
    server = create_lowlevel_todo_server()
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        await client.list_tools()
        result = await client.call_tool(
            "add_todo",
            {"text": "payload", "session_id": sid("supplied"), "context": "why"},
        )

    # The tool raises on any argument it did not declare, so a strip regression
    # surfaces here rather than passing silently.
    assert result.is_error is False, _text(result)
    assert result.content[0].text == 'Added todo: "payload" with ID 1'
    # session_id was supplied, so nothing is minted back and nothing is appended.
    assert len(result.content) == 1
    assert _call_events(capture)[0].session_id == sid("supplied")


async def test_customer_tool_schema_is_never_mutated(capture):
    """Injection works on copies, never in place on customer objects."""
    pristine = copy.deepcopy(ADD_TODO_INPUT_SCHEMA)
    server = create_lowlevel_todo_server()
    reference = create_lowlevel_todo_server()
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        assert "session_id" in _tool(listed, "add_todo").input_schema["properties"]

    # The schema constant every todo server builds its tools from. Pydantic
    # copies only the outermost dict, so `properties` is shared by reference
    # with the listed Tool — an in-place injection pass shows up right here.
    assert ADD_TODO_INPUT_SCHEMA == pristine

    untracked = await reference.get_request_handler("tools/list").handler(None, None)
    for tool in untracked.tools:
        assert "session_id" not in tool.input_schema["properties"], tool.name
        assert "context" not in tool.input_schema["properties"], tool.name


async def test_agent_tracking_injection(capture):
    server = create_lowlevel_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_agent_tracking=True))

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        add = _tool(listed, "add_todo")
        assert list(add.input_schema["properties"])[-3:] == [
            "session_id",
            "agent_id",
            "context",
        ]
        assert "agent_id" in add.input_schema["required"]
        assert "session_id" not in add.input_schema["required"]

        result = await client.call_tool(
            "add_todo", {"text": "with agent", "agent_id": "opus|claude-code|k3n9x"}
        )
        assert result.is_error is False, _text(result)

    event = _call_events(capture)[0]
    assert event.tags[AGENTCAT_TAG_AGENT_ID] == "opus|claude-code|k3n9x"
    assert event.tags[AGENTCAT_TAG_AGENT_SOURCE] == "supplied"


async def test_hook_mode(capture):
    server = create_lowlevel_todo_server()
    track(
        server,
        "proj_test",
        AgentCatOptions(resolve_session_id=lambda request, extra: "cust-1"),
    )

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        assert listed.tools
        for tool in listed.tools:
            assert "session_id" not in tool.input_schema.get("properties", {})

        r1 = await client.call_tool("add_todo", {"text": "hook one"})
        r2 = await client.call_tool("add_todo", {"text": "hook two"})
        assert "[MCP INSTRUCTIONS]" not in _text(r1)
        assert "[MCP INSTRUCTIONS]" not in _text(r2)
        assert MCP_INSTRUCTIONS_KEY not in (r1.structured_content or {})

    call_events = _call_events(capture)
    expected = derive_session_id("cust-1", "proj_test")
    assert [e.session_id for e in call_events] == [expected, expected]
    assert call_events[0].tags[AGENTCAT_TAG_SESSION_SOURCE] == "hook"


async def test_tracing_disabled_strips_but_publishes_nothing(capture):
    server = create_lowlevel_todo_server()
    track(
        server,
        "proj_test",
        AgentCatOptions(enable_tracing=False, enable_report_missing=True),
    )

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        add = _tool(listed, "add_todo")
        # No handles when tracing is off, but the context parameter is
        # independent — so it must still be stripped before the tool runs.
        assert "session_id" not in add.input_schema["properties"]
        assert "context" in add.input_schema["properties"]

        result = await client.call_tool(
            "add_todo", {"text": "quiet", "context": "no tracing"}
        )
        assert result.is_error is False, _text(result)
        assert "[MCP INSTRUCTIONS]" not in _text(result)

        # get_more_tools is advertised and still answers with tracing off
        # (changelog 6.6) — it is a tool, not telemetry.
        reported = await client.call_tool("get_more_tools", {"context": "email"})
        assert "Unfortunately" in _text(reported)

    assert capture == []


async def test_error_result_still_carries_the_handle(capture):
    server = create_lowlevel_todo_server()
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        await client.list_tools()
        result = await client.call_tool("complete_todo", {"id": 999})

    assert result.is_error is True
    # A retry after a failure has to carry the same task, so error results
    # decorate on identical terms.
    assert MINT_BACK_HEADER in _text(result)
    event = _call_events(capture)[0]
    assert event.is_error is True
    assert event.session_id.startswith("ses_")
    # The no-tap error payload: the tool flattened whatever went wrong before
    # we saw it, so type is unrecoverable — but the message is the tool's own
    # text, not a repr of the result model, and the shape stays SDK-wide.
    assert event.error == {
        "message": "Todo with ID 999 not found",
        "type": None,
        "platform": "python",
    }


async def test_raising_handler_publishes_the_live_exception(capture):
    """A lowlevel v2 handler that raises reaches our wrapper with its traceback
    intact, so the event keeps the real type."""

    async def on_call_tool(ctx, params):
        raise RuntimeError("kaboom")

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="boom",
                    description="Raises.",
                    input_schema={"type": "object", "properties": {}},
                )
            ]
        )

    server = Server("raiser", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        # The runner turns a raised handler into a protocol-level error, so the
        # agent sees a failed request rather than an isError result.
        with pytest.raises(MCPError):
            await client.call_tool("boom", {})

    event = _call_events(capture)[0]
    assert event.is_error is True
    assert event.error["type"] == "RuntimeError"
    assert event.error["message"] == "kaboom"


# ── multi round-trip (§6.4) ──────────────────────────────────────────────────


def _mrtr_server() -> Server:
    """A tool that needs one extra round before it can complete."""

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="ask",
                    description="Needs another round.",
                    input_schema={"type": "object", "properties": {}},
                    output_schema={
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                    },
                )
            ]
        )

    async def on_call_tool(ctx, params):
        if params.input_responses is None:
            result = types.InputRequiredResult(
                request_state="opaque-state",
                input_requests={"roots": types.ListRootsRequest(method="roots/list")},
            )
        else:
            result = types.CallToolResult(
                content=[types.TextContent(type="text", text="done")],
                structured_content={"answer": "done"},
            )
        return result

    return Server("mrtr", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def test_intermediate_mrtr_round_is_tagged_but_never_decorated(capture):
    """Two real rounds over the wire: the intermediate one is tagged
    `input_required` and reaches the agent as the server built it, the
    completing one is tagged `continuation` and carries the mint-back.

    The tag assertions are what this test is for. What stops the intermediate
    round being decorated is asserted by
    `test_an_intermediate_round_that_carries_content_is_still_undecorated` —
    an `InputRequiredResult` has no `content` or `structured_content` to
    decorate in the first place, so it cannot show the gate working.
    """
    server = _mrtr_server()
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        await client.list_tools()
        first = await client.session.call_tool("ask", {}, allow_input_required=True)
        assert isinstance(first, types.InputRequiredResult)
        assert first.request_state == "opaque-state"

        second = await client.session.call_tool(
            "ask",
            {},
            input_responses={"roots": types.ListRootsResult(roots=[])},
            request_state=first.request_state,
            allow_input_required=True,
        )

    events = _call_events(capture)
    assert len(events) == 2
    assert events[0].tags[AGENTCAT_TAG_MRTR] == "input_required"
    assert events[1].tags[AGENTCAT_TAG_MRTR] == "continuation"
    # The intermediate round still publishes.
    assert events[0].session_id.startswith("ses_")
    # Only the completing round carries the mint-back.
    assert MINT_BACK_HEADER in _text(second)
    assert MCP_INSTRUCTIONS_KEY in second.structured_content


async def test_input_responses_alone_still_tags_a_continuation(capture):
    """`inputResponses` is a continuation witness on its own.

    The round above carries both witnesses, so it can no longer tell which one
    fired. This one carries `inputResponses` with **no** `requestState`, which
    is the shape a client resuming from persisted responses sends — and the
    path that was pinned before `requestState` was added as a second witness.
    """
    server = _mrtr_server()
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        await client.list_tools()
        await client.session.call_tool(
            "ask",
            {},
            input_responses={"roots": types.ListRootsResult(roots=[])},
            allow_input_required=True,
        )

    event = _call_events(capture)[0]
    assert event.tags[AGENTCAT_TAG_MRTR] == "continuation"


def _state_only_mrtr_server() -> Server:
    """A tool that asks to be RESUMED rather than asking the client anything.

    Its intermediate round carries `requestState` and no `inputRequests`, so
    the SEP-2322 driver retries it after a backoff with no `inputResponses` at
    all — the continuation shape that carries only `requestState`, and the one
    FastMCP 4's own `InputRequiredResult(request_state=...)` produces.
    """
    rounds: list = []

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="resume",
                    description="Asks to be resumed.",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                )
            ]
        )

    async def on_call_tool(ctx, params):
        rounds.append(params.request_state)
        if len(rounds) == 1:
            return types.InputRequiredResult(request_state="not-done-yet")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="done")]
        )

    server = Server(
        "resume", on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )
    server.mrtr_rounds = rounds  # type: ignore[attr-defined]
    return server


async def test_a_request_state_only_continuation_is_tagged(capture):
    """A real client drives the state-only chain, and round two is tagged.

    Driven through `client.call_tool`, which runs the SDK's own SEP-2322
    driver — so the second round is the one the driver built, not one this
    test hand-shaped. Keying the tag on `inputResponses` alone left this round
    tagged `None`.
    """
    server = _state_only_mrtr_server()
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        await client.list_tools()
        await client.call_tool("resume", {"text": "hello"})

    # The driver really did send state and no responses.
    assert server.mrtr_rounds == [None, "not-done-yet"]
    events = _call_events(capture)
    assert [e.tags.get(AGENTCAT_TAG_MRTR) for e in events] == [
        "input_required",
        "continuation",
    ]


async def test_a_supplied_session_id_correlates_every_mrtr_round(capture):
    """The handle the agent supplied rides every round of the conversation.

    The SEP-2322 driver replays the ORIGINAL arguments verbatim on each retry
    (`mcp/client/_input_required.py`), so a `session_id` the agent supplied on
    round one is on the wire for round two as well and both rounds resolve to
    it. This is the correlation changelog §6.4 promises, and it is why the
    minted-first-call case is the only one that fragments.
    """
    server = _state_only_mrtr_server()
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        await client.list_tools()
        await client.call_tool(
            "resume", {"text": "hello", "session_id": sid("supplied")}
        )

    events = _call_events(capture)
    assert len(events) == 2
    assert [e.session_id for e in events] == [sid("supplied"), sid("supplied")]
    assert [e.tags[AGENTCAT_TAG_SESSION_SOURCE] for e in events] == [
        "supplied",
        "supplied",
    ]
    # The customer's tool never sees the handle, on any round.
    assert server.mrtr_rounds == [None, "not-done-yet"]


async def test_hook_mode_correlates_every_mrtr_round(capture):
    """A `resolve_session_id` hook correlates the rounds too.

    The hook runs per request against that round's own request/extra, and every
    round of one conversation is the same tool call on the same connection — so
    anything a hook keys on returns the same value and derives the same task.
    With `supplied` (above) that leaves prompted-mode MINTING as the only
    resolution mode an MRTR conversation fragments under.
    """
    seen: list = []
    server = _state_only_mrtr_server()

    def hook(request, extra):
        seen.append(request)
        return "tenant"

    track(server, "proj_test", AgentCatOptions(resolve_session_id=hook))

    async with create_modern_client(server, mode=MODERN) as client:
        await client.list_tools()
        await client.call_tool("resume", {"text": "hello"})

    events = _call_events(capture)
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert [e.tags[AGENTCAT_TAG_SESSION_SOURCE] for e in events] == ["hook", "hook"]
    derived = derive_session_id("tenant", "proj_test")
    assert [e.session_id for e in events] == [derived, derived]


async def test_an_mrtr_conversation_is_byte_identical_tracked_or_not(capture):
    """Tagging a round changes the event, never the wire (design §12).

    Both rounds of the same real conversation are compared as serialized JSON
    against an untracked run of the identical server. The agent supplies a
    handle, so nothing is minted and there is no specified decoration to
    subtract: any difference at all would be AgentCat altering what the client
    is told. `continuation` and an untagged round take the same wire path —
    only `input_required` gates decoration — so re-tagging one cannot move it.
    """

    async def rounds_of(tracked: bool) -> list[str]:
        seen: list = []
        server = _state_only_mrtr_server()
        if tracked:
            track(server, "proj_test")
        async with create_modern_client(server, mode=MODERN) as client:
            await client.list_tools()
            # Driven by hand so the INTERMEDIATE round is observable too; the
            # SDK's driver resolves it away before `call_tool` returns.
            first = await client.session.call_tool(
                "resume",
                {"text": "hello", "session_id": sid("supplied")},
                allow_input_required=True,
            )
            seen.append(first.model_dump_json())
            second = await client.session.call_tool(
                "resume",
                {"text": "hello", "session_id": sid("supplied")},
                request_state=first.request_state,
                allow_input_required=True,
            )
            seen.append(second.model_dump_json())
        return seen

    tracked = await rounds_of(True)
    untracked = await rounds_of(False)
    assert tracked == untracked
    # And the conversation really was tagged on the tracked run.
    assert [e.tags.get(AGENTCAT_TAG_MRTR) for e in _call_events(capture)] == [
        "input_required",
        "continuation",
    ]


async def test_a_minted_first_round_does_not_yet_correlate(capture):
    """KNOWN GAP, pinned deliberately: a minted MRTR chain fragments.

    Round one mints a handle the agent is never told — §6.4 forbids decorating
    an intermediate round — and the driver replays round one's arguments
    verbatim, so round two carries no handle either and mints its own. Both
    ways of correlating them are closed, but for different reasons: remembering
    `requestState -> session_id` across rounds is server-side state, which design
    §13 forbids ("no server-side session or handle storage"), while carrying
    the handle on the wire INSIDE `requestState` is genuinely stateless and
    does work — it just rewrites a value the customer's own tool produced,
    which design §12 forbids. See task 13.6's report §5.

    Delete this test when that design question is answered; until then it stops
    the fragmentation being rediscovered as a surprise.
    """
    server = _state_only_mrtr_server()
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        await client.list_tools()
        result = await client.call_tool("resume", {"text": "hello"})

    events = _call_events(capture)
    assert len(events) == 2
    assert [e.tags[AGENTCAT_TAG_SESSION_SOURCE] for e in events] == ["minted", "minted"]
    assert events[0].session_id != events[1].session_id
    # What the agent is handed is the COMPLETING round's handle, so every call
    # after the conversation joins that task.
    minted = _text(result).split("session_id=")[1].split(" ")[0]
    assert events[1].session_id == minted


async def test_an_intermediate_round_that_carries_content_is_still_undecorated(
    capture,
):
    """`result_type` is what makes a round intermediate, not the result class:
    a `CallToolResult` may report `input_required` while already carrying
    partial content and structured output. That round must go back exactly as
    the customer built it — asserted by object identity, because the mint-back
    would otherwise be indistinguishable from a completing round's."""
    returned: list = []

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="partial",
                    description="Answers in installments.",
                    input_schema={"type": "object", "properties": {}},
                    output_schema={
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                    },
                )
            ]
        )

    async def on_call_tool(ctx, params):
        result = types.CallToolResult(
            content=[types.TextContent(type="text", text="so far so good")],
            structured_content={"answer": "partial"},
            result_type="input_required",
        )
        returned.append(result)
        return result

    server = Server(
        "partial", on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )
    track(server, "proj_test")

    handler = server.get_request_handler("tools/call").handler
    result = await handler(
        None, types.CallToolRequestParams(name="partial", arguments={})
    )

    assert result is returned[0]
    assert MCP_INSTRUCTIONS_KEY not in result.structured_content
    assert [block.text for block in result.content] == ["so far so good"]
    assert _call_events(capture)[0].tags[AGENTCAT_TAG_MRTR] == "input_required"


async def test_a_resolved_call_is_never_reused_across_rounds(capture):
    """Each round resolves afresh, so the customer's event callbacks see that
    round's own params."""
    seen: list = []
    server = _mrtr_server()
    track(
        server,
        "proj_test",
        AgentCatOptions(
            event_tags=lambda request, extra: seen.append(request) or {"round": "x"}
        ),
    )

    async with create_modern_client(server, mode=MODERN) as client:
        first = await client.session.call_tool("ask", {}, allow_input_required=True)
        await client.session.call_tool(
            "ask",
            {},
            input_responses={"roots": types.ListRootsResult(roots=[])},
            request_state=first.request_state,
            allow_input_required=True,
        )

    assert len(seen) == 2
    assert seen[0] is not seen[1]
    assert seen[0].input_responses is None
    assert seen[1].input_responses is not None


async def test_the_request_clone_keeps_the_continuation_fields(capture):
    """Stripping clones the params rather than rebuilding them, so
    `input_responses` / `request_state` / `_meta` survive into the handler."""
    seen: list = []

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="echo",
                    description="Echo.",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                )
            ]
        )

    async def on_call_tool(ctx, params):
        seen.append(params)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="ok")]
        )

    server = Server("echo", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    track(server, "proj_test")

    async with create_modern_client(server, mode=MODERN) as client:
        await client.list_tools()
        await client.session.call_tool(
            "echo",
            {"text": "hi", "session_id": sid("x"), "context": "why"},
            input_responses={"roots": types.ListRootsResult(roots=[])},
            request_state="carried",
            allow_input_required=True,
        )

    params = seen[0]
    assert params.arguments == {"text": "hi"}
    assert params.request_state == "carried"
    assert params.input_responses is not None
    assert params.meta is not None


# ── get_more_tools ownership ─────────────────────────────────────────────────


async def test_get_more_tools_keeps_its_own_context_and_publishes(capture):
    server = create_lowlevel_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        gmt = _tool(listed, "get_more_tools")
        # Its bespoke `context` is a real parameter: still required, still
        # described by the tool's own copy — and handles ride alongside.
        assert gmt.input_schema["required"] == ["context"]
        assert "session_id" in gmt.input_schema["properties"]
        assert gmt.annotations.read_only_hint is True

        result = await client.call_tool(
            "get_more_tools", {"context": "I need a tool to send emails"}
        )
        assert "Unfortunately" in _text(result)

    event = _call_events(capture)[0]
    assert event.resource_name == "get_more_tools"
    assert event.user_intent == "I need a tool to send emails"
    assert event.parameters["arguments"]["context"] == "I need a tool to send emails"


async def test_customer_get_more_tools_is_never_hijacked(capture):
    """A customer tool that happens to be named `get_more_tools` keeps running.

    We refuse to advertise a second one; we must equally refuse to answer for
    the one they wrote (spec §12 — nothing may alter customer tool behavior).
    """

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="get_more_tools",
                    description="The customer's own tool.",
                    input_schema={
                        "type": "object",
                        "properties": {"context": {"type": "string"}},
                        "required": ["context"],
                    },
                )
            ]
        )

    async def on_call_tool(ctx, params):
        answer = (params.arguments or {}).get("context")
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text=f"customer answered: {answer}")
            ]
        )

    server = Server(
        "collision", on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        # Exactly one, and it is theirs — ours is not advertised alongside it.
        assert [t.name for t in listed.tools] == ["get_more_tools"]
        assert listed.tools[0].description.startswith("The customer's own tool")

        result = await client.call_tool("get_more_tools", {"context": "mine"})

    assert result.is_error is False, _text(result)
    assert "customer answered: mine" in _text(result)
    assert "Unfortunately" not in _text(result)
    assert _call_events(capture)[0].resource_name == "get_more_tools"


async def test_customer_get_more_tools_survives_a_server_with_no_listing(capture):
    """Nothing advertised the tool, so we may not answer for it.

    A server that exposes no `tools/list` at all never runs the pass that would
    tell us who owns the name. The gate is stated positively — "AgentCat
    advertised it" — so an absent listing keeps the customer's handler.

    Driven through the registered handler rather than a `Client`: the modern
    client revalidates every successful result against the tool's declared
    output schema and fetches `tools/list` to get it, so no client can reach a
    server that serves no listing at all.
    """

    async def on_call_tool(ctx, params):
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text", text=f"customer answered: {params.name}"
                )
            ]
        )

    server = Server("no-listing", on_call_tool=on_call_tool)
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    handler = server.get_request_handler("tools/call").handler
    result = await handler(
        None,
        types.CallToolRequestParams(
            name="get_more_tools", arguments={"context": "mine"}
        ),
    )

    assert "customer answered: get_more_tools" in _text(result)
    assert "Unfortunately" not in _text(result)
    # Still tracked: no listing is not a reason to stop publishing.
    assert _call_events(capture)[0].resource_name == "get_more_tools"


# ── install semantics: idempotence, re-arm, degrade ──────────────────────────


async def test_retracking_updates_options_without_double_wrapping(capture):
    server = create_lowlevel_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_report_missing=False))
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        assert [t.name for t in listed.tools].count("get_more_tools") == 1
        add = _tool(listed, "add_todo")
        assert list(add.input_schema["properties"]) == ["text", "session_id", "context"]

        # A second, stacked wrapper would inject session_id in the inner pass and
        # then skip it in the outer one — leaving it out of the strip registry
        # and handing the customer's tool a parameter it never declared.
        result = await client.call_tool(
            "add_todo", {"text": "retracked", "session_id": sid("retrack")}
        )
        assert result.is_error is False, _text(result)

    assert len(_call_events(capture)) == 1
    assert _call_events(capture)[0].session_id == sid("retrack")


def test_a_tracked_server_is_collectable_once_the_customer_drops_it():
    """Nothing AgentCat holds may outlive the server.

    This era's documented topology is a fresh server per request with
    `track()` inside the factory (cross-SDK changelog §6.8), so per-server
    state that survives the server is not a bounded cost — it is one immortal
    `Server`, handler table, deep-copied schema set and registry pair per
    request. A module-level `WeakKeyDictionary` does not save you: every value
    an adapter files there closes over the server, so the value keeps its own
    weak key alive.
    """
    alive = []
    for _ in range(3):
        server = create_lowlevel_todo_server()
        track(server, "proj_test")
        alive.append(weakref.ref(server))
        del server

    gc.collect()
    assert [ref() for ref in alive] == [None, None, None]


def test_a_tracked_mcpserver_is_collectable_with_its_lowlevel_server():
    """The adapter is installed on `_lowlevel_server`, so both halves have to
    go — a retained lowlevel server pins the MCPServer that owns it, and its
    tool manager with it."""
    server = create_mcpserver_todo_server()
    track(server, "proj_test")
    facade, lowlevel = weakref.ref(server), weakref.ref(server._lowlevel_server)
    del server

    gc.collect()
    assert (facade(), lowlevel()) == (None, None)


def test_a_server_that_cannot_hold_state_is_left_untracked(log_sink):
    """State on the server is what keeps a repeated `track()` idempotent, so a
    server that refuses the attribute is refused rather than tracked with a
    wrapper we could never recognize again."""

    class Slotted:
        # `__weakref__` so it gets as far as the adapter: the tracking-data map
        # is weakly keyed and would refuse it one step earlier otherwise.
        __slots__ = ("__weakref__", "_request_handlers", "add_request_handler")

        def __init__(self) -> None:
            self._request_handlers: dict = {}
            self.add_request_handler = lambda *a: None

    server = Slotted()
    source = create_lowlevel_todo_server("slotted-source")
    server._request_handlers["tools/call"] = source.get_request_handler("tools/call")

    assert track(server, "proj_test") is server
    assert server._request_handlers["tools/call"] is source.get_request_handler(
        "tools/call"
    )
    assert any("could not store AgentCat state" in line for line in log_sink), log_sink


async def test_options_are_read_per_request_not_captured_at_install(capture):
    """The wrappers re-read the tracking data on every call.

    Captured once at install, a wrapper would serve the first `track()`'s
    options forever — so anything that replaces a server's data without
    re-installing (a re-`track()` racing a request, a restore) would silently
    not take effect.
    """
    from agentcat.modules.internal import set_server_tracking_data
    from agentcat.types import AgentCatData

    server = create_lowlevel_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_tracing=False))
    set_server_tracking_data(
        server,
        AgentCatData(
            project_id="proj_test", options=AgentCatOptions(enable_tracing=True)
        ),
    )

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        assert "session_id" in _tool(listed, "add_todo").input_schema["properties"]
        result = await client.call_tool("add_todo", {"text": "live"})
        assert result.is_error is False, _text(result)

    assert len(_call_events(capture)) == 1


async def test_handlers_registered_after_track_are_still_wrapped(capture):
    """`track()` on a server with no tools handlers yet still takes effect: the
    registration seam is re-armed, so a later `add_request_handler` lands
    wrapped."""
    server = Server("late")
    track(server, "proj_test")

    late = create_lowlevel_todo_server("late-source")
    for method in ("tools/list", "tools/call"):
        entry = late.get_request_handler(method)
        server.add_request_handler(method, entry.params_type, entry.handler)

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        assert "session_id" in _tool(listed, "add_todo").input_schema["properties"]
        result = await client.call_tool("add_todo", {"text": "late"})
        assert result.is_error is False, _text(result)
        assert MINT_BACK_HEADER in _text(result)

    assert len(_call_events(capture)) == 1


async def test_reregistering_a_handler_after_track_rewraps_it(capture):
    """A customer who replaces their `tools/call` handler after `track()` does
    not silently lose tracking."""
    server = create_lowlevel_todo_server()
    track(server, "proj_test")

    replacement = create_lowlevel_todo_server("replacement")
    entry = replacement.get_request_handler("tools/call")
    server.add_request_handler("tools/call", entry.params_type, entry.handler)

    async with create_modern_client(server, mode=MODERN) as client:
        await client.list_tools()
        result = await client.call_tool("add_todo", {"text": "replaced"})
        assert result.is_error is False, _text(result)
        assert MINT_BACK_HEADER in _text(result)

    assert len(_call_events(capture)) == 1


async def test_re_arm_is_not_stacked_by_a_second_track(capture):
    """Two `track()` calls leave one wrapper, not two, even on a handler
    registered after both."""
    server = Server("double")
    track(server, "proj_test")
    track(server, "proj_test")

    source = create_lowlevel_todo_server("source")
    for method in ("tools/list", "tools/call"):
        entry = source.get_request_handler(method)
        server.add_request_handler(method, entry.params_type, entry.handler)

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        add = _tool(listed, "add_todo")
        # A stacked pair would inject session_id twice or leave it unstrippable.
        assert list(add.input_schema["properties"]) == ["text", "session_id", "context"]
        result = await client.call_tool("add_todo", {"text": "twice"})
        assert result.is_error is False, _text(result)

    assert len(_call_events(capture)) == 1


async def test_resolution_failure_degrades_to_an_untraced_call(capture, log_sink):
    """A blown resolver must not fail the customer's call — and the injected
    parameters are still stripped on the way through."""
    server = create_lowlevel_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    from agentcat.modules.adapters import lowlevel_v2

    async def boom(*args, **kwargs):
        raise RuntimeError("resolver exploded")

    original = lowlevel_v2.resolve_call
    lowlevel_v2.resolve_call = boom
    try:
        async with create_modern_client(server, mode=MODERN) as client:
            await client.list_tools()
            result = await client.call_tool(
                "add_todo", {"text": "degraded", "context": "why"}
            )
            # A tool AgentCat advertised must still be answered by AgentCat, or
            # the agent gets "unknown tool" for something it was just offered.
            reported = await client.call_tool("get_more_tools", {"context": "email"})
    finally:
        lowlevel_v2.resolve_call = original

    assert result.is_error is False, _text(result)
    assert "Unfortunately" in _text(reported)
    assert _call_events(capture) == []
    assert any("running it untraced" in line for line in log_sink), log_sink


# ── MCPServer ────────────────────────────────────────────────────────────────


async def test_mcpserver_end_to_end(capture):
    """`MCPServer` is served through its `_lowlevel_server` by the same
    adapter."""
    server = create_mcpserver_todo_server()
    assert track(server, "proj_test") is server

    async with create_modern_client(server, mode=MODERN) as client:
        listed = await client.list_tools()
        add = _tool(listed, "add_todo")
        assert list(add.input_schema["properties"])[-2:] == ["session_id", "context"]

        result = await client.call_tool(
            "add_todo", {"text": "via mcpserver", "context": "why"}
        )
        assert result.is_error is False, _text(result)
        text = _text(result)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id=")[1].split(" ")[0]

    # `add_todo(text: str)` is typed and this manager drops an undeclared
    # argument silently, so the delivered dict is the only place a failed
    # strip would show. See `tests.test_utils.delivery`.
    assert delivered_arguments_for(server, "add_todo") == [{"text": "via mcpserver"}]

    events = _call_events(capture)
    assert len(events) == 1
    assert events[0].resource_name == "add_todo"
    assert events[0].session_id == minted
    # Server identity is captured off the lowlevel object at track() time.
    assert events[0].tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"


async def test_mcpserver_tracking_is_keyed_on_the_lowlevel_server(capture):
    server = create_mcpserver_todo_server()
    track(server, "proj_test")

    from agentcat.modules.internal import get_server_tracking_data

    assert get_server_tracking_data(server._lowlevel_server) is not None


class _DevLineServer:
    """The 2.x shape from before `HandlerEntry` and the public seam landed.

    Read off the vendored upstream checkout
    (`model-context-protocol-sdks/python-sdk`, v1.25.0-156-g3d7b311): bare
    callables in `_request_handlers`, registration through a private
    `_add_request_handler(method, handler)`. The classifier accepts it, so the
    adapter has to survive it — otherwise a build shipping that spelling would
    be classified, then explode or half-install.
    """

    def __init__(self) -> None:
        self._request_handlers: dict = {}

    def _add_request_handler(self, method, handler) -> None:
        self._request_handlers[method] = handler


async def test_the_pre_handler_entry_shape_installs_through_its_private_seam(
    capture,
):
    server = _DevLineServer()
    source = create_lowlevel_todo_server("dev-line-source")
    server._add_request_handler(
        "tools/list", source.get_request_handler("tools/list").handler
    )

    track(server, "proj_test")

    # Registered after track(): it lands wrapped through the private seam.
    server._add_request_handler(
        "tools/call", source.get_request_handler("tools/call").handler
    )

    listed = await server._request_handlers["tools/list"](None, None)
    assert "session_id" in _tool(listed, "add_todo").input_schema["properties"]

    result = await server._request_handlers["tools/call"](
        None,
        types.CallToolRequestParams(
            name="add_todo", arguments={"text": "dev line", "context": "why"}
        ),
    )
    assert result.is_error is False, _text(result)
    assert MINT_BACK_HEADER in _text(result)
    assert _call_events(capture)[0].resource_name == "add_todo"


class _DualSeamServer:
    """A build exposing BOTH registration spellings.

    The natural refactor shape when a private method is promoted: a public
    wrapper delegating to the private one, both live at once. Whichever seam a
    customer registers through has to end up wrapped — patching only the first
    name found would silently drop re-arm for the other.
    """

    def __init__(self) -> None:
        self._request_handlers: dict = {}

    def _add_request_handler(self, method, params_type, handler) -> None:
        entry = _DevLineEntry(params_type, handler)
        self._request_handlers[method] = entry

    def add_request_handler(self, method, params_type, handler) -> None:
        self._add_request_handler(method, params_type, handler)


@dataclasses.dataclass(frozen=True)
class _DevLineEntry:
    params_type: type
    handler: object


@pytest.mark.parametrize("seam", ["add_request_handler", "_add_request_handler"])
async def test_every_registration_seam_is_re_armed(capture, seam):
    server = _DualSeamServer()
    track(server, "proj_test")

    source = create_lowlevel_todo_server("dual-seam-source")
    for method in ("tools/list", "tools/call"):
        entry = source.get_request_handler(method)
        getattr(server, seam)(method, entry.params_type, entry.handler)

    listed = await server._request_handlers["tools/list"].handler(None, None)
    assert "session_id" in _tool(listed, "add_todo").input_schema["properties"]

    result = await server._request_handlers["tools/call"].handler(
        None, types.CallToolRequestParams(name="add_todo", arguments={"text": "dual"})
    )
    assert result.is_error is False, _text(result)
    assert MINT_BACK_HEADER in _text(result)
    # One event, not two: the public seam delegates to the private one, so both
    # patches fire on a public registration and the re-swap must stay idempotent.
    assert len(_call_events(capture)) == 1


async def test_a_registration_record_keeps_every_field_it_carries(capture):
    """The record is rebuilt, not mutated (`HandlerEntry` is frozen), so it must
    carry fields by name — a positional rebuild would silently drop anything
    upstream adds beyond `params_type` and `handler`."""

    @dataclasses.dataclass(frozen=True)
    class _RicherEntry:
        params_type: type
        handler: object
        title: str = "customer title"

    server = _DualSeamServer()
    source = create_lowlevel_todo_server("richer-source")
    entry = source.get_request_handler("tools/call")
    server._request_handlers["tools/call"] = _RicherEntry(
        entry.params_type, entry.handler, title="do not lose me"
    )

    track(server, "proj_test")

    installed = server._request_handlers["tools/call"]
    assert installed.handler is not entry.handler
    assert installed.params_type is entry.params_type
    assert installed.title == "do not lose me"


# ── detection ────────────────────────────────────────────────────────────────


def test_the_installed_modern_sdk_classifies_as_a_v2_flavor():
    """Every other detection test builds doubles from what we *believe* each
    generation looks like. This one asks the SDK actually installed here, so an
    upstream shape change surfaces as a failure rather than as a silently
    untracked fleet."""
    from agentcat.modules.detection import ServerFlavor, detect_server

    lowlevel = Server("bare")
    detected = detect_server(lowlevel)
    assert detected.flavor is ServerFlavor.LOWLEVEL_V2
    assert detected.lowlevel is lowlevel

    mcpserver = MCPServer("bare")
    detected = detect_server(mcpserver)
    assert detected.flavor is ServerFlavor.MCPSERVER_V2
    assert detected.lowlevel is mcpserver._lowlevel_server


def test_track_never_raises():
    sentinel = object()
    assert track(sentinel, None) is sentinel
    assert track(sentinel, "proj_test") is sentinel
    server = create_lowlevel_todo_server()
    assert track(server, None) is server
    assert track(server, "proj_test", {"debug_mode": True}) is server
