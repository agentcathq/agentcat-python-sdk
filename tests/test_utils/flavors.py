"""Every server shape the v2 adapters serve, behind one small interface.

The cross-flavor regressions — concurrency, removed events, rebuild-on-demand,
the per-flavor ``response`` spelling — have to hold on every shape a customer
can hand ``track()``. There are six of them, four adapters wide and two
dependency sets deep, and a suite that quietly covered five would be worse than
one that covered none: it would read as complete.

So the shapes are enumerated once, here. Each `Flavor` is a small explicit
description of ONE real server — how to build it, how to talk to it, and where
its list source is. There is no normalization layer over the SDKs; only the
three era spellings the tests actually read are bridged (``inputSchema`` vs
``input_schema``, ``structuredContent`` vs ``structured_content``, ``isError``
vs ``is_error``).

`flavors()` returns exactly the shapes the INSTALLED dependency set can build.
``tests/test_detection.py::TestRealServerObjects`` pins that the list is
complete for its era and that every ``Flavor.flavor`` here is what
`detect_server` actually says about the object `build` returns, so neither can
drift silently.

Every ``mcp`` / ``fastmcp`` import is function-local: this module is imported
by test files that collect under both SDK majors.

No ``from __future__ import annotations`` here, deliberately. PEP 563 would
stringify the annotations of the tool bodies defined inside ``build()``, and
``mcp.server.fastmcp``'s ``Tool.from_function`` calls
``issubclass(param.annotation, Context)`` on mcp 1.7-1.13 — which raises
``TypeError: issubclass() arg 1 must be a class`` against a string. Nothing in
this module needs the future import: every annotation is PEP 585/604, which
Python 3.10 evaluates natively, and no name is used before it is defined.
"""

import contextlib
import copy
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from agentcat.modules.constants import GET_MORE_TOOLS_NAME
from agentcat.modules.detection import ServerFlavor
from tests.test_utils.delivery import record_delivered_arguments

MCP_MAJOR = int(version("mcp").split(".")[0])

try:
    FASTMCP_MAJOR: int | None = int(version("fastmcp").split(".")[0])
except PackageNotFoundError:  # pragma: no cover - community extra not installed
    FASTMCP_MAJOR = None

# A tool body's hook, awaited from inside the customer's handler. The
# concurrency test uses it to hold every call inside the adapter's per-call
# window at once.
Hook = Callable[[], Awaitable[None]]

ECHO_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}
RESULT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}
CONTEXT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"context": {"type": "string"}},
    "required": ["context"],
}
# A customer tool whose OWN first parameter is called `session_id` — a task
# tracker, a ticketing system, a job runner. Nothing about it is AgentCat's:
# the injection pass sees the collision and leaves the name alone, the strip
# spares it, and the call path must not read it as the analytics handle.
CUSTOMER_SESSION_ID_DESCRIPTION = "The customer's own session identifier."
COMPLETE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "description": CUSTOMER_SESSION_ID_DESCRIPTION,
        },
        "note": {"type": "string"},
    },
    "required": ["session_id"],
}

CUSTOMER_GET_MORE_TOOLS_DESCRIPTION = (
    "The customer's own tool, which happens to share AgentCat's name."
)

# What each tool body is allowed to receive, for the LOWLEVEL flavors, whose
# handlers are handed the raw argument dict: an argument the tool never declared
# is fatal there rather than ignored, because a body that quietly dropped one
# would let a strip regression pass unnoticed.
#
# The facade flavors cannot use this — their bodies are typed functions and the
# SDK never delivers an argument the signature does not name. Both official tool
# managers drop an unexpected one SILENTLY (community FastMCP is the one that
# raises), so on those two shapes "what was delivered" is observable only at the
# manager, which is what `tests.test_utils.delivery` reads.
ALLOWED_ARGUMENTS = {
    "echo": {"text"},
    GET_MORE_TOOLS_NAME: {"context"},
    "complete_task": {"session_id", "note"},
}


# The one text `echo` refuses. Every era reports a failing tool differently —
# a raise here, an `is_error` result there — and a test that wants a failure on
# all of them needs the failure itself to be era-independent.
BOOM_TEXT = "boom"


class ListSourceDown(RuntimeError):
    """Raised by a deliberately broken list source, so a log line names it."""


class ToolFailed(RuntimeError):
    """Raised by `echo` on the sentinel text: one failure shape for every era."""


