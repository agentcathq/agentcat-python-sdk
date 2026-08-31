"""End-to-end handle behavior on the community FastMCP adapter.

The community sibling of `tests/test_lowlevel_v1_handles.py`: every assertion
here rides a real `fastmcp.Client` against a real server, so the whole wire
path is covered — schema injection on `tools/list`, the stripped arguments the
customer's tool actually receives, the mint-back the agent sees, and the single
`mcp:tools/call` event AgentCat publishes.
"""

import json

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import (
    AGENTCAT_TAG_AGENT_ID,
    AGENTCAT_TAG_AGENT_SOURCE,
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_SESSION_KEY,
    META_CLIENT_INFO_KEY,
)
from agentcat.modules.handles import derive_session_id

from ..test_utils import sid
from ..test_utils.community_client import (
    HAS_COMMUNITY_CLIENT,
    create_community_test_client,
)
from ..test_utils.community_todo_server import (
    HAS_COMMUNITY_FASTMCP,
    create_community_todo_server,
)

pytestmark = pytest.mark.skipif(
    not (HAS_COMMUNITY_FASTMCP and HAS_COMMUNITY_CLIENT),
    reason="Community FastMCP not available",
)

MINT_BACK_HEADER = "[session_id issued — see this tool's session_id parameter description]"  # noqa: E501


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


def _call_events(capture) -> list:
    return [e for e in capture if e.event_type == "mcp:tools/call"]


def _named(tools, name):
    return next(t for t in tools if t.name == name)


def _new_server(name: str = "probe-server"):
    from fastmcp import FastMCP

    return FastMCP(name)


async def test_prompted_mode_end_to_end(capture):
    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        add = _named(listed, "add_todo")
        assert list(add.inputSchema["properties"])[-2:] == ["session_id", "context"]
        # Both injected params are required on the wire — schema-aware clients
        # are the only enforcement, and session_id names `start` as its
        # explicit first-call value.
        assert "session_id" in add.inputSchema["required"]
        assert "pattern" in add.inputSchema["properties"]["session_id"]
        assert "context" in add.inputSchema["required"]
        assert any(t.name == "get_more_tools" for t in listed)

        r1 = await client.call_tool(
            "add_todo",
            {
                "text": "hi",
                "session_id": "start",
                "context": "Adding a todo item for the user's task list to track work",
            },
        )
        text = _text(r1)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id: ")[1].split("\n")[0]
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
    assert "[session_id" not in json.dumps(call_events[0].response)
    assert call_events[0].tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"
    assert call_events[1].tags[AGENTCAT_TAG_SESSION_SOURCE] == "supplied"
    assert call_events[0].user_intent.startswith("Adding a todo item")


async def test_structured_mint_back_mirrors_into_structured_content(capture):
    """A tool with an output schema gets `mcp_session` mirrored in, and
    its schema declares the field so schema-validating clients still accept
    it."""
    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        assert MCP_SESSION_KEY in _named(listed, "add_todo").outputSchema[
            "properties"
        ]

        result = await client.call_tool("add_todo", {"text": "structured"})
        mint = result.structured_content[MCP_SESSION_KEY]
        assert mint["session_id"].startswith("ses_")
        assert mint["session_id"] == _call_events(capture)[0].session_id
        # The customer's own structured payload survives untouched.
        assert result.structured_content["result"].startswith("Added todo")


async def test_handler_sees_stripped_args_and_customer_result_untouched(capture):
    """The injected params never reach the tool body, and what the tool returned
    is exactly what the agent gets back (minus AgentCat's leading block)."""
    seen: dict = {}
    server = _new_server()

    @server.tool
    def probe(text: str) -> str:
        """A tool that would fail validation if handed an unexpected argument."""
        seen["text"] = text
        return f"probe:{text}"

    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        await client.list_tools()
        result = await client.call_tool(
            "probe",
            {"text": "payload", "session_id": sid("supplied"), "context": "why"},
        )

    assert result.is_error is False, _text(result)
    assert seen == {"text": "payload"}
    assert result.content[0].text == "probe:payload"
    # session_id was supplied, so nothing is minted back and nothing is added.
    assert len(result.content) == 1
    assert _call_events(capture)[0].session_id == sid("supplied")


