"""The inner tap: one contract, proven on every server generation.

v2 intercepts at the protocol boundary, where most SDK generations have already
caught the customer's exception and flattened it into an `isError` result. The
tap is what puts the type, the traceback and the `__cause__` chain back on the
event. This module proves three things:

- **the contract** — `inner_tap()` opens a slot, `capture()` fills it, the slot
  closes on every exit path, and `error()` falls back to the flattened result
  when nothing local ever raised;
- **the concurrency argument** — parallel calls cannot read each other's
  exception, and the proof is structural rather than a lucky single run;
- **per-era placement** — official FastMCP v1, bare lowlevel v1, MCPServer,
  bare lowlevel v2 and both community FastMCP eras each recover the detail,
  and the customer's wire result is what an untracked server returns.

Both eras live here on purpose: splitting the file across the two
conftest-gated trees would hide the parity it exists to show. The era classes
carry `LEGACY_ONLY` / `MODERN_ONLY`; the community class runs on both, because
`--extra community` installs FastMCP 3 beside mcp 1.x and FastMCP 4 beside
mcp 2.x.
"""

import asyncio
import contextlib

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.adapters._inner_tap import (
    _open_cell,
    capture,
    capture_in_flight,
    inner_tap,
    probing,
    tap_method,
    tapped,
)

from .test_utils import (
    LEGACY_ONLY,
    MCPSERVER_CRASH_TEXT_ON_WIRE,
    MCPSERVER_CRASH_WRAPPER,
    MODERN_ONLY,
    NEEDS_CONCURRENT_DISPATCH,
    NEEDS_LOWLEVEL_ERROR_SEAM,
)

try:
    import fastmcp  # noqa: F401

    HAS_COMMUNITY = True
except ImportError:  # pragma: no cover - community extra not installed
    HAS_COMMUNITY = False


class Boom(Exception):
    """Raised only by this module's tools, so a frame for it is unambiguous."""


@pytest.fixture
def events(monkeypatch):
    """Every event the queue is handed, without touching the network."""
    collected: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", collected.append)
    return collected


def _call_events(events, name):
    return [
        e
        for e in events
        if e.event_type == "mcp:tools/call" and e.resource_name == name
    ]


def _one(events, name):
    matched = _call_events(events, name)
    assert len(matched) == 1, f"expected one {name} event, got {len(matched)}"
    return matched[0]


def _frames_for(error, function):
    return [f for f in error.get("frames", []) if f["function"] == function]


def _chained_frames_for(error, function):
    return [
        f
        for chained in error.get("chained_errors", [])
        for f in chained.get("frames", [])
        if f["function"] == function
    ]