def _reject_unexpected(tool_name: str, arguments: dict[str, Any]) -> None:
    allowed = ALLOWED_ARGUMENTS.get(tool_name, set())
    unexpected = sorted(set(arguments) - allowed)
    if unexpected:
        raise ValueError(f"unexpected arguments: {unexpected}")


def _boom(*args: Any, **kwargs: Any) -> Any:
    """A list source that is down. Synchronous on purpose: it raises before an
    awaiting caller ever reaches the ``await``, so one function breaks both the
    sync tool managers and the async ``list_tools`` seams."""
    raise ListSourceDown("the list source is down")


def _answer(tool_name: str, arguments: dict[str, Any]) -> str:
    """What every flavor's tool body returns, so only the wiring differs."""
    if tool_name == GET_MORE_TOOLS_NAME:
        return f"customer answered: {arguments['context']}"
    if tool_name == "complete_task":
        return f"completed {arguments['session_id']}: {arguments.get('note')}"
    if arguments["text"] == BOOM_TEXT:
        raise ToolFailed("the tool failed")
    return f"echo:{arguments['text']}"


@dataclass
class Built:
    """A fresh, UNTRACKED server plus what its tool bodies actually receive."""

    server: Any
    seen: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


@dataclass(frozen=True)
class ListedTool:
    """One advertised tool, era spellings bridged."""

    name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


@dataclass(frozen=True)
class Called:
    """One ``tools/call`` result, era spellings bridged."""

    text: str
    structured: dict[str, Any] | None
    is_error: bool
    raw: Any


def _bridged(obj: Any, snake: str, camel: str) -> Any:
    """The snake_case field when the model HAS one, else the camelCase one.

    Presence, not truthiness: 2.x keeps the old spelling as a deprecated alias,
    so a value-based fallback would reach it — and warn — every time the real
    field is legitimately None.
    """
    if hasattr(obj, snake):
        return getattr(obj, snake)
    return getattr(obj, camel, None)


def _listed(tool: Any) -> ListedTool:
    return ListedTool(
        name=tool.name,
        input_schema=_bridged(tool, "input_schema", "inputSchema"),
        output_schema=_bridged(tool, "output_schema", "outputSchema"),
    )


def _called(result: Any) -> Called:
    blocks = getattr(result, "content", None) or []
    return Called(
        text="".join(b.text for b in blocks if hasattr(b, "text")),
        structured=_bridged(result, "structured_content", "structuredContent"),
        is_error=bool(_bridged(result, "is_error", "isError")),
        raw=result,
    )


class Flavor:
    """One server shape: build it, talk to it, break its list source."""

    id = ""
    flavor: ServerFlavor

    # Whether a `tools/call` can complete at all while the list source is down.
    #
    # On mcp 1.x it cannot, and that is the SDK's doing rather than AgentCat's:
    # the lowlevel `tools/call` handler opens by resolving the tool definition
    # from a cache it refreshes THROUGH the same list handler
    # (`Server._get_cached_tool_definition`), unconditionally — `validate_input`
    # does not gate it, which is why official FastMCP is no different. A listing
    # that raises therefore fails the call before the customer's tool body runs,
    # tracked or untracked. Everything AgentCat does on that path still happens
    # and is asserted; the tool body and the wire mirror are simply out of reach
    # on that era.
    survives_a_down_list_source = True

    def build(
        self,
        name: str = "cross-flavor",
        *,
        hook: Hook | None = None,
        customer_get_more_tools: bool = False,
        customer_session_id: bool = False,
    ) -> Built:
        """A fresh untracked server carrying `echo`, and optionally two tools
        whose names collide with AgentCat's: a `get_more_tools` whose
        ``context`` is a real parameter, and a `complete_task` whose
        ``session_id`` is a real parameter."""
        raise NotImplementedError

    def client(self, server: Any) -> Any:
        """An async context manager yielding a connected client."""
        raise NotImplementedError

    async def list_tools(self, client: Any) -> list[ListedTool]:
        raise NotImplementedError

    async def call(
        self, client: Any, name: str, arguments: dict[str, Any]
    ) -> Called:
        raise NotImplementedError

    async def call_unlisted(
        self, server: Any, name: str, arguments: dict[str, Any]
    ) -> Called:
        """One call on an instance that has never served a listing.

        The default is a real client that simply does not list first; the
        community flavors override it, because their client fetches schemas on
        its way to a call and so can never reproduce an un-listed instance.
        """
        async with self.client(server) as client:
            return await self.call(client, name, arguments)

    def break_list_source(self, server: Any) -> None:
        """Make the source `rebuild()` reads raise, and nothing else."""
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - test ids only
        return self.id