async def test_get_more_tools_keeps_its_own_context_and_publishes(capture):
    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        gmt = _named(listed, "get_more_tools")
        # Its bespoke `context` is a real parameter: still required, still
        # described by the tool's own copy — and handles ride alongside,
        # required like everywhere else.
        assert gmt.inputSchema["required"] == ["context", "session_id"]
        assert gmt.inputSchema["properties"]["context"]["description"].startswith(
            "A description of your goal"
        )
        assert "session_id" in gmt.inputSchema["properties"]
        assert gmt.annotations.readOnlyHint is True

        result = await client.call_tool(
            "get_more_tools", {"context": "I need a tool to send emails"}
        )
        assert "Unfortunately" in _text(result)

    event = _call_events(capture)[0]
    assert event.resource_name == "get_more_tools"
    assert event.user_intent == "I need a tool to send emails"
    assert event.parameters["arguments"]["context"] == "I need a tool to send emails"


async def test_get_more_tools_absent_when_disabled(capture):
    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_report_missing=False))

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()

    assert not any(t.name == "get_more_tools" for t in listed)


async def test_customer_get_more_tools_is_never_replaced(capture):
    """A customer tool that happens to be named `get_more_tools` keeps running.

    Registering ours over theirs would silently swap their handler for our
    canned reply, and nothing AgentCat does may alter tool behavior (spec §12).
    """
    server = _new_server("collision-server")

    @server.tool
    def get_more_tools(context: str) -> str:
        """The customer's own tool, which happens to share our name."""
        return f"customer answered: {context}"

    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        # Exactly one, and it is theirs — ours is not registered alongside it.
        assert [t.name for t in listed] == ["get_more_tools"]
        assert listed[0].description.startswith("The customer's own tool")

        result = await client.call_tool("get_more_tools", {"context": "mine"})

    assert result.is_error is False, _text(result)
    assert "customer answered: mine" in _text(result)
    assert "Unfortunately" not in _text(result)
    # Still tracked like any other tool call.
    assert _call_events(capture)[0].resource_name == "get_more_tools"


