"""Handle behavior on the community FastMCP adapter, FastMCP 4 era.

The 4.x sibling of `tests/community/test_community_v3_handles.py`. Task 9 built
one middleware for both eras and Task 13 turned era 4 on, so this file does not
re-prove the shared behavior the v3 file already covers — it covers what only
FastMCP 4 can reach:

- the era's own dispatch, which runs the middleware chain a SECOND time for a
  component request that failed before the interior chain ran, handing the
  hooks a raw params **mapping** instead of a typed model;
- `DereferenceRefsMiddleware`, which FastMCP 4 installs on every server by
  default and which the `get_more_tools` concession must survive;
- `ResponseCachingMiddleware`, which proves index 0 is load-bearing rather than
  incidental;
- real multi-round-trip tool calls — `InputRequiredToolResult` and the
  `input_responses` / `request_state` continuation envelope are first-class on
  this era, not a simulation;
- the four TS-parity behaviors, which the 3.x suite proves but cannot prove
  HERE: `tests/community/` is collected only under mcp 1.x, so nothing in it
  has ever run against fastmcp 4.

Most of it rides a real `fastmcp.Client` against a real server. The three
tests that call the middleware directly do so because no client can reach what
they assert: FastMCP's own dispatch is what hands the hooks a raw mapping, and
a client that receives an `InputRequiredToolResult` answers it and moves on
rather than handing the round back for inspection.
"""

import copy

import mcp.types as mt
import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import (
    AGENTCAT_TAG_AGENT_ID,
    AGENTCAT_TAG_MRTR,
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_INSTRUCTIONS_KEY,
    SESSION_ID_PARAM,
)
from agentcat.modules.detection import ServerFlavor, detect_server

from .test_utils import sid
from .test_utils.community_client import (
    HAS_COMMUNITY_CLIENT,
    create_community_test_client,
)
from .test_utils.community_openapi_server import (
    OPENAPI_TOOL_NAMES,
    create_community_openapi_server,
)
from .test_utils.community_todo_server import (
    HAS_COMMUNITY_FASTMCP,
    create_community_todo_server,
)

pytestmark = pytest.mark.skipif(
    not (HAS_COMMUNITY_FASTMCP and HAS_COMMUNITY_CLIENT),
    reason="Community FastMCP not available",
)

MINT_BACK_HEADER = "[MCP INSTRUCTIONS]: session_id issued."


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


def _agentcat_middleware(server):
    from agentcat.modules.adapters.community import AgentCatMiddleware

    return [mw for mw in server.middleware if isinstance(mw, AgentCatMiddleware)]


# ── the era is recognized and routed ────────────────────────────────────────


def test_track_installs_the_community_middleware_at_index_zero():
    """`track()` routes COMMUNITY_V4 to the community adapter with era 4.

    The flavor assert is the precondition every other test here rests on —
    `tests/test_detection.py` proves the classifier, this proves what `track()`
    does with its answer.

    Index 0 is outermost — FastMCP builds its chain over `reversed(middleware)`
    — and on this era the list is never empty to begin with:
    `DereferenceRefsMiddleware` is installed by default, so "insert at 0" and
    "append" are visibly different placements from the very first track().
    """
    from agentcat.modules.adapters.community import ERA_V4

    server = create_community_todo_server()
    assert detect_server(server).flavor is ServerFlavor.COMMUNITY_V4
    assert server.middleware, "FastMCP 4 ships a default middleware chain"

    track(server, "proj_test")

    installed = _agentcat_middleware(server)
    assert len(installed) == 1
    assert server.middleware[0] is installed[0]
    assert installed[0]._era == ERA_V4


# ── the four TS-parity behaviors, executed on this era ──────────────────────
#
# The middleware does not branch on the era, so these four are the same code
# the 3.x suite proves. They are re-run here anyway because `tests/community/`
# is collected ONLY under mcp 1.x (`tests/conftest.py::_LEGACY_ONLY`), so
# nothing in that directory has ever executed against fastmcp 4 — "shared code"
# is an argument about risk, not a substitute for running it.