class Barrier:
    """Holds every concurrent tool body until all of them have arrived.

    So the per-era parallel tests stress the window they claim to: every call
    is provably inside its own capture slot, at the same time, before any of
    them fails.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.arrived = 0
        self.open = asyncio.Event()
        self.slots: list = []

    async def wait(self) -> None:
        # Recorded from inside the customer's tool body, which is where the
        # adapter's slot is open. The cell OBJECT, not a marker derived from
        # it: this is the structural claim the concurrency argument rests on.
        self.slots.append(_open_cell.get())
        self.arrived += 1
        if self.arrived == self.total:
            self.open.set()
        await self.open.wait()

    def assert_every_call_had_its_own_slot(self) -> None:
        assert None not in self.slots, "no capture slot was open in the tool body"
        assert len(self.slots) == self.total
        # The discriminating assertion. A module-global "last error" slot, or
        # a slot hoisted out of the per-call path to install time, gives every
        # concurrent call the SAME object here — and both would still pass an
        # assertion that only checks each event's own marker, because the
        # adapter path has no `await` between the capture and the read.
        assert len({id(slot) for slot in self.slots}) == self.total, (
            "concurrent calls shared one capture slot"
        )


# ── the contract ─────────────────────────────────────────────────────────────


class TestTapContract:
    """What the tap promises, independent of any server."""

    def test_a_capture_with_no_slot_open_is_a_no_op(self):
        # A tap left armed on a server whose call is not being traced must not
        # raise into the customer's process, and must not stash anything that
        # a later call could pick up.
        capture(Boom("nobody asked for this"))
        with inner_tap() as tap:
            assert tap.captured is None

    def test_the_last_write_wins(self):
        # A tool that composes another tool sees the sub-call's failure first
        # and its own second. The event has to describe the one the agent was
        # told about, which is always the last one to unwind.
        with inner_tap() as tap:
            capture(Boom("a sub-call the tool handled"))
            capture(RuntimeError("the tool's own failure"))
            assert isinstance(tap.captured, RuntimeError)

    def test_the_slot_closes_on_the_error_path(self):
        with contextlib.suppress(Boom):
            with inner_tap():
                capture(Boom("this call failed"))
                raise Boom("this call failed")
        with inner_tap() as tap:
            assert tap.captured is None

    def test_a_nested_slot_hands_the_outer_one_back(self):
        # A tool that calls its own server re-enters the adapter. The inner
        # call takes the slot and gives it back, so the outer call still
        # records its own failure rather than the inner one's.
        with inner_tap() as outer:
            with inner_tap() as inner:
                capture(Boom("inner call"))
            assert isinstance(inner.captured, Boom)
            assert outer.captured is None
            capture(RuntimeError("outer call"))
            assert isinstance(outer.captured, RuntimeError)

    def test_capture_in_flight_records_the_live_exception(self):
        with inner_tap() as tap:
            try:
                raise Boom("caught by the SDK")
            except Boom:
                capture_in_flight("caught by the SDK")
            assert isinstance(tap.captured, Boom)

    def test_capture_in_flight_ignores_a_message_the_sdk_composed(self):
        # The lowlevel v1 SDK reaches the same factory for a schema-validation
        # failure, handing it a sentence of its own rather than the exception's
        # message. Recording there would replace the one-line text the agent saw
        # with a multi-line jsonschema dump.
        composed = "Input validation error: 'x' is not of type 'integer'"
        with inner_tap() as tap:
            try:
                raise Boom("'x' is not of type 'integer'\n\nOn instance…")
            except Boom:
                capture_in_flight(composed)
            assert tap.captured is None

    def test_capture_in_flight_ignores_a_failure_that_predates_the_slot(self):
        # `sys.exc_info` answers for the whole stack, and the lowlevel v1 SDK
        # calls its error-result factory outside any `except` too (a bad return
        # type). An older frame's exception is not this call's failure.
        try:
            raise RuntimeError("someone else's failure")
        except RuntimeError:
            with inner_tap() as tap:
                capture_in_flight("someone else's failure")
                assert tap.captured is None

    def test_error_uses_the_tapped_exception_when_there_is_one(self):
        with inner_tap() as tap:
            try:
                raise Boom("the real failure")
            except Boom as exc:
                capture(exc)
            payload = tap.error("the flattened message")
        assert payload["type"] == "Boom"
        assert payload["message"] == "the real failure"
        assert payload["frames"]
        assert payload["platform"] == "python"

    def test_error_falls_back_when_nothing_local_ever_raised(self):
        # A proxy passing an upstream error through, or a tool that simply
        # returned is_error: there is no local exception and never will be.
        with inner_tap() as tap:
            payload = tap.error("upstream said no")
        assert payload == {
            "message": "upstream said no",
            "type": None,
            "platform": "python",
        }

    @pytest.mark.asyncio
    async def test_the_wrapping_form_re_raises_the_very_same_exception(self):
        """The tap observes. It must not swallow, replace or delay."""
        raised = Boom("the customer's own failure")

        async def fails(*args, **kwargs):
            raise raised

        wrapped = tapped(fails)
        with inner_tap() as tap:
            with pytest.raises(Boom) as caught:
                await wrapped("arg", keyword="value")
        assert caught.value is raised
        assert tap.captured is raised

    @pytest.mark.asyncio
    async def test_the_wrapping_form_returns_the_originals_own_result(self):
        sentinel = object()

        async def succeeds(*args, **kwargs):
            return sentinel

        assert await tapped(succeeds)() is sentinel

    def test_the_probing_form_returns_the_originals_own_result(self):
        sentinel = object()
        probe = probing(lambda message: sentinel)
        with inner_tap() as tap:
            try:
                raise Boom("the SDK is converting this right now")
            except Boom:
                assert probe("the SDK is converting this right now") is sentinel
            assert isinstance(tap.captured, Boom)

    def test_the_probing_form_survives_a_call_shape_it_does_not_know(self):
        # A capture is what an unrecognized call costs — never the customer's
        # error path.
        sentinel = object()
        probe = probing(lambda **kwargs: sentinel)
        with inner_tap() as tap:
            try:
                raise Boom("live")
            except Boom:
                assert probe(some_future_kwarg="live") is sentinel
            assert tap.captured is None

    def test_a_tap_that_cannot_be_installed_degrades_rather_than_raises(self):
        class ReadOnly:
            async def call_tool(self):  # pragma: no cover - never called
                return None

            def __setattr__(self, name, value):
                raise AttributeError("this server refuses attributes")

        state: dict = {}
        assert tap_method(ReadOnly(), "call_tool", state, "call_tool") is False
        assert tap_method(object(), "no_such_method", state, "missing") is False
        assert state == {}

    def test_a_synchronous_seam_is_refused_rather_than_broken(self):
        """`tapped` awaits what it wraps; a sync override would break every call."""

        class SyncOverride:
            def call_tool(self, name, arguments):  # pragma: no cover - never called
                return None

        server = SyncOverride()
        state: dict = {}
        assert tap_method(server, "call_tool", state, "call_tool") is False
        assert state == {}
        # The customer's own method is exactly where it was.
        assert server.call_tool.__func__ is SyncOverride.call_tool

    @pytest.mark.asyncio
    async def test_a_capture_a_seam_handled_itself_is_discarded(self):
        """A sub-call the caller suppressed did not produce the agent's result."""

        async def fails(*args, **kwargs):
            raise Boom("the sub-call")

        sub_call = tapped(fails)

        async def caller(*args, **kwargs):
            with contextlib.suppress(Boom):
                await sub_call()
            return "answered anyway"

        with inner_tap() as tap:
            assert await tapped(caller)() == "answered anyway"
            assert tap.captured is None

    @pytest.mark.asyncio
    async def test_a_capture_the_seam_re_raised_is_kept(self):
        """The community shape: a layer below converts a real failure."""

        async def fails(*args, **kwargs):
            raise Boom("the tool")

        seam = tapped(fails)
        with inner_tap() as tap:
            with contextlib.suppress(Boom):
                await seam()
            assert isinstance(tap.captured, Boom)

    @pytest.mark.asyncio
    async def test_a_sub_call_in_flight_cannot_erase_an_escaping_capture(self):
        """A concurrent sub-call is not "the seam that handled it".

        "Did this failure escape?" is answered per SEAM, not per nesting
        level: a seam's normal return drops only what was recorded inside its
        own dynamic extent. It used to be answered by a depth counter kept on
        the cell — and the cell is shared by every task that inherits it, so a
        sub-call the tool left running inflated the count, and then erased a
        genuine capture on its own normal return. The failure the agent was
        told about would have been published as the no-tap payload.
        """
        sub_call_is_inside = asyncio.Event()
        let_the_sub_call_finish = asyncio.Event()

        async def fails(*args, **kwargs):
            raise Boom("the tool")

        async def sub_call(*args, **kwargs):
            sub_call_is_inside.set()
            await let_the_sub_call_finish.wait()
            return "the sub-call finished"

        failing_seam = tapped(fails)
        other_seam = tapped(sub_call)

        with inner_tap() as tap:
            # A task the tool spawned: it inherits this call's slot.
            spawned = asyncio.create_task(other_seam())
            await sub_call_is_inside.wait()

            with contextlib.suppress(Boom):
                await failing_seam()
            assert isinstance(tap.captured, Boom)

            # The layer below AgentCat awaits while converting the raise into
            # an `is_error` result, and the sub-call lands in that window.
            let_the_sub_call_finish.set()
            await spawned
            assert isinstance(tap.captured, Boom), (
                "a concurrent sub-call's normal return erased the tool's failure"
            )