async def test_a_mounted_customer_get_more_tools_is_never_usurped(capture):
    """A customer tool can arrive from a provider the install-time gate cannot
    see — a mounted sub-server, a proxy, an OpenAPI provider — and the local
    provider wins aggregation, so ours would answer for theirs. The first
    listing is the full provider view, and it concedes the name.
    """
    parent = _new_server("parent")
    sub = _new_server("sub")

    @sub.tool
    def get_more_tools(context: str) -> str:
        """The customer's own tool, supplied by a mounted sub-server."""
        return f"customer answered: {context}"

    parent.mount(sub)
    track(parent, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_community_test_client(parent) as client:
        listed = await client.list_tools()
        # Exactly one, and it is theirs.
        assert [t.name for t in listed] == ["get_more_tools"]
        assert listed[0].description.startswith("The customer's own tool")

        result = await client.call_tool("get_more_tools", {"context": "mine"})

    assert "customer answered: mine" in _text(result)
    assert "Unfortunately" not in _text(result)
    # And ours is gone from the registry, so the call path routes to theirs too.
    names = [t.name for t in await parent.list_tools(run_middleware=False)]
    assert names.count("get_more_tools") == 1
    assert _call_events(capture)[0].resource_name == "get_more_tools"


async def test_a_mounted_get_more_tools_survives_a_re_track(capture):
    """The local gate cannot tell OUR get_more_tools from a customer's, so a
    re-track skips registration — and the fresh middleware must inherit the
    marker or it could never concede the name again."""
    parent = _new_server("parent")
    sub = _new_server("sub")

    @sub.tool
    def get_more_tools(context: str) -> str:
        """The customer's own tool, supplied by a mounted sub-server."""
        return f"customer answered: {context}"

    parent.mount(sub)
    # No listing in between: a listing would self-heal it and hide the bug.
    track(parent, "proj_test", AgentCatOptions(enable_report_missing=True))
    track(parent, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_community_test_client(parent) as client:
        listed = await client.list_tools()
        assert [t.description for t in listed] == [
            "The customer's own tool, supplied by a mounted sub-server."
        ]
        result = await client.call_tool("get_more_tools", {"context": "mine"})

    assert "customer answered: mine" in _text(result)
    assert "Unfortunately" not in _text(result)


async def test_a_call_before_any_listing_concedes_get_more_tools(capture):
    """rebuild-on-demand reads the same all-provider view, so the case the
    eager registration exists for is also the case it must not hijack.

    Driven through `server.call_tool`, which runs the middleware chain without
    a tools/list: a real FastMCP client fetches output schemas on its way to a
    call, so it can never reproduce a genuinely un-listed instance.
    """
    parent = _new_server("parent")
    sub = _new_server("sub")

    @sub.tool
    def get_more_tools(context: str) -> str:
        """The customer's own tool, supplied by a mounted sub-server."""
        return f"customer answered: {context}"

    parent.mount(sub)
    track(parent, "proj_test", AgentCatOptions(enable_report_missing=True))

    result = await parent.call_tool("get_more_tools", {"context": "mine"})

    assert "customer answered: mine" in _text(result)
    assert "Unfortunately" not in _text(result)
    assert _call_events(capture)[0].resource_name == "get_more_tools"


async def test_a_middleware_that_copies_tools_keeps_get_more_tools_working(capture):
    """A layer below us handing back `model_copy` results must not make our own
    tool read as a customer's.

    Detecting ours by object identity alone did: the copy looked foreign, ours
    was un-registered, and because the copy was not the object being filtered
    it stayed in the listing — advertising a `get_more_tools` whose very next
    call raised `Unknown tool`.
    """
    from fastmcp.server.middleware import Middleware

    class Copier(Middleware):
        async def on_list_tools(self, context, call_next):
            return [t.model_copy(update={}) for t in await call_next(context)]

    server = _new_server("copy-server")

    @server.tool
    def add_todo(text: str) -> str:
        """Add a todo."""
        return f"added {text}"

    server.add_middleware(Copier())
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_community_test_client(server) as client:
        first = sorted(t.name for t in await client.list_tools())
        assert first == ["add_todo", "get_more_tools"]

        # The listing advertised it, so it has to answer.
        result = await client.call_tool("get_more_tools", {"context": "why"})
        assert "Unfortunately" in _text(result)

        # ...and it is still advertised on the next listing.
        assert sorted(t.name for t in await client.list_tools()) == first

    names = [t.name for t in await server.list_tools(run_middleware=False)]
    assert "get_more_tools" in names, "our tool was un-registered on a false positive"


async def test_a_middleware_that_rebuilds_tools_does_not_lose_get_more_tools(capture):
    """The authoritative re-check, and the only shape that needs it.

    `_is_ours` recognizes a copy of our tool three ways: object identity, the
    underlying `fn` a `model_copy` carries over, and our canonical description.
    A layer that REBUILDS each tool with `Tool.from_function` and a description
    of its own defeats all three — and that is not exotic: it is what any
    middleware that re-stamps or re-documents a listing does.

    Ours then reads as a foreign `get_more_tools` in the processed listing.
    Conceding on that would un-register the real tool while the rebuilt copy
    stays advertised, so the very next call to it raises `Unknown tool`. The
    re-check asks the RAW provider listing — where our own object is present
    and recognizable — and answers "nobody else supplies this".
    """
    from fastmcp.server.middleware import Middleware
    from fastmcp.tools import Tool

    class Rebuilder(Middleware):
        """Hands back tools it built itself, not copies of the originals."""

        async def on_list_tools(self, context, call_next):
            async def rebuilt_body(context: str = "") -> str:
                return "rebuilt"

            return [
                Tool.from_function(
                    rebuilt_body,
                    name=tool.name,
                    description=f"rebuilt: {tool.description}",
                ).model_copy(update={"parameters": tool.parameters})
                for tool in await call_next(context)
            ]

    server = _new_server("rebuilding-server")

    @server.tool
    def add_todo(text: str) -> str:
        """Add a todo."""
        return f"added {text}"

    server.add_middleware(Rebuilder())
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        assert sorted(t.name for t in listed) == ["add_todo", "get_more_tools"]
        # Nothing in that listing is recognizable as ours any more.
        assert all(t.description.startswith("rebuilt: ") for t in listed)

        # The listing advertised it, so it has to answer — and the answer is
        # ours, from the provider, because ours is still registered.
        result = await client.call_tool("get_more_tools", {"context": "why"})
        assert "Unfortunately" in _text(result)

    names = [t.name for t in await server.list_tools(run_middleware=False)]
    assert "get_more_tools" in names, "ours was un-registered on a false positive"


async def test_a_copying_middleware_still_concedes_to_a_real_owner(capture):
    """The copy tolerance must not blind the concession: with a genuine
    customer tool present, ours goes even though both arrive as copies."""
    from fastmcp.server.middleware import Middleware

    class Copier(Middleware):
        async def on_list_tools(self, context, call_next):
            return [t.model_copy(update={}) for t in await call_next(context)]

    parent = _new_server("parent")
    sub = _new_server("sub")

    @sub.tool
    def get_more_tools(context: str) -> str:
        """The customer's own tool, supplied by a mounted sub-server."""
        return f"customer answered: {context}"

    parent.mount(sub)
    parent.add_middleware(Copier())
    track(parent, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_community_test_client(parent) as client:
        listed = await client.list_tools()
        # Exactly one — ours was dropped from the list even as a copy.
        assert [t.description for t in listed] == [
            "The customer's own tool, supplied by a mounted sub-server."
        ]
        result = await client.call_tool("get_more_tools", {"context": "mine"})

    assert "customer answered: mine" in _text(result)


async def test_conceding_get_more_tools_never_deletes_the_customers_tool(capture):
    """The un-registration is by object identity, so a customer who registers
    their own on the LOCAL provider after track() keeps it."""
    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    # Replaces ours in the local store under the same key (on_duplicate=warn).
    @server.tool
    def get_more_tools(context: str) -> str:
        """The customer's own tool, registered after track()."""
        return f"customer answered: {context}"

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        assert [t.name for t in listed].count("get_more_tools") == 1
        result = await client.call_tool("get_more_tools", {"context": "mine"})

    assert "customer answered: mine" in _text(result)
    names = [t.name for t in await server.list_tools(run_middleware=False)]
    assert names.count("get_more_tools") == 1


async def test_an_unreadable_tool_registry_skips_registration(capture, monkeypatch):
    """Registration is gated positively: only a registry we could actually read
    and found clean lets us add our tool over the customer's server."""
    from agentcat.modules.adapters import community

    logged: list[str] = []
    monkeypatch.setattr(community, "write_to_log", logged.append)

    server = create_community_todo_server()
    monkeypatch.setattr(server, "_local_provider", None, raising=False)
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()

    assert any(t.name == "add_todo" for t in listed), "the server is still serving"
    assert not any(t.name == "get_more_tools" for t in listed)
    assert any("could not read the tool registry" in line for line in logged), logged


async def test_agent_tracking_injection(capture):
    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_agent_tracking=True))

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        add = _named(listed, "add_todo")
        assert list(add.inputSchema["properties"])[-3:] == [
            "session_id",
            "agent_id",
            "context",
        ]
        assert "agent_id" in add.inputSchema["required"]
        assert "session_id" in add.inputSchema["required"]

        result = await client.call_tool(
            "add_todo", {"text": "with agent", "agent_id": "opus|claude-code|k3n9x"}
        )
        assert result.is_error is False, _text(result)

    event = _call_events(capture)[0]
    assert event.tags[AGENTCAT_TAG_AGENT_ID] == "opus|claude-code|k3n9x"
    assert event.tags[AGENTCAT_TAG_AGENT_SOURCE] == "supplied"


async def test_hook_mode(capture):
    server = create_community_todo_server()
    track(
        server,
        "proj_test",
        AgentCatOptions(resolve_session_id=lambda request, extra: "cust-1"),
    )

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        assert listed
        for tool in listed:
            assert "session_id" not in tool.inputSchema.get("properties", {})

        r1 = await client.call_tool("add_todo", {"text": "hook one"})
        r2 = await client.call_tool("add_todo", {"text": "hook two"})
        assert "[session_id" not in _text(r1)
        assert "[session_id" not in _text(r2)
        assert MCP_SESSION_KEY not in (r1.structured_content or {})

    call_events = _call_events(capture)
    expected = derive_session_id("cust-1", "proj_test")
    assert [e.session_id for e in call_events] == [expected, expected]
    assert call_events[0].tags[AGENTCAT_TAG_SESSION_SOURCE] == "hook"


async def test_hook_receives_the_live_request_every_round(capture):
    """A ResolvedCall is single-round state: the hook must see each round's own
    message, never a cached first-round one."""
    seen: list = []

    def hook(request, extra):
        seen.append(getattr(request, "arguments", None))
        return "cust-1"

    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions(resolve_session_id=hook))

    async with create_community_test_client(server) as client:
        await client.call_tool("add_todo", {"text": "first"})
        await client.call_tool("add_todo", {"text": "second"})

    assert [args.get("text") for args in seen] == ["first", "second"]