async def test_retracking_updates_options_without_stacking_a_middleware(capture):
    """A repeated `track()` replaces the middleware; it never adds a second.

    A stacked pass would inject session_id on the inside, find it already present
    on the outside, never record it as strippable, and hand the customer's tool
    a parameter it never declared. The second track() also carries different
    options, so this pins the replacement AND that the new options are the ones
    serving requests.
    """
    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_report_missing=False))
    track(
        server,
        "proj_test",
        AgentCatOptions(enable_report_missing=True, enable_agent_tracking=True),
    )

    installed = _agentcat_middleware(server)
    assert len(installed) == 1, "a second track() stacked another middleware"
    assert server.middleware[0] is installed[0], "the replacement left index 0"

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        # Both option changes are live: the tool the first track() suppressed is
        # now advertised, and agent tracking is now injecting.
        assert [t.name for t in listed].count("get_more_tools") == 1
        add = _named(listed, "add_todo")
        assert list(add.input_schema["properties"]) == [
            "text",
            SESSION_ID_PARAM,
            "agent_id",
            "context",
        ]

        result = await client.call_tool(
            "add_todo",
            {
                "text": "retracked",
                SESSION_ID_PARAM: sid("retrack"),
                "agent_id": "o|cc|k3n9x",
            },
        )
        assert result.is_error is False, _text(result)

    events = _call_events(capture)
    assert len(events) == 1, "a stacked middleware published the call twice"
    assert events[0].session_id == sid("retrack")
    assert events[0].tags[AGENTCAT_TAG_AGENT_ID] == "o|cc|k3n9x"


async def test_options_are_read_per_request_not_captured_at_install(capture):
    """The middleware re-reads the server's tracking data every request.

    Distinct from the re-track above, which installs a fresh middleware object:
    here the data is swapped in place and the SAME middleware has to pick it up,
    which is the only thing that proves the lookup is per request rather than
    captured in `__init__`.
    """
    from dataclasses import replace

    from agentcat.modules.internal import (
        get_server_tracking_data,
        set_server_tracking_data,
    )

    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions(enable_agent_tracking=False))
    middleware = server.middleware[0]

    set_server_tracking_data(
        server,
        replace(
            get_server_tracking_data(server),
            options=AgentCatOptions(enable_agent_tracking=True),
            injected_params_registry=None,
            output_injection_registry=None,
        ),
    )

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        assert "agent_id" in _named(listed, "add_todo").input_schema["properties"]
        result = await client.call_tool(
            "add_todo", {"text": "swapped", "agent_id": "o|cc|abc12"}
        )

    assert server.middleware[0] is middleware, "the middleware was reinstalled"
    assert result.is_error is False, _text(result)
    assert _call_events(capture)[0].tags[AGENTCAT_TAG_AGENT_ID] == "o|cc|abc12"


async def test_tracing_disabled_strips_but_publishes_nothing(capture):
    """Tracing off is not injection off.

    No handles are injected and nothing is published, but `context` is an
    independent option — so it is still advertised, and it must still be
    stripped before the customer's tool runs or the call fails validation.
    """
    seen: dict = {}
    server = _new_server("quiet-server")

    @server.tool
    def probe(text: str) -> str:
        """Would fail validation if handed an argument it never declared."""
        seen["text"] = text
        return f"probe:{text}"

    track(server, "proj_test", AgentCatOptions(enable_tracing=False))

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        schema = _named(listed, "probe").input_schema
        assert SESSION_ID_PARAM not in schema["properties"]
        assert "context" in schema["properties"]

        result = await client.call_tool(
            "probe", {"text": "quiet", "context": "no tracing"}
        )

    assert result.is_error is False, _text(result)
    assert seen == {"text": "quiet"}
    assert MINT_BACK_HEADER not in _text(result)
    assert capture == []


async def test_resolution_failure_degrades_to_an_untraced_call(capture, monkeypatch):
    """A tool call must never fail because analytics did.

    The injected parameters are stripped on the way DOWN, before resolution is
    attempted, so the degrade path still hands the customer's tool a clean
    argument set rather than failing its validation.
    """
    from agentcat.modules.adapters import community

    seen: dict = {}

    async def boom(*args, **kwargs):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(community, "resolve_call", boom)

    server = _new_server("degrade-server")

    @server.tool
    def probe(text: str) -> str:
        """Would fail validation if handed an argument it never declared."""
        seen["text"] = text
        return f"probe:{text}"

    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        await client.list_tools()
        result = await client.call_tool(
            "probe",
            {"text": "degraded", "context": "why", SESSION_ID_PARAM: sid("x")},
        )

    assert result.is_error is False, _text(result)
    assert "probe:degraded" in _text(result)
    assert seen == {"text": "degraded"}
    assert capture == []


# ── injection, mint-back and echo ───────────────────────────────────────────