# ── the concurrency argument ─────────────────────────────────────────────────


class TestTapIsolation:
    """Cross-attribution has to be impossible, not merely absent in one run."""

    @pytest.mark.asyncio
    async def test_parallel_calls_cannot_read_each_others_exception(self):
        """Eight calls, all inside their slot at once, capturing out of order.

        A module-global "last error" slot passes a sequential test and fails
        this one: every call is provably inside its own slot before any of them
        captures, and they capture in reverse order, so a shared slot would
        hand call 0 call 7's exception.
        """
        total = 8
        arrived = 0
        all_inside = asyncio.Event()
        seen: dict[int, str] = {}

        async def one_call(index: int) -> None:
            nonlocal arrived
            with inner_tap() as tap:
                arrived += 1
                if arrived == total:
                    all_inside.set()
                await all_inside.wait()
                # Every sibling's slot is open right now.
                assert tap.captured is None
                # Reverse order, with a yield between each, so the captures
                # interleave with every other call's read.
                await asyncio.sleep(0.001 * (total - index))
                capture(Boom(f"call-{index}"))
                await asyncio.sleep(0.02)
                seen[index] = str(tap.captured)

        await asyncio.wait_for(
            asyncio.gather(*(one_call(i) for i in range(total))), timeout=10
        )
        assert seen == {i: f"call-{i}" for i in range(total)}

    @pytest.mark.asyncio
    async def test_a_capture_from_a_child_task_reaches_the_caller(self):
        """The reason the slot is an object and not a ContextVar of exceptions.

        A `ContextVar.set()` inside a child task is invisible to the parent —
        which is what the retired `store_captured_error` did. Writing to the
        cell the parent holds is visible, and both FastMCP generations reach
        the tap through a child of the request task.
        """

        async def child() -> None:
            capture(Boom("raised below a task boundary"))

        with inner_tap() as tap:
            await asyncio.create_task(child())
            assert isinstance(tap.captured, Boom)

    @pytest.mark.asyncio
    async def test_a_capture_from_a_worker_thread_reaches_the_caller(self):
        # How every SDK generation runs a synchronous tool body.
        with inner_tap() as tap:
            await asyncio.to_thread(capture, Boom("raised in a worker thread"))
            assert isinstance(tap.captured, Boom)


# ── official MCP SDK 1.x ─────────────────────────────────────────────────────


def _bare_v1_server(name="bare-v1"):
    """A lowlevel v1 `Server` whose own handler raises."""
    import mcp.types as types
    from mcp.server.lowlevel import Server

    server = Server(name)

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name="boom",
                description="raises",
                inputSchema={
                    "type": "object",
                    "properties": {"marker": {"type": "string"}},
                },
            )
        ]

    @server.call_tool()
    async def call_tool(tool: str, arguments: dict):
        raise Boom(f"bare kaboom {arguments.get('marker')}")

    return server