async def test_tracing_disabled_strips_but_publishes_nothing(capture):
    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_tracing=False))

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        add = _named(listed, "add_todo")
        # No handles when tracing is off, but the context parameter is
        # independent — so it must still be stripped before the tool runs.
        assert "session_id" not in add.inputSchema["properties"]
        assert "context" in add.inputSchema["properties"]

        result = await client.call_tool(
            "add_todo", {"text": "quiet", "context": "no tracing"}
        )
        assert result.is_error is False, _text(result)
        assert "[session_id" not in _text(result)

    assert capture == []


async def test_resolution_failure_degrades_to_an_untraced_call(capture, monkeypatch):
    """A tool call must never fail because analytics did — and the injected
    parameters are still stripped on the way down."""
    from agentcat.modules.adapters import community

    async def boom(*args, **kwargs):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(community, "resolve_call", boom)

    server = create_community_todo_server()
    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        await client.list_tools()
        result = await client.call_tool(
            "add_todo", {"text": "degraded", "context": "why", "session_id": sid("x")}
        )

    assert result.is_error is False, _text(result)
    assert "Added todo" in _text(result)
    assert capture == []


async def test_customer_tool_schema_is_never_mutated(capture):
    """Injection works on copies: the server's own tool definitions and a
    second, untracked server built the same way stay identical."""
    server = create_community_todo_server()
    reference = create_community_todo_server()
    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        await client.list_tools()
        await client.list_tools()

    tracked = {
        t.name: t.parameters for t in await server.list_tools(run_middleware=False)
    }
    untracked = {
        t.name: t.parameters for t in await reference.list_tools(run_middleware=False)
    }
    assert {k: v for k, v in tracked.items() if k != "get_more_tools"} == untracked