async def test_prompted_mode_end_to_end(capture):
    server = create_community_todo_server()
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        add = _named(listed, "add_todo")
        tail = list(add.input_schema["properties"])[-2:]
        assert tail == [SESSION_ID_PARAM, "context"]
        assert SESSION_ID_PARAM not in add.input_schema.get("required", [])
        assert MCP_INSTRUCTIONS_KEY in add.output_schema["properties"]
        assert any(t.name == "get_more_tools" for t in listed)

        r1 = await client.call_tool(
            "add_todo", {"text": "hi", "context": "tracking the user's work"}
        )
        text = _text(r1)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id=")[1].split(" ")[0]
        assert minted.startswith("ses_")
        assert r1.structured_content[MCP_INSTRUCTIONS_KEY]["session_id"] == minted

        r2 = await client.call_tool(
            "add_todo", {"text": "again", SESSION_ID_PARAM: minted}
        )
        assert MINT_BACK_HEADER not in _text(r2)

    # v2 publishes tools/call and nothing else.
    assert {e.event_type for e in capture} == {"mcp:tools/call"}
    events = _call_events(capture)
    assert [e.session_id for e in events] == [minted, minted]
    assert [e.tags[AGENTCAT_TAG_SESSION_SOURCE] for e in events] == [
        "minted",
        "supplied",
    ]
    # The event records the call as the agent made it: raw arguments, and the
    # customer's own undecorated result.
    assert events[0].parameters["arguments"]["context"]
    assert MINT_BACK_HEADER not in str(events[0].response)


async def test_handler_sees_stripped_args(capture):
    """The injected parameters never reach the customer's tool body."""
    seen: dict = {}
    server = _new_server()

    @server.tool
    def probe(text: str) -> str:
        """A tool that would fail validation if handed an extra argument."""
        seen["text"] = text
        return f"probe:{text}"

    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        result = await client.call_tool(
            "probe",
            {"text": "payload", SESSION_ID_PARAM: sid("supplied"), "context": "why"},
        )

    assert result.is_error is False, _text(result)
    assert seen == {"text": "payload"}
    assert _call_events(capture)[0].session_id == sid("supplied")


# ── multi round-trip tool calls (SEP-2322) ─────────────────────────────────


async def test_strip_preserves_the_mrtr_continuation_envelope(capture):
    """The stripped message is a COPY of the customer's, not a rebuilt
    `CallToolRequestParams`.

    Rebuilding from `(name, arguments)` — what the v1 middleware did — drops
    `_meta` and, on this era, `input_responses` / `request_state`. A tool that
    asked for input would then be handed a continuation with no responses and
    no state, and the round-trip would restart forever.
    """
    from fastmcp.server.middleware import MiddlewareContext
    from fastmcp.tools import ToolResult
    from mcp.types import CallToolRequestParams, ElicitResult, TextContent

    server = create_community_todo_server()
    track(server, "proj_test")
    middleware = server.middleware[0]

    seen: dict = {}

    async def call_next(ctx):
        seen["message"] = ctx.message
        return ToolResult(content=[TextContent(type="text", text="ok")])

    responses = {"r1": ElicitResult(action="accept", content={"answer": "yes"})}
    message = CallToolRequestParams(
        name="add_todo",
        arguments={"text": "hi", "context": "why"},
        _meta={"trace": "abc"},
        input_responses=responses,
        request_state="opaque-state",
    )
    await middleware(
        MiddlewareContext(message=message, method="tools/call"), call_next
    )

    delivered = seen["message"]
    assert delivered.arguments == {"text": "hi"}
    assert delivered.input_responses == responses
    assert delivered.request_state == "opaque-state"
    assert delivered.meta is message.meta
    # The customer's own message object is untouched.
    assert message.arguments == {"text": "hi", "context": "why"}
    # A round carrying inputResponses is a continuation (changelog §6.4).
    assert _call_events(capture)[0].tags[AGENTCAT_TAG_MRTR] == "continuation"