# ── official MCP SDK 1.x ─────────────────────────────────────────────────────


def _backfill_structured_output(fastmcp_server: Any) -> None:
    """Give a pre-1.10 `mcp.server.fastmcp.FastMCP` the structured output that
    its later selves produce natively.

    From mcp 1.10 the facade derives an ``outputSchema`` from a tool's return
    annotation and answers with matching ``structuredContent``. Before that the
    feature does not exist at all — not spelled differently, absent. But this
    flavor's contract is "a FastMCP server whose tools declare an output
    schema", because that is what the cross-flavor parity suites compare
    against the other five shapes; a flavor that quietly dropped half its
    contract on old mcp would make those suites read as green while covering
    less.

    Both patches go on ``_mcp_server.request_handlers`` — the same table the
    adapter itself wraps, and the one seam whose contract is stable across all
    of 1.x — so what AgentCat sees here is exactly what it sees on 1.10+.
    No-ops when the running SDK already does this.
    """
    import mcp.types as types

    if "outputSchema" in types.Tool.model_fields:
        return

    low = fastmcp_server._mcp_server
    list_inner = low.request_handlers[types.ListToolsRequest]
    call_inner = low.request_handlers[types.CallToolRequest]

    async def list_handler(req: Any) -> Any:
        result = await list_inner(req)
        listed = result.root
        return types.ServerResult(
            listed.model_copy(
                update={
                    "tools": [
                        tool.model_copy(
                            update={"outputSchema": dict(RESULT_OUTPUT_SCHEMA)}
                        )
                        for tool in listed.tools
                    ]
                }
            )
        )

    async def call_handler(req: Any) -> Any:
        result = await call_inner(req)
        inner = result.root
        if inner.isError:
            return result
        message = "".join(
            block.text for block in inner.content if hasattr(block, "text")
        )
        return types.ServerResult(
            inner.model_copy(update={"structuredContent": {"result": message}})
        )

    low.request_handlers[types.ListToolsRequest] = list_handler
    low.request_handlers[types.CallToolRequest] = call_handler


class OfficialFastMCPV1(Flavor):
    """`mcp.server.fastmcp.FastMCP`, adapted through its `_mcp_server`."""

    id = "official-fastmcp-v1"
    flavor = ServerFlavor.OFFICIAL_FASTMCP_V1
    survives_a_down_list_source = False

    def build(
        self,
        name: str = "cross-flavor",
        *,
        hook: Hook | None = None,
        customer_get_more_tools: bool = False,
        customer_session_id: bool = False,
    ) -> Built:
        from mcp.server.fastmcp import FastMCP

        built = Built(FastMCP(name))
        server = built.server

        @server.tool()
        async def echo(text: str) -> str:
            """Echo the text back."""
            if hook is not None:
                await hook()
            return _answer("echo", {"text": text})

        if customer_get_more_tools:

            @server.tool(name=GET_MORE_TOOLS_NAME)
            async def customers_get_more_tools(context: str) -> str:
                """The customer's own tool, which happens to share our name."""
                return f"customer answered: {context}"

        if customer_session_id:

            @server.tool()
            async def complete_task(session_id: str, note: str = "") -> str:
                """The customer's own session_id, which is not AgentCat's."""
                return _answer(
                    "complete_task", {"session_id": session_id, "note": note}
                )

        # `seen` is filled at the manager, not in the bodies above: see
        # `record_delivered_arguments` for why a typed body cannot report it.
        record_delivered_arguments(server._tool_manager, built.seen)
        _backfill_structured_output(server)
        return built

    @contextlib.asynccontextmanager
    async def client(self, server: Any) -> AsyncIterator[Any]:
        from .client import create_test_client

        async with create_test_client(server) as session:
            yield session

    async def list_tools(self, client: Any) -> list[ListedTool]:
        return [_listed(tool) for tool in (await client.list_tools()).tools]

    async def call(
        self, client: Any, name: str, arguments: dict[str, Any]
    ) -> Called:
        return _called(await client.call_tool(name, arguments))

    async def call_unlisted(
        self, server: Any, name: str, arguments: dict[str, Any]
    ) -> Called:
        # The raw request, not `call_tool`, for the reason `MCPServerV2` gives:
        # from mcp 1.15 the convenience method revalidates a result carrying
        # `structuredContent` against the tool's output schema, and fetches the
        # listing to do it. That listing is harmless to the rebuild — which has
        # already happened by then — but fatal to a test that has deliberately
        # taken the list source down.
        import mcp.types as types

        async with self.client(server) as client:
            result = await client.send_request(
                types.ClientRequest(
                    types.CallToolRequest(
                        method="tools/call",
                        params=types.CallToolRequestParams(
                            name=name, arguments=arguments
                        ),
                    )
                ),
                types.CallToolResult,
            )
        return _called(result)

    def break_list_source(self, server: Any) -> None:
        # The tool manager is what FastMCP's own `tools/list` handler reads,
        # and that handler is what the adapter recorded as the list source.
        server._tool_manager.list_tools = _boom