async def test_tool_error_publishes_the_event_and_reraises(capture):
    """FastMCP surfaces a failing tool as a raised ToolError, so unlike the
    official adapters this path holds the live exception: the community error
    payload keeps its type, its traceback and the cause chain, and the error
    itself reaches the client untouched."""
    from fastmcp.exceptions import ToolError

    server = create_community_todo_server()
    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        await client.list_tools()
        with pytest.raises(ToolError):
            await client.call_tool("complete_todo", {"id": 999})

    event = _call_events(capture)[0]
    assert event.is_error is True
    assert event.session_id.startswith("ses_")
    assert "Todo with ID 999 not found" in event.error["message"]
    assert event.error["type"] == "ToolError"
    assert event.error["platform"] == "python"
    assert event.error["frames"], "the live exception's traceback was lost"
    assert "ValueError" in [c["type"] for c in event.error["chained_errors"]]
    assert event.duration is not None and event.duration >= 0


async def test_a_tool_returning_is_error_records_the_surfaced_message(capture):
    """A tool that returns is_error instead of raising never had an exception
    to tap, so the payload is the SDK-wide no-tap shape built from the wire
    result — not a pydantic repr of FastMCP's ToolResult."""
    from fastmcp.server.middleware import Middleware
    from mcp.types import TextContent

    from ..test_utils import error_tool_result

    class Failing(Middleware):
        async def on_call_tool(self, context, call_next):
            return error_tool_result(
                content=[TextContent(type="text", text="the tool said no")],
                is_error=True,
            )

    server = _new_server("is-error-server")

    @server.tool(output_schema=None)
    def flaky() -> str:
        """Answered by the middleware below AgentCat."""
        return "unused"

    server.add_middleware(Failing())
    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        result = await client.call_tool("flaky", {}, raise_on_error=False)

    assert result.is_error is True
    event = _call_events(capture)[0]
    assert event.is_error is True
    assert event.error["message"] == "the tool said no"
    assert event.error["type"] is None
    assert event.error["platform"] == "python"
    # An error result still carries the handle: the retry has to be the same
    # task.
    assert MINT_BACK_HEADER in _text(result)


async def test_retracking_updates_options_without_double_wrapping(capture):
    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_report_missing=False))
    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        assert [t.name for t in listed].count("get_more_tools") == 1
        add = _named(listed, "add_todo")
        assert list(add.inputSchema["properties"]) == ["text", "session_id", "context"]

        # A second, stacked middleware would inject session_id in the inner pass
        # and then skip it in the outer one — leaving it out of the strip
        # registry and handing the customer's tool a parameter it never
        # declared.
        result = await client.call_tool(
            "add_todo", {"text": "retracked", "session_id": sid("retrack")}
        )
        assert result.is_error is False, _text(result)

    assert len(_call_events(capture)) == 1
    assert _call_events(capture)[0].session_id == sid("retrack")