async def test_input_required_round_is_tagged_but_never_decorated(capture):
    """`InputRequiredToolResult` is FastMCP 4's real ask-for-input result.

    It is not the completing round, so it carries no mint-back — and it must
    come back as the very object the layer below produced. Its own docstring
    warns that `content` / `structured_content` carry nothing on this subclass,
    so a decorated copy would write a mint-back where the wire handler never
    looks; identity is the assertion because it is the only one that fails for
    a copy that happens to look right.
    """
    from fastmcp.server.middleware import MiddlewareContext

    ask = _input_required_result("s1")

    server = _new_server("mrtr-server")

    @server.tool(output_schema=None)
    def needs_input(text: str) -> str:
        """Answered by the layer below AgentCat."""
        return text

    track(server, "proj_test")

    async def call_next(ctx):
        return ask

    result = await server.middleware[0](
        MiddlewareContext(
            message=mt.CallToolRequestParams(
                name="needs_input", arguments={"text": "round one"}
            ),
            method="tools/call",
        ),
        call_next,
    )

    assert result is ask, "the intermediate round was copied or decorated"
    event = _call_events(capture)[0]
    assert event.tags[AGENTCAT_TAG_MRTR] == "input_required"
    assert event.session_id.startswith("ses_")


def _input_required_result(state: str):
    from fastmcp.tools import InputRequiredToolResult

    return InputRequiredToolResult(mt.InputRequiredResult(request_state=state))


async def test_a_real_client_drives_a_full_input_required_round_trip(capture):
    """Round one asks for state, round two completes — over a real client.

    Only the completing round is decorated, and only it may carry the
    mint-back: an agent that echoed a `session_id` off an intermediate round would
    be echoing one the server never issued.
    """
    rounds: list[str] = []
    server = _new_server("mrtr-e2e")

    @server.tool(output_schema=None)
    def guarded(text: str) -> str | mt.InputRequiredResult:
        """Asks for opaque state on the first round, completes on the second."""
        rounds.append(text)
        if len(rounds) == 1:
            return mt.InputRequiredResult(request_state="round-two-please")
        return f"completed: {text}"

    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        result = await client.call_tool("guarded", {"text": "hello"})

    assert rounds == ["hello", "hello"]
    assert "completed: hello" in _text(result)
    assert MINT_BACK_HEADER in _text(result)

    events = _call_events(capture)
    assert len(events) == 2
    assert events[0].tags[AGENTCAT_TAG_MRTR] == "input_required"
    # Round two answers with `requestState` and NO `inputResponses` — the tool
    # asked to be resumed rather than asking the client a question, so the
    # SEP-2322 driver retries it after a backoff with nothing else. It is still
    # a continuation, and keying the tag on `inputResponses` alone missed it.
    assert events[1].tags[AGENTCAT_TAG_MRTR] == "continuation"
    # The completing round is the one that mints, and its handle is the one the
    # agent was handed.
    minted = _text(result).split("session_id=")[1].split(" ")[0]
    assert events[1].session_id == minted


async def test_a_supplied_session_id_correlates_every_mrtr_round(capture):
    """The handle the agent supplied rides every round of the conversation.

    The SEP-2322 driver replays the ORIGINAL arguments verbatim on each retry
    (`mcp/client/_input_required.py`, reached through
    `fastmcp/client/mixins/tools.py`), so a `session_id` supplied on round one is
    on the wire for round two as well. This is the correlation changelog §6.4
    promises; the minted-first-call case is the only one that fragments, and
    every fix for it is either server-side state, which design §13 forbids, or
    a rewrite of the customer's own `requestState` — that one IS stateless and
    does work, but design §12 forbids altering what a customer's tool produced
    (see task 13.6's report §5).
    """
    rounds: list[str] = []
    server = _new_server("mrtr-supplied")

    @server.tool(output_schema=None)
    def guarded(text: str) -> str | mt.InputRequiredResult:
        """Asks for opaque state on the first round, completes on the second."""
        rounds.append(text)
        if len(rounds) == 1:
            return mt.InputRequiredResult(request_state="round-two-please")
        return f"completed: {text}"

    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        await client.call_tool(
            "guarded", {"text": "hello", SESSION_ID_PARAM: sid("supplied")}
        )

    # The injected parameter never reached the tool, on either round.
    assert rounds == ["hello", "hello"]
    events = _call_events(capture)
    assert [e.session_id for e in events] == [sid("supplied"), sid("supplied")]
    assert [e.tags[AGENTCAT_TAG_SESSION_SOURCE] for e in events] == [
        "supplied",
        "supplied",
    ]
    assert [e.tags.get(AGENTCAT_TAG_MRTR) for e in events] == [
        "input_required",
        "continuation",
    ]