@LEGACY_ONLY
class TestOfficialV1:
    """Official FastMCP v1 and the bare lowlevel `Server` behind it."""

    @pytest.mark.asyncio
    async def test_fastmcp_v1_publishes_the_tool_error_and_its_cause(self, events):
        from .test_utils.client import create_test_client
        from .test_utils.todo_server import create_todo_server

        server = create_todo_server()
        track(server, "test_project", AgentCatOptions())

        async with create_test_client(server) as client:
            result = await client.call_tool(
                "tool_that_raises", {"error_type": "value"}
            )

        assert result.isError is True
        error = _one(events, "tool_that_raises").error
        # FastMCP wraps a failing tool before the lowlevel SDK flattens it, so
        # the recorded type is the wrapper and the customer's own exception is
        # the cause — the same shape the community adapter has always had.
        assert error["type"] == "ToolError"
        assert "Test value error from tool" in error["message"]
        assert error["stack"]
        assert error["frames"]
        cause = error["chained_errors"][0]
        assert cause["type"] == "ValueError"
        assert cause["message"] == "Test value error from tool"
        tool_frames = _chained_frames_for(error, "tool_that_raises")
        assert tool_frames, "the customer's own tool is missing from the traceback"
        assert tool_frames[0]["in_app"] is True
        assert "Test value error from tool" in tool_frames[0]["context_line"]

    @NEEDS_LOWLEVEL_ERROR_SEAM
    @pytest.mark.asyncio
    async def test_bare_lowlevel_v1_publishes_the_handlers_own_exception(
        self, events
    ):
        from .test_utils.client import create_test_client

        server = _bare_v1_server()
        track(server, "test_project", AgentCatOptions())

        async with create_test_client(server) as client:
            result = await client.call_tool("boom", {"marker": "one"})

        assert result.isError is True
        error = _one(events, "boom").error
        # No tool manager in the way: the handler's exception IS the failure.
        assert error["type"] == "Boom"
        assert error["message"] == "bare kaboom one"
        handler_frames = _frames_for(error, "call_tool")
        assert handler_frames and handler_frames[0]["in_app"] is True
        assert "raise Boom" in handler_frames[0]["context_line"]

    @pytest.mark.asyncio
    async def test_the_wire_error_is_what_an_untracked_server_returns(self, events):
        from .test_utils.client import create_test_client
        from .test_utils.todo_server import create_todo_server

        async def call(server):
            async with create_test_client(server) as client:
                return await client.call_tool(
                    "tool_that_raises", {"error_type": "runtime"}
                )

        untracked = await call(create_todo_server())
        tracked_server = create_todo_server()
        track(tracked_server, "test_project", AgentCatOptions())
        tracked = await call(tracked_server)

        assert tracked.isError == untracked.isError is True
        # The SDK's own error block, byte for byte. (A tracked result also
        # carries AgentCat's task mint-back in front of it, which is v2
        # behavior the tap neither adds to nor removes from.)
        assert tracked.content[1].model_dump() == untracked.content[0].model_dump()
        # Positive control. Everything above is an equality between two runs,
        # so it passes just as well when track() is a no-op — verified by
        # reducing track() to `return server`, which leaves the assertions
        # above green and fails the two below.
        assert _call_events(events, "tool_that_raises")
        assert len(tracked.content) > len(untracked.content)

    @NEEDS_CONCURRENT_DISPATCH
    @pytest.mark.asyncio
    async def test_parallel_failures_each_get_their_own_slot(self, events):
        from .test_utils.client import create_test_client
        from .test_utils.todo_server import create_todo_server

        total = 6
        barrier = Barrier(total)
        server = create_todo_server()

        @server.tool()
        async def boom(marker: str) -> str:
            """Raises once every parallel call is inside it."""
            await barrier.wait()
            raise Boom(f"kaboom {marker}")

        track(server, "test_project", AgentCatOptions())

        async with create_test_client(server) as client:
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        client.call_tool("boom", {"marker": f"m{i}"})
                        for i in range(total)
                    )
                ),
                timeout=20,
            )

        barrier.assert_every_call_had_its_own_slot()
        published = _call_events(events, "boom")
        assert len(published) == total
        for event in published:
            marker = event.parameters["arguments"]["marker"]
            cause = event.error["chained_errors"][0]
            assert cause["message"] == f"kaboom {marker}"

    @pytest.mark.asyncio
    async def test_a_tool_that_composes_a_tool_publishes_its_own_failure(
        self, events
    ):
        """The caller handles a sub-call's failure, then fails on its own.

        The event must describe what the agent was told, not the failure the
        caller dealt with. Keeping the FIRST capture published the sub-call's.
        """
        from .test_utils.client import create_test_client
        from .test_utils.todo_server import create_todo_server

        server = create_todo_server()

        @server.tool()
        async def inner(marker: str) -> str:
            """Always fails."""
            raise Boom(f"INNER {marker}")

        @server.tool()
        async def outer(marker: str) -> str:
            """Handles inner's failure, then fails on its own."""
            with contextlib.suppress(Exception):
                await server._tool_manager.call_tool("inner", {"marker": marker})
            raise Boom(f"OUTER {marker}")

        track(server, "test_project", AgentCatOptions())

        async with create_test_client(server) as client:
            result = await client.call_tool("outer", {"marker": "one"})

        wire = "".join(c.text for c in result.content if hasattr(c, "text"))
        assert "OUTER one" in wire and "INNER one" not in wire
        error = _one(events, "outer").error
        assert "OUTER one" in error["message"]
        assert "INNER one" not in error["message"]
        assert error["chained_errors"][0]["message"] == "OUTER one"

    @pytest.mark.asyncio
    async def test_a_handled_sub_call_is_not_published_as_the_result(self, events):
        """The caller handles a sub-call's failure and answers with its own error.

        Nothing the agent sees came from a Python exception, so the surfaced
        message is the whole payload — the sub-call's stack must not be it.

        The error result is produced by a layer BELOW AgentCat rather than by
        returning a `CallToolResult` from the tool body, which mcp only honors
        from 1.19 (PR #1459) and which JSON-serializes into an `isError=False`
        text block below it. Wrapping `request_handlers` before `track()` puts
        AgentCat above the layer that answers, which is the arrangement this
        test is about, and it behaves identically on every mcp 1.x.
        """
        from mcp.types import CallToolRequest, ServerResult, TextContent

        from .test_utils.client import create_test_client
        from .test_utils.todo_server import create_todo_server

        server = create_todo_server()

        @server.tool()
        async def inner(marker: str) -> str:
            """Always fails."""
            raise Boom(f"INNER {marker}")

        @server.tool()
        async def outer(marker: str) -> str:
            """Handles inner's failure and reports its own message."""
            with contextlib.suppress(Exception):
                await server._tool_manager.call_tool("inner", {"marker": marker})
            return f"OUTER declined {marker}"

        low = server._mcp_server
        answered = low.request_handlers[CallToolRequest]

        async def declines(req):
            result = await answered(req)
            return ServerResult(
                result.root.model_copy(
                    update={
                        "isError": True,
                        "content": [
                            TextContent(
                                type="text",
                                text=f"OUTER declined {req.params.arguments['marker']}",
                            )
                        ],
                    }
                )
            )

        low.request_handlers[CallToolRequest] = declines

        track(server, "test_project", AgentCatOptions())

        async with create_test_client(server) as client:
            result = await client.call_tool("outer", {"marker": "one"})

        assert result.isError is True
        error = _one(events, "outer").error
        assert error == {
            "message": "OUTER declined one",
            "type": None,
            "platform": "python",
        }

    @NEEDS_LOWLEVEL_ERROR_SEAM
    @pytest.mark.asyncio
    async def test_a_schema_validation_failure_keeps_the_surfaced_message(
        self, events
    ):
        """The lowlevel SDK composes that message itself; the probe skips it.

        Gated on the same seam: the input validation this asserts arrived in
        the very PR that added `_make_error_result`, so below it there is no
        "Input validation error:" for AgentCat to keep.
        """
        from .test_utils.client import create_test_client

        server = _bare_v1_server("validating-v1")
        track(server, "test_project", AgentCatOptions())

        async with create_test_client(server) as client:
            await client.list_tools()
            result = await client.call_tool("boom", {"marker": 123})

        assert result.isError is True
        # content[0] is AgentCat's task mint-back, which every v2 result
        # carries in front; the SDK's own error block follows it.
        surfaced = result.content[1].text
        assert surfaced.startswith("Input validation error:")
        error = _one(events, "boom").error
        assert error == {"message": surfaced, "type": None, "platform": "python"}