class LowlevelV1(OfficialFastMCPV1):
    """A bare `mcp.server.lowlevel.Server`: the same adapter, no facade."""

    id = "lowlevel-v1"
    flavor = ServerFlavor.LOWLEVEL_V1
    survives_a_down_list_source = False

    def build(
        self,
        name: str = "cross-flavor",
        *,
        hook: Hook | None = None,
        customer_get_more_tools: bool = False,
        customer_session_id: bool = False,
    ) -> Built:
        import mcp.types as types
        from mcp.server.lowlevel import Server

        built = Built(Server(name))
        server = built.server
        broken = {"list": False}
        server._cross_flavor_broken = broken

        tools = [
            types.Tool(
                name="echo",
                description="Echo the text back.",
                inputSchema=dict(ECHO_INPUT_SCHEMA),
                outputSchema=dict(RESULT_OUTPUT_SCHEMA),
            )
        ]
        if customer_get_more_tools:
            tools.append(
                types.Tool(
                    name=GET_MORE_TOOLS_NAME,
                    description=CUSTOMER_GET_MORE_TOOLS_DESCRIPTION,
                    inputSchema=dict(CONTEXT_INPUT_SCHEMA),
                    outputSchema=dict(RESULT_OUTPUT_SCHEMA),
                )
            )
        if customer_session_id:
            tools.append(
                types.Tool(
                    name="complete_task",
                    description="The customer's own session_id, not AgentCat's.",
                    inputSchema=copy.deepcopy(COMPLETE_INPUT_SCHEMA),
                    outputSchema=dict(RESULT_OUTPUT_SCHEMA),
                )
            )

        @server.list_tools()
        async def list_tools() -> list[Any]:
            if broken["list"]:
                raise ListSourceDown("the list source is down")
            return [tool.model_copy(deep=True) for tool in tools]

        # Registered straight into `request_handlers` rather than through
        # `@server.call_tool()`, because the decorator's return CONVENTION is
        # era-specific and this flavor has to build the same server on every
        # mcp 1.x. Returning `(content, structured)` needs mcp >= 1.10, and
        # returning a `CallToolResult` needs >= 1.19; below those the SDK
        # wraps the value as content and CallToolResult validation fails
        # INSIDE its own try, yielding a plausible-looking `isError` result
        # instead of an exception. `request_handlers` is the one seam whose
        # contract — a `ServerResult` in, a `ServerResult` out — has been
        # stable since 1.0, and it is the same table AgentCat itself wraps.
        #
        # The two behaviours the decorator supplies are reproduced here: a
        # successful call answers `isError=False`, and ANY raise becomes an
        # `isError=True` result carrying `str(exc)` — which is what makes
        # `ToolFailed` and `_reject_unexpected` observable to the tests.
        async def call_tool_handler(req: Any) -> Any:
            try:
                tool_name = req.params.name
                arguments = dict(req.params.arguments or {})
                _reject_unexpected(tool_name, arguments)
                built.seen.append((tool_name, arguments))
                if tool_name == "echo" and hook is not None:
                    await hook()
                message = _answer(tool_name, arguments)
                return types.ServerResult(
                    types.CallToolResult(
                        content=[types.TextContent(type="text", text=message)],
                        # `Tool`/`CallToolResult` are extra="allow" on every
                        # 1.x, so this reaches the wire below 1.10 too.
                        structuredContent={"result": message},
                        isError=False,
                    )
                )
            except Exception as exc:
                return types.ServerResult(
                    types.CallToolResult(
                        content=[types.TextContent(type="text", text=str(exc))],
                        isError=True,
                    )
                )

        server.request_handlers[types.CallToolRequest] = call_tool_handler

        return built

    def break_list_source(self, server: Any) -> None:
        server._cross_flavor_broken["list"] = True