async def test_hook_mode_correlates_every_mrtr_round(capture):
    """A `resolve_session_id` hook correlates the rounds on this era too.

    The hook runs per request against that round's own message/extra, and every
    round of one conversation is the same tool call on the same connection — so
    anything a hook keys on returns the same value and derives the same task.
    With `supplied` (above) that leaves prompted-mode MINTING as the only
    resolution mode an MRTR conversation fragments under, on BOTH modern eras.
    That claim is load-bearing input to the design question task 13.6 raised,
    so it is pinned on each era rather than inferred from the shared engine.
    """
    from agentcat.modules.handles import derive_session_id

    seen: list = []
    rounds: list[str] = []
    server = _new_server("mrtr-hook")

    @server.tool(output_schema=None)
    def guarded(text: str) -> str | mt.InputRequiredResult:
        """Asks for opaque state on the first round, completes on the second."""
        rounds.append(text)
        if len(rounds) == 1:
            return mt.InputRequiredResult(request_state="round-two-please")
        return f"completed: {text}"

    def hook(message, extra):
        seen.append(message)
        return "tenant"

    track(server, "proj_test", AgentCatOptions(resolve_session_id=hook))

    async with create_community_test_client(server) as client:
        await client.call_tool("guarded", {"text": "hello"})

    # Each round resolved afresh, against its own message.
    assert len(seen) == 2 and seen[0] is not seen[1]
    events = _call_events(capture)
    assert [e.tags[AGENTCAT_TAG_SESSION_SOURCE] for e in events] == ["hook", "hook"]
    derived = derive_session_id("tenant", "proj_test")
    assert [e.session_id for e in events] == [derived, derived]


# ── FastMCP 4's own middleware, above and below us ─────────────────────────


async def test_agentcat_is_outermost_of_the_response_cache(capture):
    """Index 0 is load-bearing, and `ResponseCachingMiddleware` is what proves it.

    Below us, the cache keys on the STRIPPED arguments and stores the
    customer's own result, so every call still reaches AgentCat: two identical
    calls publish two events and mint two different handles. Above us it would
    key on the raw arguments and cache OUR decorated result — the second agent
    would be handed the first agent's `session_id` and its call would never be
    recorded at all.
    """
    from fastmcp.server.middleware import Middleware
    from fastmcp.server.middleware.caching import ResponseCachingMiddleware

    below: dict = {}

    class Probe(Middleware):
        async def on_call_tool(self, context, call_next):
            below.setdefault("arguments", []).append(
                dict(context.message.arguments or {})
            )
            return await call_next(context)

        async def on_list_tools(self, context, call_next):
            tools = list(await call_next(context))
            below.setdefault("schemas", []).append(
                {t.name: set((t.parameters or {}).get("properties", {})) for t in tools}
            )
            return tools

    server = create_community_todo_server()
    server.add_middleware(ResponseCachingMiddleware())
    server.add_middleware(Probe())
    track(server, "proj_test")

    assert server.middleware[0] is _agentcat_middleware(server)[0]

    async with create_community_test_client(server) as client:
        first = _named(await client.list_tools(), "add_todo")
        second = _named(await client.list_tools(), "add_todo")

        r1 = await client.call_tool("add_todo", {"text": "same", "context": "why"})
        r2 = await client.call_tool("add_todo", {"text": "same", "context": "why"})

    # The listing below us was served from the cache the second time, and the
    # injection still reached the agent on both.
    assert SESSION_ID_PARAM in first.input_schema["properties"]
    assert SESSION_ID_PARAM in second.input_schema["properties"]
    assert len(below["schemas"]) == 1, "the cache below us served the second listing"
    assert below["schemas"][0]["add_todo"] == {"text"}
    assert SESSION_ID_PARAM not in below["schemas"][0]["get_more_tools"]

    # The cache below us never saw an injected argument...
    assert below["arguments"] == [{"text": "same"}]
    # ...and its hit did not swallow the second call.
    minted = [_text(r).split("session_id=")[1].split(" ")[0] for r in (r1, r2)]
    assert minted[0] != minted[1]
    assert [e.session_id for e in _call_events(capture)] == minted