async def test_agentcat_middleware_is_outermost(capture):
    """Element 0 of `server.middleware` runs first, so a caching or
    dereferencing middleware below us keys on STRIPPED arguments and never
    caches our injection."""
    from fastmcp.server.middleware import Middleware

    below: dict = {}

    class Probe(Middleware):
        async def on_call_tool(self, context, call_next):
            below["arguments"] = dict(context.message.arguments or {})
            return await call_next(context)

        async def on_list_tools(self, context, call_next):
            tools = list(await call_next(context))
            below["schemas"] = {t.name: dict(t.parameters or {}) for t in tools}
            return tools

    server = create_community_todo_server()
    server.add_middleware(Probe())
    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        await client.list_tools()
        await client.call_tool(
            "add_todo", {"text": "layered", "context": "why", "session_id": sid("z")}
        )

    assert below["arguments"] == {"text": "layered"}
    assert "session_id" not in below["schemas"]["add_todo"]["properties"]


async def test_options_are_read_per_request_not_captured_at_install(capture):
    """The middleware re-reads the server's tracking data every request, so
    swapping it in place takes effect on the next call."""
    from dataclasses import replace

    from agentcat.modules.internal import (
        get_server_tracking_data,
        set_server_tracking_data,
    )

    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_agent_tracking=False))

    installed = get_server_tracking_data(server)
    set_server_tracking_data(
        server,
        replace(
            installed,
            options=AgentCatOptions(enable_agent_tracking=True),
            injected_params_registry=None,
            output_injection_registry=None,
        ),
    )

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        assert "agent_id" in _named(listed, "add_todo").inputSchema["properties"]
        result = await client.call_tool(
            "add_todo", {"text": "swapped", "agent_id": "opus|cc|abc12"}
        )

    assert result.is_error is False, _text(result)
    assert _call_events(capture)[0].tags[AGENTCAT_TAG_AGENT_ID] == "opus|cc|abc12"


# ── the handshake rung (design §7, rung 3) ──────────────────────────────────
#
# One middleware object serves every connection, so the rung that remembers
# what `initialize` said has to be filed per connection or it will rename other
# people's calls. The doubles below reproduce the shapes real FastMCP v3 hands
# the middleware, verified against a live server on both transports:
#
#   stateful   initialize: fastmcp_context.request_context is None, .session=S
#              tools/call: request_context.session is the SAME S
#   stateless  every request gets a NEW session, and it carries no
#              client_params — so the ladder always reaches this rung
#
# `client_params = None` on the double is the stateless shape on purpose: with
# it set, rung 3a answers first and this rung is never exercised at all.


class _FakeSession:
    """A weak-referenceable ServerSession stand-in with no handshake recorded."""

    client_params = None
    session_id = None


class _FakeRequestContext:
    def __init__(self, session: object) -> None:
        self.session = session
        self.request = None
        self.request_id = None
        self.meta = None


class _FakeConnection:
    """One connection's FastMCP `Context`, before and during a request."""

    def __init__(self) -> None:
        self.session = _FakeSession()
        self.request_context = None
        self.session_id = None

    def in_request(self) -> "_FakeConnection":
        self.request_context = _FakeRequestContext(self.session)
        return self


async def _ok_result(_ctx):
    from fastmcp.tools import ToolResult
    from mcp.types import TextContent

    return ToolResult(content=[TextContent(type="text", text="ok")])


async def _handshake(middleware, connection, name: str, version: str) -> None:
    from fastmcp.server.middleware import MiddlewareContext
    from mcp.types import Implementation, InitializeRequest, InitializeRequestParams

    initialize = InitializeRequest(
        method="initialize",
        params=InitializeRequestParams(
            protocolVersion="2025-06-18",
            capabilities={},
            clientInfo=Implementation(name=name, version=version),
        ),
    )
    await middleware(
        MiddlewareContext(
            message=initialize, method="initialize", fastmcp_context=connection
        ),
        _ok_result,
    )


async def _tool_call(middleware, connection, meta: dict | None = None) -> None:
    from fastmcp.server.middleware import MiddlewareContext
    from mcp.types import CallToolRequestParams

    params = CallToolRequestParams(name="add_todo", arguments={"text": "x"})
    if meta is not None:
        params = params.model_copy(update={"meta": meta})
    await middleware(
        MiddlewareContext(
            message=params, method="tools/call", fastmcp_context=connection.in_request()
        ),
        _ok_result,
    )


async def test_handshake_client_info_is_the_last_rung_of_the_ladder(capture):
    """initialize publishes nothing, but what it captures still names the
    client when no per-request source does (design §7, rung 3).

    It is the LAST rung on purpose: a per-request source always wins.
    """
    server = create_community_todo_server()
    track(server, "proj_test")
    middleware = server.middleware[0]

    connection = _FakeConnection()
    await _handshake(middleware, connection, "Cursor", "2.6.22")

    await _tool_call(middleware, connection)
    assert _call_events(capture)[-1].client_name == "Cursor"
    assert _call_events(capture)[-1].client_version == "2.6.22"

    await _tool_call(
        middleware,
        connection,
        meta={META_CLIENT_INFO_KEY: {"name": "Claude", "version": "1.0.0"}},
    )
    assert _call_events(capture)[-1].client_name == "Claude"
    assert _call_events(capture)[-1].client_version == "1.0.0"