# ── official MCP SDK 2.x ─────────────────────────────────────────────────────


class MCPServerV2(Flavor):
    """`mcp.server.mcpserver.MCPServer`, adapted through its
    `_lowlevel_server`."""

    id = "mcpserver-v2"
    flavor = ServerFlavor.MCPSERVER_V2

    def build(
        self,
        name: str = "cross-flavor",
        *,
        hook: Hook | None = None,
        customer_get_more_tools: bool = False,
        customer_session_id: bool = False,
    ) -> Built:
        from mcp.server.mcpserver import MCPServer

        built = Built(MCPServer(name))
        server = built.server

        @server.tool()
        async def echo(text: str) -> str:
            """Echo the text back."""
            if hook is not None:
                await hook()
            return _answer("echo", {"text": text})

        if customer_get_more_tools:

            @server.tool(name=GET_MORE_TOOLS_NAME)
            async def customers_get_more_tools(context: str) -> str:
                """The customer's own tool, which happens to share our name."""
                return f"customer answered: {context}"

        if customer_session_id:

            @server.tool()
            async def complete_task(session_id: str, note: str = "") -> str:
                """The customer's own session_id, which is not AgentCat's."""
                return _answer(
                    "complete_task", {"session_id": session_id, "note": note}
                )

        # `seen` is filled at the manager, not in the bodies above: see
        # `record_delivered_arguments` for why a typed body cannot report it.
        record_delivered_arguments(server._tool_manager, built.seen)
        return built

    @contextlib.asynccontextmanager
    async def client(self, server: Any) -> AsyncIterator[Any]:
        from .modern_server import create_modern_client

        async with create_modern_client(server) as session:
            yield session

    async def list_tools(self, client: Any) -> list[ListedTool]:
        return [_listed(tool) for tool in (await client.list_tools()).tools]

    async def call(
        self, client: Any, name: str, arguments: dict[str, Any]
    ) -> Called:
        return _called(await client.call_tool(name, arguments))

    async def call_unlisted(
        self, server: Any, name: str, arguments: dict[str, Any]
    ) -> Called:
        # The raw request, not `call_tool`: the convenience method revalidates
        # the result against the tool's output schema and fetches the listing
        # to do it. That listing is harmless to the rebuild — which has already
        # happened by then — but fatal to a test that has deliberately taken
        # the list source down. Same wire path, without the client's own extra
        # round trip.
        from mcp import types

        async with self.client(server) as client:
            result = await client.session.send_request(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name=name, arguments=arguments
                    )
                ),
                types.CallToolResult,
            )
        return _called(result)

    def break_list_source(self, server: Any) -> None:
        server._tool_manager.list_tools = _boom