async def test_get_more_tools_survives_the_default_dereference_middleware(capture):
    """FastMCP 4 installs `DereferenceRefsMiddleware` on every server.

    It hands back a `model_copy` for any tool carrying `$defs`/`$ref`, and a
    copy of our own tool must never read as a customer's — that false positive
    un-registers ours while leaving the copy in the listing, advertising a
    `get_more_tools` whose next call raises `Unknown tool`.
    """
    from fastmcp.server.middleware.dereference import DereferenceRefsMiddleware

    server = _new_server("deref-server")
    assert any(
        isinstance(mw, DereferenceRefsMiddleware) for mw in server.middleware
    ), "FastMCP 4 no longer installs the dereferencing middleware by default"

    @server.tool
    def with_refs(payload: dict) -> str:
        """A tool whose schema is the kind the dereferencer rewrites."""
        return str(payload)

    track(server, "proj_test", AgentCatOptions(enable_report_missing=True))

    async with create_community_test_client(server) as client:
        listed = sorted(t.name for t in await client.list_tools())
        assert listed == ["get_more_tools", "with_refs"]

        result = await client.call_tool("get_more_tools", {"context": "why"})
        assert "Unfortunately" in _text(result)

        assert sorted(t.name for t in await client.list_tools()) == listed

    names = [t.name for t in await server.list_tools(run_middleware=False)]
    assert "get_more_tools" in names, "ours was un-registered on a false positive"


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


# ── the era's second dispatch pass ─────────────────────────────────────────


async def test_a_message_that_is_not_the_typed_model_is_passed_straight_through(
    capture,
):
    """FastMCP 4 runs the chain a SECOND time for a component request that
    failed before the interior chain did (`low_level._dispatch_component`).

    That pass is observation only — its `call_next` re-raises the original
    failure — and it hands `MiddlewareContext.message` the RAW params mapping,
    because reconstructing a typed model is exactly what fails on a malformed
    message. A hook that assumed the typed model raised `AttributeError` there,
    and that error replaced the customer's own protocol error on the wire.
    """
    from fastmcp.server.middleware import MiddlewareContext
    from mcp.shared.exceptions import MCPError

    server = create_community_todo_server()
    track(server, "proj_test")
    # The helper, not `middleware[0]`: index 0 is only ours while nothing else
    # inserts ahead of us, and a blind bind would silently exercise FastMCP's
    # own middleware instead of AgentCat's.
    installed = _agentcat_middleware(server)
    assert len(installed) == 1
    middleware = installed[0]

    original = MCPError(-32602, "Invalid request parameters")

    async def re_raise(ctx):
        raise original

    # `initialize` is on the list because a failed handshake reconstructs no
    # message at all — FastMCP hands the hook `None` there.
    for method, message in (
        ("tools/call", {"arguments": {"text": "no name"}}),
        ("tools/list", {"cursor": 12345}),
        ("initialize", None),
    ):
        with pytest.raises(MCPError) as raised:
            await middleware(
                MiddlewareContext(message=message, method=method), re_raise
            )
        assert raised.value is original, f"{method} replaced the customer's error"

    assert capture == [], "a failed request published an event"


# ── tools that hold live runtime state ─────────────────────────────────────


async def test_openapi_generated_tools_are_still_injectable(capture):
    """An OpenAPI tool holds a live `httpx` client, and the schema copy is the
    only thing that makes injection possible for it.

    Deep-copying the `Tool` reintroduces the `threading.RLock` pickling failure
    that silently dropped injection for whole servers in v1, so the guard below
    asserts the hazard is still real on this era before asserting the outcome.
    """
    requests: list = []
    server = create_community_openapi_server(record_requests=requests)

    raw = await server.list_tools(run_middleware=False)
    with pytest.raises(TypeError, match="cannot pickle '_thread.RLock' object"):
        copy.deepcopy(raw[0])

    track(server, "proj_test")

    async with create_community_test_client(server) as client:
        listed = {t.name: t for t in await client.list_tools()}
        for name in OPENAPI_TOOL_NAMES:
            props = listed[name].input_schema["properties"]
            assert SESSION_ID_PARAM in props, f"session_id not injected into {name}"
            assert "context" in props, f"context not injected into {name}"

        await client.call_tool(
            "get_severity",
            {"id": "42", "context": "an outage", SESSION_ID_PARAM: sid("openapi")},
        )

    event = _call_events(capture)[-1]
    assert event.resource_name == "get_severity"
    assert event.session_id == sid("openapi")
    assert event.user_intent == "an outage"
    # Neither injected parameter may reach the customer's own backend.
    assert requests
    assert not any(sid("openapi") in str(r.url) for r in requests)
    assert not any("an outage" in str(r.url) for r in requests)

    # ...and the server's own cached tools are never mutated.
    raw_now = await server.list_tools(run_middleware=False)
    after = {t.name: t.parameters for t in raw_now}
    assert SESSION_ID_PARAM not in after["get_severity"]["properties"]