async def test_a_later_handshake_never_renames_an_earlier_connections_call(capture):
    """Two clients, one middleware: B's initialize must not rename A's call.

    A single "last seen" slot published Cursor's tool call as Claude on every
    stateless-HTTP server — the session there carries no client_params, so the
    ladder reaches this rung on every call, and whichever client connected most
    recently owned it. The 1.x `stateless` option was the only guard against
    that, and it is gone, so the rung is filed per connection instead.
    """
    server = create_community_todo_server()
    track(server, "proj_test")
    middleware = server.middleware[0]

    first, second = _FakeConnection(), _FakeConnection()
    await _handshake(middleware, first, "Cursor", "2.6.22")
    await _handshake(middleware, second, "Claude", "1.0.0")

    await _tool_call(middleware, first)
    assert _call_events(capture)[-1].client_name == "Cursor"
    assert _call_events(capture)[-1].client_version == "2.6.22"

    await _tool_call(middleware, second)
    assert _call_events(capture)[-1].client_name == "Claude"
    assert _call_events(capture)[-1].client_version == "1.0.0"


async def test_a_connection_that_never_handshook_gets_no_client_name(capture):
    """The stateless-HTTP shape: a fresh session per request, none of which
    handshook. The honest answer is "unknown", never the last name we saw."""
    server = create_community_todo_server()
    track(server, "proj_test")
    middleware = server.middleware[0]

    await _handshake(middleware, _FakeConnection(), "Cursor", "2.6.22")

    await _tool_call(middleware, _FakeConnection())
    assert _call_events(capture)[-1].client_name is None
    assert _call_events(capture)[-1].client_version is None


async def test_strip_preserves_the_rest_of_the_request_message(capture):
    """The stripped message is a copy of the customer's, not a rebuilt
    `CallToolRequestParams`: rebuilding drops `_meta` (and, in the 2026 era,
    `input_responses`/`request_state`), which severs MRTR continuations."""
    from fastmcp.server.middleware import MiddlewareContext
    from fastmcp.tools import ToolResult
    from mcp.types import CallToolRequestParams, TextContent

    server = create_community_todo_server()
    track(server, "proj_test")
    middleware = server.middleware[0]

    seen: dict = {}

    async def call_next(ctx):
        seen["message"] = ctx.message
        return ToolResult(content=[TextContent(type="text", text="ok")])

    message = CallToolRequestParams(
        name="add_todo",
        arguments={"text": "hi", "context": "why"},
        _meta={"trace": "abc"},
    )
    await middleware(
        MiddlewareContext(message=message, method="tools/call"), call_next
    )

    assert seen["message"].arguments == {"text": "hi"}
    assert getattr(seen["message"].meta, "trace", None) == "abc"
    assert seen["message"].meta is message.meta
    # The customer's own message object is untouched.
    assert message.arguments == {"text": "hi", "context": "why"}


@pytest.mark.parametrize("flavor", ["by_type_name", "by_result_type"])
async def test_intermediate_mrtr_round_is_tagged_but_never_decorated(capture, flavor):
    """A round that asks the client for more input is not the completing round,
    so it carries no mint-back — text or structured."""
    from fastmcp.server.middleware import Middleware
    from fastmcp.tools import ToolResult
    from mcp.types import TextContent

    class InputRequiredToolResult(ToolResult):
        pass

    class LaterEraToolResult(ToolResult):
        result_type: str = "input_required"

    cls = InputRequiredToolResult if flavor == "by_type_name" else LaterEraToolResult

    class Intermediate(Middleware):
        async def on_call_tool(self, context, call_next):
            return cls(content=[TextContent(type="text", text="need more")])

    server = _new_server("mrtr-server")

    @server.tool(output_schema=None)
    def ask(text: str) -> str:
        """Needs another round."""
        return text

    server.add_middleware(Intermediate())
    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        result = await client.call_tool("ask", {"text": "round one"})

    assert [block.text for block in result.content] == ["need more"]
    assert result.structured_content is None
    event = _call_events(capture)[0]
    assert event.tags["agentcat_mrtr"] == "input_required"
    assert event.session_id.startswith("ses_")