# ── official MCP SDK 2.x ─────────────────────────────────────────────────────


def _mcpserver_with_a_raising_tool(name="boom-mcpserver"):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name)

    @server.tool()
    def boom(marker: str) -> str:
        """Raises."""
        raise Boom(f"kaboom {marker}")

    return server


def _bare_v2_server(name="bare-v2"):
    from mcp import types
    from mcp.server import Server

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="boom",
                    description="raises",
                    input_schema={
                        "type": "object",
                        "properties": {"marker": {"type": "string"}},
                    },
                )
            ]
        )

    async def on_call_tool(ctx, params):
        raise Boom(f"bare kaboom {(params.arguments or {}).get('marker')}")

    return Server(name, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


@MODERN_ONLY
class TestOfficialV2:
    """`MCPServer` and the bare lowlevel v2 `Server` behind it."""

    @pytest.mark.asyncio
    async def test_mcpserver_publishes_the_tool_error_and_its_cause(self, events):
        from .test_utils.modern_server import create_modern_client

        server = _mcpserver_with_a_raising_tool()
        track(server, "test_project", AgentCatOptions())

        async with create_modern_client(server) as client:
            result = await client.call_tool("boom", {"marker": "one"})

        assert result.is_error is True
        error = _one(events, "boom").error
        assert error["type"] == MCPSERVER_CRASH_WRAPPER
        assert "kaboom one" in error["message"]
        assert error["stack"]
        assert error["frames"]
        cause = error["chained_errors"][0]
        assert cause["type"] == "Boom"
        assert cause["message"] == "kaboom one"
        tool_frames = _chained_frames_for(error, "boom")
        assert tool_frames and tool_frames[0]["in_app"] is True
        assert "raise Boom" in tool_frames[0]["context_line"]

    @pytest.mark.asyncio
    async def test_a_crash_publishes_the_same_message_on_every_generation(
        self, events
    ):
        """mcp 2.1 keeps a crash's text off the wire; the event still has it."""
        from .test_utils.modern_server import create_modern_client

        server = _mcpserver_with_a_raising_tool()
        track(server, "test_project", AgentCatOptions())

        async with create_modern_client(server) as client:
            await client.call_tool("boom", {"marker": "one"})

        error = _one(events, "boom").error
        assert error["message"] == "Error executing tool boom: kaboom one"
        assert error["chained_errors"][0]["message"] == "kaboom one"

    @pytest.mark.asyncio
    async def test_a_nested_crash_names_the_inner_tool_once(self, events):
        """The restored text never repeats what the wrapper already carries."""
        from mcp.server.mcpserver import MCPServer

        from .test_utils.modern_server import create_modern_client

        server = MCPServer("nesting-server")

        @server.tool()
        async def inner(marker: str) -> str:
            """Always fails."""
            raise Boom(f"INNER {marker}")

        @server.tool()
        async def outer(marker: str) -> str:
            """Lets inner's failure through."""
            await server.call_tool("inner", {"marker": marker})
            return "unreachable"

        track(server, "test_project", AgentCatOptions())

        async with create_modern_client(server) as client:
            await client.call_tool("outer", {"marker": "one"})

        message = _one(events, "outer").error["message"]
        prefix = "Error executing tool outer: Error executing tool inner"
        assert message.startswith(prefix)
        assert message.count("Error executing tool inner") == 1

    @pytest.mark.asyncio
    async def test_tracking_the_lowlevel_object_directly_still_taps(self, events):
        """`track(mcpserver._lowlevel_server)` hands over no facade at all."""
        from .test_utils.modern_server import create_modern_client

        server = _mcpserver_with_a_raising_tool("tracked-by-lowlevel")
        track(server._lowlevel_server, "test_project", AgentCatOptions())

        async with create_modern_client(server) as client:
            await client.call_tool("boom", {"marker": "one"})

        assert _one(events, "boom").error["type"] == MCPSERVER_CRASH_WRAPPER

    @pytest.mark.asyncio
    async def test_a_repeated_track_does_not_stack_a_second_tap(self, events):
        """Two taps would mean two frames and, worse, two installs to unstack."""
        from .test_utils.modern_server import create_modern_client

        server = _mcpserver_with_a_raising_tool("re-tracked")
        track(server, "test_project", AgentCatOptions())
        first = server.call_tool
        track(server, "test_project", AgentCatOptions())
        assert server.call_tool is not first

        async with create_modern_client(server) as client:
            await client.call_tool("boom", {"marker": "one"})

        error = _one(events, "boom").error
        assert error["type"] == MCPSERVER_CRASH_WRAPPER
        assert len([f for f in error["frames"] if f["function"] == "tap"]) == 1

    @pytest.mark.asyncio
    async def test_bare_lowlevel_v2_keeps_the_handlers_own_exception(self, events):
        """This era already let a handler's exception through; do not regress."""
        from .test_utils.modern_server import create_modern_client

        server = _bare_v2_server()
        track(server, "test_project", AgentCatOptions())

        async with create_modern_client(server) as client:
            with contextlib.suppress(Exception):
                await client.call_tool("boom", {"marker": "one"})

        error = _one(events, "boom").error
        assert error["type"] == "Boom"
        assert error["message"] == "bare kaboom one"
        handler_frames = _frames_for(error, "on_call_tool")
        assert handler_frames and handler_frames[0]["in_app"] is True

    @pytest.mark.asyncio
    async def test_a_customers_protocol_error_reaches_the_wire_unchanged(
        self, events
    ):
        """The Task 13 hazard class: AgentCat must not replace a -32602."""
        from mcp.server.mcpserver import MCPServer
        from mcp.shared.exceptions import MCPError
        from mcp.types import INVALID_PARAMS

        from .test_utils.modern_server import create_modern_client

        def build():
            server = MCPServer("protocol-error-server")

            @server.tool()
            def strict(marker: str) -> str:
                """Rejects at the protocol level."""
                raise MCPError(code=INVALID_PARAMS, message="Invalid parameters")

            return server

        async def call(server):
            async with create_modern_client(server) as client:
                try:
                    await client.call_tool("strict", {"marker": "x"})
                except Exception as exc:  # noqa: BLE001 - the error IS the result
                    return type(exc).__name__, getattr(exc, "code", None), str(exc)
                return None

        untracked = await call(build())
        tracked_server = build()
        track(tracked_server, "test_project", AgentCatOptions())
        tracked = await call(tracked_server)

        assert untracked is not None
        assert tracked == untracked
        # Positive control. This is the sole regression test for the bug fixed
        # in 18e68a1, and it is an equality between two runs — a detection
        # regression that left MCPServer unclassified would leave it green.
        # Verified by reducing track() to `return server`: the assertion above
        # still passes, this one does not.
        assert _call_events(events, "strict")

    @pytest.mark.asyncio
    async def test_the_wire_error_is_what_an_untracked_server_returns(self, events):
        from .test_utils.modern_server import create_modern_client

        async def call(server):
            async with create_modern_client(server) as client:
                return await client.call_tool("boom", {"marker": "one"})

        untracked = await call(_mcpserver_with_a_raising_tool("untracked"))
        tracked_server = _mcpserver_with_a_raising_tool("tracked")
        track(tracked_server, "test_project", AgentCatOptions())
        tracked = await call(tracked_server)

        assert tracked.is_error == untracked.is_error is True
        # Positive control: see the v1 sibling. An equality between two runs
        # holds trivially when track() does nothing.
        assert _call_events(events, "boom")
        assert len(tracked.content) > len(untracked.content)
        assert tracked.content[1].model_dump() == untracked.content[0].model_dump()

    @pytest.mark.asyncio
    async def test_parallel_failures_each_get_their_own_slot(self, events):
        from mcp.server.mcpserver import MCPServer

        from .test_utils.modern_server import create_modern_client

        total = 6
        barrier = Barrier(total)
        server = MCPServer("parallel-boom")

        @server.tool()
        async def boom(marker: str) -> str:
            """Raises once every parallel call is inside it."""
            await barrier.wait()
            raise Boom(f"kaboom {marker}")

        track(server, "test_project", AgentCatOptions())

        async with create_modern_client(server) as client:
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        client.call_tool("boom", {"marker": f"m{i}"})
                        for i in range(total)
                    )
                ),
                timeout=20,
            )

        barrier.assert_every_call_had_its_own_slot()
        published = _call_events(events, "boom")
        assert len(published) == total
        for event in published:
            marker = event.parameters["arguments"]["marker"]
            cause = event.error["chained_errors"][0]
            assert cause["message"] == f"kaboom {marker}"

    @pytest.mark.asyncio
    async def test_a_tool_that_composes_a_tool_publishes_its_own_failure(
        self, events
    ):
        """The caller handles a sub-call's failure, then fails on its own."""
        from mcp.server.mcpserver import MCPServer

        from .test_utils.modern_server import create_modern_client

        server = MCPServer("composing-server")

        @server.tool()
        async def inner(marker: str) -> str:
            """Always fails."""
            raise Boom(f"INNER {marker}")

        @server.tool()
        async def outer(marker: str) -> str:
            """Handles inner's failure, then fails on its own."""
            with contextlib.suppress(Exception):
                await server.call_tool("inner", {"marker": marker})
            raise Boom(f"OUTER {marker}")

        track(server, "test_project", AgentCatOptions())

        async with create_modern_client(server) as client:
            result = await client.call_tool("outer", {"marker": "one"})

        wire = "".join(c.text for c in result.content if hasattr(c, "text"))
        # The crash's own text is on the wire only where upstream puts it
        # (see MCPSERVER_CRASH_TEXT_ON_WIRE); the event carries it everywhere.
        assert "tool outer" in wire and "INNER one" not in wire
        if MCPSERVER_CRASH_TEXT_ON_WIRE:
            assert "OUTER one" in wire
        error = _one(events, "outer").error
        assert "OUTER one" in error["message"]
        assert "INNER one" not in error["message"]
        assert error["chained_errors"][0]["message"] == "OUTER one"

    @pytest.mark.asyncio
    async def test_a_handled_sub_call_is_not_published_as_the_result(self, events):
        """The caller handles a sub-call's failure and answers with its own error."""
        from mcp.server.mcpserver import MCPServer
        from mcp.types import CallToolResult, TextContent

        from .test_utils.modern_server import create_modern_client

        server = MCPServer("declining-server")

        @server.tool()
        async def inner(marker: str) -> str:
            """Always fails."""
            raise Boom(f"INNER {marker}")

        @server.tool()
        async def outer(marker: str) -> CallToolResult:
            """Handles inner's failure and reports its own error result."""
            with contextlib.suppress(Exception):
                await server.call_tool("inner", {"marker": marker})
            return CallToolResult(
                content=[TextContent(type="text", text=f"OUTER declined {marker}")],
                is_error=True,
            )

        track(server, "test_project", AgentCatOptions())

        async with create_modern_client(server) as client:
            result = await client.call_tool("outer", {"marker": "one"})

        assert result.is_error is True
        error = _one(events, "outer").error
        assert error == {
            "message": "OUTER declined one",
            "type": None,
            "platform": "python",
        }