class LowlevelV2(MCPServerV2):
    """A bare `mcp.server.Server`: the same adapter, no facade."""

    id = "lowlevel-v2"
    flavor = ServerFlavor.LOWLEVEL_V2

    def build(
        self,
        name: str = "cross-flavor",
        *,
        hook: Hook | None = None,
        customer_get_more_tools: bool = False,
        customer_session_id: bool = False,
    ) -> Built:
        from mcp import types
        from mcp.server import Server

        seen: list[tuple[str, dict[str, Any]]] = []
        broken = {"list": False}

        tools = [
            types.Tool(
                name="echo",
                description="Echo the text back.",
                input_schema=dict(ECHO_INPUT_SCHEMA),
                output_schema=dict(RESULT_OUTPUT_SCHEMA),
            )
        ]
        if customer_get_more_tools:
            tools.append(
                types.Tool(
                    name=GET_MORE_TOOLS_NAME,
                    description=CUSTOMER_GET_MORE_TOOLS_DESCRIPTION,
                    input_schema=dict(CONTEXT_INPUT_SCHEMA),
                    output_schema=dict(RESULT_OUTPUT_SCHEMA),
                )
            )
        if customer_session_id:
            tools.append(
                types.Tool(
                    name="complete_task",
                    description="The customer's own session_id, not AgentCat's.",
                    input_schema=copy.deepcopy(COMPLETE_INPUT_SCHEMA),
                    output_schema=dict(RESULT_OUTPUT_SCHEMA),
                )
            )

        async def on_list_tools(ctx: Any, params: Any) -> Any:
            if broken["list"]:
                raise ListSourceDown("the list source is down")
            return types.ListToolsResult(
                tools=[tool.model_copy(deep=True) for tool in tools]
            )

        async def on_call_tool(ctx: Any, params: Any) -> Any:
            arguments = dict(params.arguments or {})
            _reject_unexpected(params.name, arguments)
            seen.append((params.name, arguments))
            if params.name == "echo" and hook is not None:
                await hook()
            message = _answer(params.name, arguments)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=message)],
                structured_content={"result": message},
            )

        server = Server(name, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        server._cross_flavor_broken = broken
        return Built(server, seen)

    def break_list_source(self, server: Any) -> None:
        server._cross_flavor_broken["list"] = True


# ── community FastMCP (3.x beside mcp 1.x, 4.x beside mcp 2.x) ───────────────


class Community(Flavor):
    """Community `fastmcp.FastMCP`, adapted by a middleware at index 0."""

    def __init__(self, era: int) -> None:
        self.id = f"community-v{era}"
        self.flavor = (
            ServerFlavor.COMMUNITY_V3 if era == 3 else ServerFlavor.COMMUNITY_V4
        )

    def build(
        self,
        name: str = "cross-flavor",
        *,
        hook: Hook | None = None,
        customer_get_more_tools: bool = False,
        customer_session_id: bool = False,
    ) -> Built:
        from fastmcp import FastMCP

        built = Built(FastMCP(name))
        server = built.server

        @server.tool
        async def echo(text: str) -> str:
            """Echo the text back."""
            built.seen.append(("echo", {"text": text}))
            if hook is not None:
                await hook()
            return _answer("echo", {"text": text})

        if customer_get_more_tools:

            @server.tool(name=GET_MORE_TOOLS_NAME)
            async def customers_get_more_tools(context: str) -> str:
                """The customer's own tool, which happens to share our name."""
                built.seen.append((GET_MORE_TOOLS_NAME, {"context": context}))
                return f"customer answered: {context}"

        if customer_session_id:

            @server.tool
            async def complete_task(session_id: str, note: str = "") -> str:
                """The customer's own session_id, which is not AgentCat's."""
                built.seen.append(
                    ("complete_task", {"session_id": session_id, "note": note})
                )
                return _answer(
                    "complete_task", {"session_id": session_id, "note": note}
                )

        return built

    @contextlib.asynccontextmanager
    async def client(self, server: Any) -> AsyncIterator[Any]:
        from .community_client import create_community_test_client

        async with create_community_test_client(server) as session:
            yield session

    async def list_tools(self, client: Any) -> list[ListedTool]:
        return [_listed(tool) for tool in await client.list_tools()]

    async def call(
        self, client: Any, name: str, arguments: dict[str, Any]
    ) -> Called:
        return _called(await client.call_tool(name, arguments))

    async def call_unlisted(
        self, server: Any, name: str, arguments: dict[str, Any]
    ) -> Called:
        # Driven through the server rather than a client: a real FastMCP client
        # fetches output schemas on its way to a call, so it can never
        # reproduce a genuinely un-listed instance. `call_tool` runs the
        # middleware chain, which is the path under test.
        return _called(await server.call_tool(name, arguments))

    def break_list_source(self, server: Any) -> None:
        # `list_tools(run_middleware=False)` is the community rebuild's list
        # source, and an instance attribute shadows the bound method.
        server.list_tools = _boom


# ── what this dependency set can build ───────────────────────────────────────

# The official flavors of each era, keyed by the installed `mcp` major. Stated
# as a table rather than derived, so a missing entry reads as a gap.
_OFFICIAL: dict[int, list[Flavor]] = {
    1: [OfficialFastMCPV1(), LowlevelV1()],
    2: [MCPServerV2(), LowlevelV2()],
}


def flavors() -> list[Flavor]:
    """Every server shape the installed dependency set can build."""
    available = list(_OFFICIAL.get(MCP_MAJOR, []))
    if FASTMCP_MAJOR is not None:
        available.append(Community(FASTMCP_MAJOR))
    return available


def flavor_ids() -> list[str]:
    return [flavor.id for flavor in flavors()]


def tracking_data(server: Any) -> Any:
    """The `AgentCatData` `track()` stored for ``server``, on any flavor.

    Keyed on the object the adapter was installed on — the lowlevel server for
    the official flavors, the FastMCP itself for the community ones — which is
    exactly what `detect_server` hands back.
    """
    from agentcat.modules.detection import detect_server
    from agentcat.modules.internal import get_server_tracking_data

    return get_server_tracking_data(detect_server(server).lowlevel or server)