async def test_tools_added_after_track_are_injected_and_tracked(capture):
    """track() may run before a single tool exists: the middleware reads the
    provider's listing per request, so late registrations are covered."""
    server = _new_server("late-server")
    track(server, "proj_test")

    @server.tool
    def late(value: str) -> str:
        """Registered after track()."""
        return f"late:{value}"

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        assert "session_id" in _named(listed, "late").inputSchema["properties"]

        result = await client.call_tool(
            "late", {"value": "v", "session_id": sid("late")}
        )

    assert "late:v" in _text(result)
    assert _call_events(capture)[0].resource_name == "late"
    assert _call_events(capture)[0].session_id == sid("late")


async def test_a_call_before_any_listing_rebuilds_the_strip_registry(capture):
    """tools/call may land on an instance that never served tools/list; the
    rebuild reads the server's own listing so the strip still matches."""
    seen: dict = {}
    server = _new_server("rebuild-server")

    @server.tool
    def probe(text: str) -> str:
        """Would fail validation if handed an argument it never declared."""
        seen["text"] = text
        return "ok"

    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        # No list_tools first.
        result = await client.call_tool(
            "probe", {"text": "t", "session_id": sid("rb"), "context": "why"}
        )

    assert result.is_error is False, _text(result)
    assert seen == {"text": "t"}
    assert _call_events(capture)[0].session_id == sid("rb")


async def test_tool_with_its_own_context_parameter_is_left_alone(capture):
    """A customer parameter named `context` is theirs: it is neither
    re-described nor stripped."""
    seen: dict = {}
    server = _new_server("context-server")

    @server.tool
    def search(query: str, context: str) -> str:
        """A tool whose own API already has a context argument."""
        seen.update(query=query, context=context)
        return "found"

    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        schema = _named(listed, "search").inputSchema
        assert "description" not in schema["properties"]["context"]
        assert schema["required"] == ["query", "context", "session_id"]

        await client.call_tool("search", {"query": "q", "context": "theirs"})

    assert seen == {"query": "q", "context": "theirs"}
    # It is still read as the intent — the event records what the agent sent.
    assert _call_events(capture)[0].user_intent == "theirs"


async def test_two_tracked_servers_stay_isolated(capture):
    """Registries and project IDs are per-server; one server's listing never
    settles another's strip."""
    first = _new_server("first")
    second = _new_server("second")

    @first.tool
    def alpha(a: str) -> str:
        """First server's tool."""
        return "alpha"

    @second.tool
    def beta(b: str) -> str:
        """Second server's tool."""
        return "beta"

    track(first, "proj_one", AgentCatOptions(enable_report_missing=False))
    track(second, "proj_two", AgentCatOptions(enable_agent_tracking=True))

    async with create_community_test_client(first) as client:
        listed = await client.list_tools()
        assert "agent_id" not in _named(listed, "alpha").inputSchema["properties"]
        await client.call_tool("alpha", {"a": "1", "session_id": sid("one")})

    async with create_community_test_client(second) as client:
        listed = await client.list_tools()
        assert "agent_id" in _named(listed, "beta").inputSchema["properties"]
        await client.call_tool("beta", {"b": "2", "session_id": sid("two")})

    events = _call_events(capture)
    assert [e.resource_name for e in events] == ["alpha", "beta"]
    assert [e.session_id for e in events] == [sid("one"), sid("two")]
    assert [e.project_id for e in events] == ["proj_one", "proj_two"]


async def test_event_carries_server_and_sdk_metadata(capture):
    server = create_community_todo_server()
    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        await client.call_tool("add_todo", {"text": "meta"})

    event = _call_events(capture)[0]
    assert event.server_name == "todo-server"
    assert event.sdk_language.startswith("Python ")
    assert event.duration is not None and event.duration >= 0


def test_a_tracked_server_is_collectable_once_the_customer_drops_it():
    """Nothing AgentCat holds may outlive the server (changelog §6.8).

    The community adapter keeps no module-level per-server map — the middleware
    lives in the server's own `middleware` list, and its per-connection
    handshake cache is weakly keyed on sessions by values that do not reference
    them — so this is a guard against that changing, and the community half of
    the same audit the official adapters carry.
    """
    import gc
    import weakref

    alive = []
    for _ in range(3):
        server = create_community_todo_server()
        track(server, "proj_test")
        alive.append(weakref.ref(server))
        del server

    gc.collect()
    assert [ref() for ref in alive] == [None, None, None]