# ── community FastMCP (both eras) ────────────────────────────────────────────


class _SwallowingMiddleware:
    """A layer that turns a raised tool error into an `is_error` result.

    Exactly what `fastmcp/server/providers/proxy.py` does for an upstream
    error, and what any error-handling middleware a customer writes does. It
    takes the branch where the community adapter used to have nothing but the
    surfaced message.
    """

    async def __call__(self, context, call_next):
        if getattr(context, "method", None) != "tools/call":
            return await call_next(context)
        try:
            return await call_next(context)
        except Exception as exc:
            from mcp.types import TextContent

            from .test_utils import error_tool_result

            return error_tool_result(
                content=[TextContent(type="text", text=f"swallowed: {exc}")],
                is_error=True,
            )


def _community_server(name="boom-community"):
    from fastmcp import FastMCP

    server = FastMCP(name)

    @server.tool
    def boom(marker: str) -> str:
        """Raises."""
        raise Boom(f"kaboom {marker}")

    return server


def _create_proxy(backend):
    """An in-process FastMCP proxy in front of ``backend``, either era.

    FastMCP 4 exposes `fastmcp.server.create_proxy`; the 3.x line spells the
    same thing `FastMCP.as_proxy`.
    """
    try:
        from fastmcp.server import create_proxy
    except ImportError:
        from fastmcp import FastMCP

        return FastMCP.as_proxy(backend)
    return create_proxy(backend)


@pytest.mark.skipif(not HAS_COMMUNITY, reason="Community FastMCP not installed")
class TestCommunity:
    """FastMCP 3 and 4 — whichever era this dependency set installed."""

    @pytest.mark.asyncio
    async def test_the_raise_path_still_carries_full_detail(self, events):
        """Already true before the tap; this is the do-not-regress guard."""
        from fastmcp import Client

        server = _community_server("raise-path")
        track(server, "test_project", AgentCatOptions())

        async with Client(server) as client:
            with contextlib.suppress(Exception):
                await client.call_tool("boom", {"marker": "one"})

        error = _one(events, "boom").error
        assert error["type"] is not None
        assert error["frames"]
        tool_frames = _chained_frames_for(error, "boom")
        assert tool_frames and tool_frames[0]["in_app"] is True

    @pytest.mark.asyncio
    async def test_an_is_error_result_without_a_raise_still_carries_detail(
        self, events
    ):
        from fastmcp import Client

        server = _community_server("swallowed")
        server.add_middleware(_SwallowingMiddleware())
        track(server, "test_project", AgentCatOptions())

        async with Client(server) as client:
            result = await client.call_tool(
                "boom", {"marker": "one"}, raise_on_error=False
            )

        assert result.is_error is True
        error = _one(events, "boom").error
        # No exception reached the adapter — a layer below it answered with an
        # is_error result — but one was raised, and the tap kept it.
        assert error["type"] is not None
        assert "kaboom one" in error["message"]
        tool_frames = _chained_frames_for(error, "boom")
        assert tool_frames and tool_frames[0]["in_app"] is True

    @pytest.mark.asyncio
    async def test_a_repeated_track_does_not_stack_a_second_tap(self, events):
        from fastmcp import Client

        server = _community_server("re-tracked")
        server.add_middleware(_SwallowingMiddleware())
        track(server, "test_project", AgentCatOptions())
        first = server.call_tool
        track(server, "test_project", AgentCatOptions())
        assert server.call_tool is not first

        async with Client(server) as client:
            await client.call_tool(
                "boom", {"marker": "one"}, raise_on_error=False
            )

        error = _one(events, "boom").error
        assert error["type"] is not None
        assert len([f for f in error["frames"] if f["function"] == "tap"]) == 1

    @pytest.mark.asyncio
    async def test_a_proxied_upstream_error_keeps_the_surfaced_message(self, events):
        """The documented gap, driven through a real proxy.

        From fastmcp 3.4 `providers/proxy.py` passes an upstream error result
        through deliberately — "rather than collapsing it into a raised
        ToolError" — from a `call_tool_mcp` that never raises. The failure
        happened in the backend; there is no Python exception anywhere in this
        process to recover, and no tap placement could change that.

        Below 3.4 the proxy did exactly the collapsing that comment rules out
        (`if result.isError: raise ToolError(first.text)`), so there IS a local
        exception and the tap keeps it — the gap is a CONSEQUENCE of PR #4217
        making the pass-through expressible, not a property of proxying. Both
        branches are asserted rather than one being skipped, so this still
        fails loudly if a future FastMCP changes its mind again.
        """
        from fastmcp import Client

        from .test_utils import FASTMCP_TOOLRESULT_HAS_IS_ERROR

        backend = _community_server("proxy-backend")
        front = _create_proxy(backend)
        track(front, "test_project", AgentCatOptions())

        async with Client(front) as client:
            result = await client.call_tool(
                "boom", {"marker": "one"}, raise_on_error=False
            )

        assert result.is_error is True
        # The backend's own error block is the last one: fastmcp >= 3.4 carries
        # AgentCat's task mint-back in front of it, while earlier versions (no
        # `ToolResult.is_error`) put no mint-back on an error result at all.
        upstream = result.content[-1].text
        assert "kaboom one" in upstream
        error = _one(events, "boom").error
        assert error["message"] == upstream
        if FASTMCP_TOOLRESULT_HAS_IS_ERROR:
            assert error == {"message": upstream, "type": None, "platform": "python"}
        else:
            assert error["type"] == "ToolError"
            assert error["frames"]

    @pytest.mark.asyncio
    async def test_the_wire_error_is_what_an_untracked_server_returns(self, events):
        from fastmcp import Client

        async def call(server):
            async with Client(server) as client:
                return await client.call_tool(
                    "boom", {"marker": "one"}, raise_on_error=False
                )

        untracked_server = _community_server("untracked")
        untracked_server.add_middleware(_SwallowingMiddleware())
        untracked = await call(untracked_server)

        tracked_server = _community_server("tracked")
        tracked_server.add_middleware(_SwallowingMiddleware())
        track(tracked_server, "test_project", AgentCatOptions())
        tracked = await call(tracked_server)

        assert tracked.is_error == untracked.is_error is True
        assert tracked.content[1].model_dump() == untracked.content[0].model_dump()
        # Positive control: see the v1 sibling. An equality between two runs
        # holds trivially when track() does nothing.
        assert _call_events(events, "boom")
        assert len(tracked.content) > len(untracked.content)

    @pytest.mark.asyncio
    async def test_parallel_failures_each_get_their_own_slot(self, events):
        from fastmcp import Client, FastMCP

        total = 6
        barrier = Barrier(total)
        server = FastMCP("parallel-community")
        server.add_middleware(_SwallowingMiddleware())

        @server.tool
        async def boom(marker: str) -> str:
            """Raises once every parallel call is inside it."""
            await barrier.wait()
            raise Boom(f"kaboom {marker}")

        track(server, "test_project", AgentCatOptions())

        async with Client(server) as client:
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        client.call_tool(
                            "boom", {"marker": f"m{i}"}, raise_on_error=False
                        )
                        for i in range(total)
                    )
                ),
                timeout=20,
            )

        barrier.assert_every_call_had_its_own_slot()
        published = _call_events(events, "boom")
        assert len(published) == total
        for event in published:
            marker = event.parameters["arguments"]["marker"]
            cause = event.error["chained_errors"][0]
            assert cause["message"] == f"kaboom {marker}"

    @pytest.mark.asyncio
    async def test_a_tool_that_composes_a_tool_publishes_its_own_failure(
        self, events
    ):
        """The caller handles a sub-call's failure, then fails on its own."""
        from fastmcp import Client

        server = _community_server("composing-community")

        @server.tool
        async def inner(marker: str) -> str:
            """Always fails."""
            raise Boom(f"INNER {marker}")

        @server.tool
        async def outer(marker: str) -> str:
            """Handles inner's failure, then fails on its own."""
            with contextlib.suppress(Exception):
                await server.call_tool("inner", {"marker": marker})
            raise Boom(f"OUTER {marker}")

        track(server, "test_project", AgentCatOptions())

        async with Client(server) as client:
            result = await client.call_tool(
                "outer", {"marker": "one"}, raise_on_error=False
            )

        wire = "".join(c.text for c in result.content if hasattr(c, "text"))
        assert "OUTER one" in wire and "INNER one" not in wire
        error = _one(events, "outer").error
        assert "OUTER one" in error["message"]
        assert "INNER one" not in error["message"]

    @pytest.mark.asyncio
    async def test_a_sub_call_still_running_does_not_cost_the_tools_detail(
        self, events
    ):
        """The unit-level sub-call case, live on whichever era is installed.

        Every piece here is something a real server does: a tool that leaves
        work running, and a layer below AgentCat that converts the raise into
        an `is_error` result and awaits while it does so. The sub-call inherits
        the failing call's slot, and its normal return used to erase the
        exception — degrading the event from the tool's own `Boom`, with its
        frames, to the three-key payload a server with no tap at all produces.
        """
        from fastmcp import Client, FastMCP
        from mcp.types import TextContent

        from .test_utils import error_tool_result

        sub_call_is_inside = asyncio.Event()
        let_the_sub_call_finish = asyncio.Event()
        spawned: list = []

        class ConvertsWhileTheSubCallFinishes:
            """A layer BELOW us doing what a proxy or an error-handling
            middleware does — with one await in it."""

            async def __call__(self, context, call_next):
                if getattr(context, "method", None) != "tools/call":
                    return await call_next(context)
                try:
                    return await call_next(context)
                except Exception as exc:
                    let_the_sub_call_finish.set()
                    await spawned[0]
                    return error_tool_result(
                        content=[TextContent(type="text", text=f"swallowed: {exc}")],
                        is_error=True,
                    )

        server = FastMCP("sub-call-in-flight")

        @server.tool
        async def slow(marker: str) -> str:
            """The sub-call the failing tool left running."""
            sub_call_is_inside.set()
            await let_the_sub_call_finish.wait()
            return f"slow {marker}"

        @server.tool
        async def boom(marker: str) -> str:
            """Leaves a sub-call running on this call's slot, then raises."""
            spawned.append(
                asyncio.create_task(server.call_tool("slow", {"marker": marker}))
            )
            await sub_call_is_inside.wait()
            raise Boom(f"kaboom {marker}")

        server.add_middleware(ConvertsWhileTheSubCallFinishes())
        track(server, "test_project", AgentCatOptions())

        async with Client(server) as client:
            result = await asyncio.wait_for(
                client.call_tool("boom", {"marker": "one"}, raise_on_error=False),
                timeout=20,
            )

        assert result.is_error is True
        error = _one(events, "boom").error
        assert error["type"] is not None, "the tap's capture was erased"
        assert "kaboom one" in error["message"]
        tool_frames = _chained_frames_for(error, "boom")
        assert tool_frames and tool_frames[0]["in_app"] is True
        # The sub-call succeeded, and its own event says so.
        assert _one(events, "slow").is_error is False
