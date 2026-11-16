"""The server info heartbeat, on every server shape the installed SDKs build.

Each test name carries the ID of the row it covers in the cross-SDK test table
(V1 to V19). The rows run through `flavors()`, on a demo server that every
flavor builds from one description.

Faults and paging are put in at the lowlevel handler table, before `track()`.
That table is the one seam every flavor has, and it is where a customer's own
handler sits, so the SDK meets a failing or paging list handler the same way on
all of them.

No ``from __future__ import annotations`` here, for the reason
`tests/test_utils/flavors.py` gives: the tool bodies below are registered with
SDK versions that read their annotations as classes.
"""

import asyncio
import dataclasses
import gc
import inspect
import io
import json
import secrets
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest
import urllib3
from pydantic import Field

from agentcat import AgentCatOptions, track
from agentcat.modules import event_queue as queue_module
from agentcat.modules import server_info
from agentcat.modules.constants import GET_MORE_TOOLS_NAME
from agentcat.modules.event_queue import EventQueue
from tests.test_utils.flavors import (
    MCP_MAJOR,
    Flavor,
    flavor_ids,
    flavors,
    tracking_data,
)

FLAVORS = pytest.mark.parametrize("flavor", flavors(), ids=flavor_ids())

LONG_DESCRIPTION = "d" * 2000
BODY_KEYS = {
    "project_id",
    "server_name",
    "server_version",
    "server_title",
    "instructions",
    "features",
}


# ── the demo server ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str = ""
    # A title, annotations and an output schema, where the flavor has them.
    rich: bool = False
    parameter_description: str = "The text."


@dataclass(frozen=True)
class Spec:
    name: str = "demo-server"
    version: str = "1.2.3"
    title: str = "Demo Title"
    instructions: str = "Use the demo tools."
    tools: tuple[ToolDef, ...] = (
        ToolDef("alpha", "Alpha tool."),
        ToolDef("beta", LONG_DESCRIPTION, rich=True),
        ToolDef("gamma", "Gamma tool."),
    )
    resources: tuple[str, ...] = ("demo://one", "demo://two")
    tools_only: bool = False


class Gate:
    """A switch on one list handler of the demo server."""

    def __init__(self) -> None:
        # None, "fail", "hang" or "same-cursor".
        self.mode: str | None = None
        self.page_size: int | None = None
        self.calls = 0

    async def enter(self) -> None:
        self.calls += 1
        if self.mode == "fail":
            raise RuntimeError("the list handler failed")
        if self.mode == "hang":
            await asyncio.Event().wait()

    def page(self, items: list[Any], cursor: str | None) -> tuple[list[Any], Any]:
        if self.mode == "same-cursor":
            return items, "again"
        if self.page_size is None:
            return items, None
        start = int(cursor or 0)
        end = start + self.page_size
        return items[start:end], (str(end) if end < len(items) else None)


@dataclass
class Demo:
    server: Any
    lowlevel: Any
    # What the heartbeat must carry: an SDK with no title or no instructions
    # gives an empty string.
    title: str
    instructions: str
    # Whether the flavor registers every list handler, used or not.
    always_lists: bool
    hooks: dict[str, Callable[[], None]]
    gates: dict[str, Gate] = field(default_factory=dict)


def _accepted(target: Any, **wanted: Any) -> dict[str, Any]:
    """The keyword arguments of `wanted` that `target` takes."""
    parameters = inspect.signature(target).parameters
    if any(p.kind is p.VAR_KEYWORD for p in parameters.values()):
        return wanted
    return {key: value for key, value in wanted.items() if key in parameters}


def _tool_function(tool: ToolDef, hooks: dict[str, Callable[[], None]]) -> Any:
    async def body(
        text: str = Field(default="", description=tool.parameter_description),
    ) -> str:
        hook = hooks.get(tool.name)
        if hook is not None:
            hook()
        return f"{tool.name}:{text}"

    body.__name__ = tool.name
    return body


def _input_schema(tool: ToolDef) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": tool.parameter_description}
        },
    }


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
}


def _register_facade(server: Any, spec: Spec, hooks: dict[str, Any]) -> None:
    """Tools, resources and prompts on a decorator-style server."""
    for tool in spec.tools:
        wanted: dict[str, Any] = {"name": tool.name, "description": tool.description}
        if tool.rich:
            wanted["title"] = "Beta"
            wanted["annotations"] = {"readOnlyHint": True}
        server.tool(**_accepted(server.tool, **wanted))(_tool_function(tool, hooks))
    if spec.tools_only:
        return
    for index, uri in enumerate(spec.resources):

        def read() -> str:
            return "content"

        read.__name__ = f"resource_{index}"
        server.resource(uri)(read)

    @server.resource("demo://item/{item_id}")
    def item(item_id: str) -> str:
        return item_id

    @server.prompt()
    def greet(who: str) -> str:
        """Greet someone."""
        return f"Greet {who}."

    @server.prompt()
    def plain() -> str:
        """A prompt with no arguments."""
        return "Plain."


def _build_official_fastmcp_v1(spec: Spec, hooks: dict[str, Any]) -> Demo:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(spec.name, instructions=spec.instructions)
    server._mcp_server.version = spec.version
    _register_facade(server, spec, hooks)
    return Demo(
        server=server,
        lowlevel=server._mcp_server,
        title="",
        # The oldest mcp 1.x takes the argument and keeps no instructions.
        instructions=getattr(server._mcp_server, "instructions", None) or "",
        always_lists=True,
        hooks=hooks,
    )


def _build_lowlevel_v1(spec: Spec, hooks: dict[str, Any]) -> Demo:
    import mcp.types as types
    from mcp.server.lowlevel import Server

    identity = _accepted(
        Server.__init__, version=spec.version, instructions=spec.instructions
    )
    server = Server(spec.name, **identity)

    def tool_model(tool: ToolDef) -> Any:
        rich: dict[str, Any] = (
            {
                "outputSchema": dict(OUTPUT_SCHEMA),
                "title": "Beta",
                "annotations": {"readOnlyHint": True},
            }
            if tool.rich
            else {}
        )
        return types.Tool(
            name=tool.name,
            description=tool.description,
            inputSchema=_input_schema(tool),
            **rich,
        )

    @server.list_tools()
    async def list_tools() -> list[Any]:
        return [tool_model(tool) for tool in spec.tools]

    # Registered straight into the table, as `flavors.LowlevelV1` does: the
    # return convention of `@server.call_tool()` differs across mcp 1.x.
    async def call_tool(request: Any) -> Any:
        name = request.params.name
        hook = hooks.get(name)
        if hook is not None:
            hook()
        text = (request.params.arguments or {}).get("text", "")
        return types.ServerResult(
            types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{name}:{text}")],
                isError=False,
            )
        )

    server.request_handlers[types.CallToolRequest] = call_tool

    if not spec.tools_only:

        @server.list_resources()
        async def list_resources() -> list[Any]:
            return [
                types.Resource(uri=uri, name=f"resource-{index}")
                for index, uri in enumerate(spec.resources)
            ]

        @server.list_resource_templates()
        async def list_resource_templates() -> list[Any]:
            return [
                types.ResourceTemplate(uriTemplate="demo://item/{item_id}", name="item")
            ]

        @server.list_prompts()
        async def list_prompts() -> list[Any]:
            return [
                types.Prompt(
                    name="greet",
                    description="Greet someone.",
                    arguments=[types.PromptArgument(name="who", required=True)],
                ),
                types.Prompt(name="plain", description="A prompt with no arguments."),
            ]

    return Demo(
        server=server,
        lowlevel=server,
        title="",
        instructions=identity.get("instructions", ""),
        always_lists=False,
        hooks=hooks,
    )


def _build_mcpserver_v2(spec: Spec, hooks: dict[str, Any]) -> Demo:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        spec.name,
        title=spec.title,
        instructions=spec.instructions,
        version=spec.version,
    )
    _register_facade(server, spec, hooks)
    return Demo(
        server=server,
        lowlevel=server._lowlevel_server,
        title=spec.title,
        instructions=spec.instructions,
        always_lists=True,
        hooks=hooks,
    )


def _build_lowlevel_v2(spec: Spec, hooks: dict[str, Any]) -> Demo:
    from mcp import types
    from mcp.server import Server

    def tool_model(tool: ToolDef) -> Any:
        rich: dict[str, Any] = (
            {
                "output_schema": dict(OUTPUT_SCHEMA),
                "title": "Beta",
                "annotations": types.ToolAnnotations(read_only_hint=True),
            }
            if tool.rich
            else {}
        )
        return types.Tool(
            name=tool.name,
            description=tool.description,
            input_schema=_input_schema(tool),
            **rich,
        )

    async def on_list_tools(ctx: Any, params: Any) -> Any:
        return types.ListToolsResult(tools=[tool_model(tool) for tool in spec.tools])

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        hook = hooks.get(params.name)
        if hook is not None:
            hook()
        text = (params.arguments or {}).get("text", "")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{params.name}:{text}")]
        )

    async def on_list_resources(ctx: Any, params: Any) -> Any:
        return types.ListResourcesResult(
            resources=[
                types.Resource(uri=uri, name=f"resource-{index}")
                for index, uri in enumerate(spec.resources)
            ]
        )

    async def on_list_resource_templates(ctx: Any, params: Any) -> Any:
        return types.ListResourceTemplatesResult(
            resource_templates=[
                types.ResourceTemplate(
                    uri_template="demo://item/{item_id}", name="item"
                )
            ]
        )

    async def on_list_prompts(ctx: Any, params: Any) -> Any:
        return types.ListPromptsResult(
            prompts=[
                types.Prompt(
                    name="greet",
                    description="Greet someone.",
                    arguments=[types.PromptArgument(name="who", required=True)],
                ),
                types.Prompt(name="plain", description="A prompt with no arguments."),
            ]
        )

    others: dict[str, Any] = (
        {}
        if spec.tools_only
        else {
            "on_list_resources": on_list_resources,
            "on_list_resource_templates": on_list_resource_templates,
            "on_list_prompts": on_list_prompts,
        }
    )
    server = Server(
        spec.name,
        version=spec.version,
        title=spec.title,
        instructions=spec.instructions,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        **others,
    )
    return Demo(
        server=server,
        lowlevel=server,
        title=spec.title,
        instructions=spec.instructions,
        always_lists=False,
        hooks=hooks,
    )


def _build_community(spec: Spec, hooks: dict[str, Any]) -> Demo:
    from fastmcp import FastMCP

    server = FastMCP(spec.name, version=spec.version, instructions=spec.instructions)
    _register_facade(server, spec, hooks)
    return Demo(
        server=server,
        lowlevel=server._mcp_server,
        title="",
        instructions=spec.instructions,
        always_lists=True,
        hooks=hooks,
    )


_BUILDERS: dict[str, Callable[[Spec, dict[str, Any]], Demo]] = {
    "official-fastmcp-v1": _build_official_fastmcp_v1,
    "lowlevel-v1": _build_lowlevel_v1,
    "mcpserver-v2": _build_mcpserver_v2,
    "lowlevel-v2": _build_lowlevel_v2,
    "community-v3": _build_community,
    "community-v4": _build_community,
}

# features key -> (mcp 1.x request type, mcp 2.x method, 1.x field, 2.x field)
_LISTS = {
    "tools": ("ListToolsRequest", "tools/list", "tools", "tools"),
    "resources": ("ListResourcesRequest", "resources/list", "resources", "resources"),
    "resourceTemplates": (
        "ListResourceTemplatesRequest",
        "resources/templates/list",
        "resourceTemplates",
        "resource_templates",
    ),
    "prompts": ("ListPromptsRequest", "prompts/list", "prompts", "prompts"),
}


def _gate_v1(lowlevel: Any) -> dict[str, Gate]:
    import mcp.types as types

    gates: dict[str, Gate] = {}
    for key, (type_name, _, attribute, _) in _LISTS.items():
        request_type = getattr(types, type_name)
        inner = lowlevel.request_handlers.get(request_type)
        if inner is None:
            continue
        gate = gates[key] = Gate()

        async def gated(
            request: Any,
            inner: Any = inner,
            gate: Gate = gate,
            attribute: str = attribute,
        ) -> Any:
            await gate.enter()
            result = await inner(request)
            params = getattr(request, "params", None)
            cursor = getattr(params, "cursor", None) or getattr(request, "cursor", None)
            page, next_cursor = gate.page(getattr(result.root, attribute), cursor)
            return types.ServerResult(
                result.root.model_copy(
                    update={attribute: page, "nextCursor": next_cursor}
                )
            )

        lowlevel.request_handlers[request_type] = gated
    return gates


def _gate_v2(lowlevel: Any) -> dict[str, Gate]:
    gates: dict[str, Gate] = {}
    for key, (_, method, _, attribute) in _LISTS.items():
        entry = lowlevel._request_handlers.get(method)
        if entry is None:
            continue
        gate = gates[key] = Gate()

        async def gated(
            ctx: Any,
            params: Any,
            inner: Any = entry.handler,
            gate: Gate = gate,
            attribute: str = attribute,
        ) -> Any:
            await gate.enter()
            result = await inner(ctx, params)
            page, next_cursor = gate.page(
                getattr(result, attribute), getattr(params, "cursor", None)
            )
            return result.model_copy(
                update={attribute: page, "next_cursor": next_cursor}
            )

        lowlevel._request_handlers[method] = dataclasses.replace(entry, handler=gated)
    return gates


def build_demo(flavor: Flavor, spec: Spec = Spec()) -> Demo:
    """A fresh, untracked demo server with a gate on each of its list handlers."""
    demo = _BUILDERS[flavor.id](spec, {})
    demo.gates = (
        _gate_v1(demo.lowlevel)
        if hasattr(demo.lowlevel, "request_handlers")
        else _gate_v2(demo.lowlevel)
    )
    return demo


# ── talking to it ────────────────────────────────────────────────────────────


def _session(client: Any) -> Any:
    """The MCP session under a client; an mcp 1.x client is the session."""
    return getattr(client, "session", client)


async def call(
    client: Any, name: str = "alpha", arguments: dict[str, Any] | None = None
) -> str:
    """One `tools/call`, sent as the bare request. Returns the text it answers.

    A tracked call answers with the tool's own text and the session note of the
    SDK, so a test looks for the tool's text in it.

    The convenience methods of the newer clients send a `tools/list` of their
    own around a call, and that would be a second request in these tests.
    """
    from mcp import types

    params = types.CallToolRequestParams(
        name=name, arguments={"text": "hi"} if arguments is None else arguments
    )
    request: Any
    if MCP_MAJOR >= 2:
        request = types.CallToolRequest(params=params)
    else:
        request = types.ClientRequest(
            types.CallToolRequest(method="tools/call", params=params)
        )
    result = await _session(client).send_request(request, types.CallToolResult)
    return "\n".join(block.text for block in result.content)


async def list_tools(client: Any) -> list[dict[str, Any]]:
    """One `tools/list`; each tool as the JSON object that was on the wire."""
    result = await _session(client).list_tools()
    return [
        tool.model_dump(by_alias=True, mode="json", exclude_none=True)
        for tool in result.tools
    ]


async def settle() -> None:
    """Wait until every started heartbeat has been built and delivered."""
    while server_info._tasks:
        await asyncio.gather(*list(server_info._tasks), return_exceptions=True)
        # A finished task leaves the set in a callback, on the next loop turn.
        await asyncio.sleep(0)
    delivered = threading.Event()
    assert queue_module.event_queue.add_job(delivered.set)
    assert await asyncio.to_thread(delivered.wait, 10)


def settled_memory() -> None:
    """Make the memory of the SDK 60 seconds old."""
    server_info._memory.first_check_at = (
        server_info._now() - server_info.SETTLED_MEMORY_SECONDS
    )


def names(items: list[dict[str, Any]]) -> list[str]:
    return [item["name"] for item in items]


@pytest.fixture
def project() -> str:
    return "proj_" + secrets.token_hex(14)[:27]


@pytest.fixture(autouse=True)
def events(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A live event queue whose events go to a list, and nowhere else."""
    captured: list[Any] = []
    api = MagicMock()
    api.publish_event.side_effect = lambda publish_event_request, **_: captured.append(
        publish_event_request
    )
    queue = EventQueue(api_client=api)
    monkeypatch.setattr(queue_module, "event_queue", queue)
    yield captured
    queue.destroy()


# ── the body ─────────────────────────────────────────────────────────────────


@FLAVORS
async def test_v16_the_test_setup_keeps_the_heartbeat_off_the_network(
    flavor: Flavor, project: str, server_info_posts: Any, monkeypatch: Any
) -> None:
    opened: list[Any] = []
    monkeypatch.setattr(
        urllib3.PoolManager, "request", lambda *args, **kwargs: opened.append(args)
    )
    demo = build_demo(flavor)
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client)
        await settle()

    assert server_info._send is server_info_posts
    assert len(server_info_posts.bodies) == 1
    assert opened == []


@FLAVORS
async def test_v1_one_tool_call_sends_the_whole_server(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor)
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        assert "alpha:hi" in await call(client)
        await settle()
        assert len(server_info_posts.bodies) == 1
        body = server_info_posts.bodies[0]
        listed = await list_tools(client)

    assert set(body) == BODY_KEYS
    assert body["project_id"] == project
    assert body["server_name"] == "demo-server"
    assert body["server_version"] == "1.2.3"
    assert body["server_title"] == demo.title
    assert body["instructions"] == demo.instructions

    features = body["features"]
    assert set(features) == {"tools", "resources", "resourceTemplates", "prompts"}
    assert features["tools"] == listed
    assert names(features["tools"]) == ["alpha", "beta", "gamma", GET_MORE_TOOLS_NAME]
    alpha, beta = features["tools"][0], features["tools"][1]
    assert "context" in alpha["inputSchema"]["properties"]
    assert beta["description"] == LONG_DESCRIPTION
    assert len(features["resources"]) == 2
    assert len(features["resourceTemplates"]) == 1
    assert names(features["prompts"]) == ["greet", "plain"]
    assert [a["name"] for a in features["prompts"][0]["arguments"]] == ["who"]


@FLAVORS
async def test_v1_the_heartbeat_is_built_after_the_connection_has_closed(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor)
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client)
    await settle()

    assert len(server_info_posts.bodies) == 1
    features = server_info_posts.bodies[0]["features"]
    assert names(features["tools"]) == ["alpha", "beta", "gamma", GET_MORE_TOOLS_NAME]
    assert len(features["prompts"]) == 2


@FLAVORS
async def test_v2_a_paged_tool_list_is_read_to_its_end(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    five = tuple(ToolDef(name, f"Tool {name}.") for name in "abcde")
    demo = build_demo(flavor, Spec(tools=five))
    demo.gates["tools"].page_size = 2
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client, "a")
        await settle()

    assert len(server_info_posts.bodies) == 1
    tools = names(server_info_posts.bodies[0]["features"]["tools"])
    assert sorted(tools) == ["a", "b", "c", "d", "e", GET_MORE_TOOLS_NAME]


@FLAVORS
async def test_v2_reading_every_page_changes_nothing_a_call_relies_on(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    if flavor.id.startswith("community"):
        pytest.skip("FastMCP pages a listing after the SDK has seen all of it")
    five = tuple(ToolDef(name, f"Tool {name}.") for name in "abcde")
    demo = build_demo(flavor, Spec(tools=five))
    demo.gates["tools"].page_size = 2
    track(demo.server, project)
    data = tracking_data(demo.server)
    during_the_call: list[Any] = []
    demo.hooks["a"] = lambda: during_the_call.append(
        (dict(data.injected_params_registry), set(data.output_injection_registry))
    )
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client, "a")
        await settle()
        assert len(server_info_posts.bodies) == 1
        # The call path strips by what the first page gave it, as before.
        assert during_the_call == [
            (data.injected_params_registry, data.output_injection_registry)
        ]
        assert "a:hi" in await call(client, "a")


@FLAVORS
async def test_v3_a_server_with_tools_only(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor, Spec(tools_only=True))
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client)
        await settle()

    features = server_info_posts.bodies[0]["features"]
    if demo.always_lists:
        # This flavor registers every list handler, so each list is there,
        # and empty.
        assert {key: features[key] for key in features if key != "tools"} == {
            "resources": [],
            "resourceTemplates": [],
            "prompts": [],
        }
    else:
        assert set(features) == {"tools"}


@FLAVORS
async def test_v4_a_failing_prompt_list_is_left_out(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor)
    demo.gates["prompts"].mode = "fail"
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client)
        await settle()

    assert len(server_info_posts.bodies) == 1
    features = server_info_posts.bodies[0]["features"]
    assert set(features) == {"tools", "resources", "resourceTemplates"}
    assert names(features["tools"]) == ["alpha", "beta", "gamma", GET_MORE_TOOLS_NAME]


@FLAVORS
async def test_v5_a_failing_tool_list_sends_nothing(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor)
    # The list fails from the moment the tool body runs, so the call itself is
    # served and only the heartbeat meets the failure.
    demo.hooks["alpha"] = lambda: setattr(demo.gates["tools"], "mode", "fail")
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        assert "alpha:hi" in await call(client)
        await settle()

    assert demo.gates["tools"].mode == "fail"
    assert server_info_posts.bodies == []


@FLAVORS
async def test_v6_a_list_that_repeats_its_cursor_fails(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor)
    demo.gates["resources"].mode = "same-cursor"
    track(demo.server, project)
    settled_memory()
    started = time.monotonic()

    async with flavor.client(demo.server) as client:
        await call(client)
        await settle()

    assert time.monotonic() - started < 5
    assert demo.gates["resources"].calls <= server_info.MAX_PAGES
    assert len(server_info_posts.bodies) == 1
    assert "resources" not in server_info_posts.bodies[0]["features"]


@FLAVORS
async def test_v15_the_heartbeat_names_the_server_as_the_event_does(
    flavor: Flavor, project: str, server_info_posts: Any, events: list[Any]
) -> None:
    demo = build_demo(flavor)
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client)
        await settle()

    body = server_info_posts.bodies[0]
    event = events[-1]
    assert event.resource_name == "alpha"
    assert (event.server_name, event.server_version) == ("demo-server", "1.2.3")
    assert (body["server_name"], body["server_version"]) == (
        event.server_name,
        event.server_version,
    )


# ── redaction ────────────────────────────────────────────────────────────────

SECRET_SPEC = Spec(
    name="secret-server",
    instructions="the secret plan",
    tools=(ToolDef("secret_tool", "secret tool", parameter_description="a secret"),),
    resources=("file:///secret.txt",),
)


def hide_secret(text: str) -> str:
    return text.replace("secret", "[x]")


@FLAVORS
async def test_v11_the_redaction_function_runs_on_the_text_and_not_on_names(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor, SECRET_SPEC)
    track(
        demo.server,
        project,
        AgentCatOptions(redact_sensitive_information=hide_secret),
    )
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client, "secret_tool")
        await settle()

    assert len(server_info_posts.bodies) == 1
    body = server_info_posts.bodies[0]
    assert body["server_name"] == "secret-server"
    assert body["instructions"] == hide_secret(demo.instructions)
    tool = body["features"]["tools"][0]
    assert tool["name"] == "secret_tool"
    assert tool["description"] == "[x] tool"
    assert tool["inputSchema"]["properties"]["text"]["description"] == "a [x]"
    assert body["features"]["resources"][0]["uri"] == "file:///secret.txt"


@FLAVORS
async def test_v12_a_failing_redaction_function_sends_nothing(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    def fail(text: str) -> str:
        raise RuntimeError("the redaction function failed")

    demo = build_demo(flavor)
    track(demo.server, project, AgentCatOptions(redact_sensitive_information=fail))
    settled_memory()

    async with flavor.client(demo.server) as client:
        assert "alpha:hi" in await call(client)
        await settle()

    assert server_info_posts.bodies == []


def _redaction_body() -> dict[str, Any]:
    return {
        "project_id": "proj_secret",
        "server_name": "secret-server",
        "server_version": "secret-1",
        "server_title": "secret title",
        "instructions": "the secret plan",
        "features": {
            "tools": [
                {
                    "name": "secret_tool",
                    "title": "secret",
                    "inputSchema": {
                        "properties": {
                            "name": {"description": "secret", "default": None},
                            "secret": {"enum": ["secret", 1, True]},
                        }
                    },
                }
            ],
            "resources": [{"uri": "file:///secret.txt", "name": "secret"}],
            "resourceTemplates": [{"uriTemplate": "file:///{secret}", "name": "t"}],
            "prompts": [{"name": "p", "arguments": [{"name": "secret"}]}],
        },
    }


def test_v11_redaction_keeps_every_key_and_the_names_of_the_items() -> None:
    from agentcat.modules.redaction import redact_server_info

    body = _redaction_body()

    redacted = redact_server_info(body, hide_secret)

    assert redacted == {
        "project_id": "proj_secret",
        "server_name": "secret-server",
        "server_version": "secret-1",
        "server_title": "[x] title",
        "instructions": "the [x] plan",
        "features": {
            "tools": [
                {
                    "name": "secret_tool",
                    "title": "[x]",
                    "inputSchema": {
                        "properties": {
                            "name": {"description": "[x]", "default": None},
                            "secret": {"enum": ["[x]", 1, True]},
                        }
                    },
                }
            ],
            "resources": [{"uri": "file:///secret.txt", "name": "secret"}],
            "resourceTemplates": [{"uriTemplate": "file:///{secret}", "name": "t"}],
            "prompts": [{"name": "p", "arguments": [{"name": "[x]"}]}],
        },
    }
    assert body == _redaction_body()


def test_v11_an_async_redaction_function_is_awaited() -> None:
    from agentcat.modules.redaction import redact_server_info

    async def hide(text: str) -> str:
        return hide_secret(text)

    redacted = redact_server_info(_redaction_body(), hide)

    assert redacted["instructions"] == "the [x] plan"
    assert redacted["features"]["tools"][0]["title"] == "[x]"


def test_v12_a_redaction_function_that_gives_no_string_fails() -> None:
    from agentcat.modules.redaction import redact_server_info

    with pytest.raises(TypeError):
        redact_server_info(_redaction_body(), lambda text: None)


# ── the guard and the start ──────────────────────────────────────────────────


class Clock:
    """A clock that moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(server_info, "_now", fake)
    return fake


@FLAVORS
async def test_v7_one_heartbeat_an_hour(
    flavor: Flavor, project: str, server_info_posts: Any, clock: Clock
) -> None:
    demo = build_demo(flavor)
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        for _ in range(3):
            await call(client)
        await settle()
        assert len(server_info_posts.bodies) == 1

        clock.now += 3599
        await call(client)
        await settle()
        assert len(server_info_posts.bodies) == 1

        clock.now += 1
        await call(client)
        await settle()
        assert len(server_info_posts.bodies) == 2


@FLAVORS
async def test_v8_two_servers_with_one_name_send_one_heartbeat(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    settled_memory()
    for _ in range(2):
        demo = build_demo(flavor)
        track(demo.server, project)
        async with flavor.client(demo.server) as client:
            await call(client)
            await settle()

    assert len(server_info_posts.bodies) == 1


@FLAVORS
async def test_v8_two_servers_with_two_names_send_two_heartbeats(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    settled_memory()
    for name in ("demo-server", "other-server"):
        demo = build_demo(flavor, Spec(name=name))
        track(demo.server, project)
        async with flavor.client(demo.server) as client:
            await call(client)
            await settle()

    assert [body["server_name"] for body in server_info_posts.bodies] == [
        "demo-server",
        "other-server",
    ]


@FLAVORS
async def test_v9_no_heartbeat_without_a_project_id(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor)
    track(demo.server, project)
    # What a server tracked for telemetry exporters only carries.
    tracking_data(demo.server).project_id = None
    settled_memory()

    async with flavor.client(demo.server) as client:
        assert "alpha:hi" in await call(client)
        assert len(await list_tools(client)) == 4
        await settle()

    assert server_info_posts.bodies == []


@FLAVORS
async def test_v9_no_heartbeat_with_tracing_off(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor)
    track(demo.server, project, AgentCatOptions(enable_tracing=False))
    settled_memory()

    async with flavor.client(demo.server) as client:
        assert "alpha:hi" in await call(client)
        assert len(await list_tools(client)) == 4
        await settle()

    assert server_info_posts.bodies == []


@pytest.fixture
async def stray_errors(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Every error that nothing caught: on the loop, on a thread, in a finalizer."""
    errors: list[Any] = []
    monkeypatch.setattr(threading, "excepthook", errors.append)
    monkeypatch.setattr(sys, "unraisablehook", errors.append)
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: errors.append(context)
    )
    yield errors
    gc.collect()


@FLAVORS
async def test_v10_an_api_that_never_answers_delays_no_tool_call(
    flavor: Flavor, project: str, capfd: Any, stray_errors: list[Any]
) -> None:
    entered = threading.Event()
    release = threading.Event()
    bodies: list[Any] = []

    def never_answers(body: dict[str, Any]) -> int:
        bodies.append(body)
        entered.set()
        release.wait(30)
        raise TimeoutError("the API did not answer")

    server_info._send = never_answers
    demo = build_demo(flavor)
    track(demo.server, project)
    settled_memory()

    try:
        async with flavor.client(demo.server) as client:
            assert "alpha:hi" in await call(client)
            assert await asyncio.to_thread(entered.wait, 10)
            # The POST is still open, and the server answers as before.
            assert "alpha:hi" in await call(client)
            assert len(await list_tools(client)) == 4
    finally:
        release.set()
    await settle()

    assert len(bodies) == 1
    assert stray_errors == []
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("fault", "posts"),
    [("prompts-fail", 1), ("tools-fail", 0), ("redaction-fails", 0), ("api-500", 1)],
)
@FLAVORS
async def test_v10_a_fault_stays_inside_the_sdk(
    flavor: Flavor,
    fault: str,
    posts: int,
    project: str,
    server_info_posts: Any,
    capfd: Any,
    stray_errors: list[Any],
) -> None:
    def fail(text: str) -> str:
        raise RuntimeError("the redaction function failed")

    demo = build_demo(flavor)
    options = AgentCatOptions()
    if fault == "prompts-fail":
        demo.gates["prompts"].mode = "fail"
    elif fault == "tools-fail":
        demo.hooks["alpha"] = lambda: setattr(demo.gates["tools"], "mode", "fail")
    elif fault == "redaction-fails":
        options = AgentCatOptions(redact_sensitive_information=fail)
    else:
        server_info_posts.statuses = [500]
    track(demo.server, project, options)
    settled_memory()

    async with flavor.client(demo.server) as client:
        assert "alpha:hi" in await call(client)
        await settle()
        demo.hooks.clear()
        assert "alpha:hi" in await call(client)
        await settle()

    assert len(server_info_posts.bodies) == posts
    assert stray_errors == []
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize(("key", "posts"), [("resources", 1), ("tools", 0)])
@FLAVORS
async def test_v10_a_list_handler_that_never_returns_delays_no_tool_call(
    flavor: Flavor,
    key: str,
    posts: int,
    project: str,
    server_info_posts: Any,
    monkeypatch: pytest.MonkeyPatch,
    capfd: Any,
    stray_errors: list[Any],
) -> None:
    monkeypatch.setattr(server_info, "LIST_TIMEOUT_SECONDS", 0.3)
    demo = build_demo(flavor)
    demo.hooks["alpha"] = lambda: setattr(demo.gates[key], "mode", "hang")
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        assert "alpha:hi" in await call(client)
        # The call has its result while the build still waits for the list.
        building = list(server_info._tasks)
        assert building and not building[0].done()
        await settle()

    assert demo.gates[key].calls >= 1
    assert len(server_info_posts.bodies) == posts
    if posts:
        assert key not in server_info_posts.bodies[0]["features"]
    assert stray_errors == []
    assert capfd.readouterr() == ("", "")


@FLAVORS
async def test_v17_a_new_memory_sends_for_a_list_request_and_a_feedback_note(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor)
    track(demo.server, project)

    async with flavor.client(demo.server) as client:
        assert "alpha:hi" in await call(client)
        await settle()
        assert server_info_posts.bodies == []

        assert len(await list_tools(client)) == 4
        await settle()
        assert len(server_info_posts.bodies) == 1

    server_info._memory.clear()
    demo = build_demo(flavor)
    track(demo.server, project)

    async with flavor.client(demo.server) as client:
        await call(client, GET_MORE_TOOLS_NAME, {"context": "I need a tool."})
        await settle()

    assert len(server_info_posts.bodies) == 2


@FLAVORS
async def test_v18_with_no_memory_between_requests_only_list_requests_send(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    demo = build_demo(flavor)
    track(demo.server, project)

    async with flavor.client(demo.server) as client:
        for _ in range(3):
            server_info._memory.clear()
            assert "alpha:hi" in await call(client)
            await settle()
        assert server_info_posts.bodies == []

        for sent in (1, 2, 3):
            server_info._memory.clear()
            assert len(await list_tools(client)) == 4
            await settle()
            assert len(server_info_posts.bodies) == sent


@FLAVORS
async def test_v19_a_tool_call_sends_once_the_memory_is_60_seconds_old(
    flavor: Flavor, project: str, server_info_posts: Any, clock: Clock
) -> None:
    demo = build_demo(flavor)
    track(demo.server, project)

    async with flavor.client(demo.server) as client:
        await call(client)
        await settle()
        assert server_info_posts.bodies == []

        clock.now += 59
        await call(client)
        await settle()
        assert server_info_posts.bodies == []

        clock.now += 1
        await call(client)
        await settle()
        assert len(server_info_posts.bodies) == 1


def _data(project: str = "proj_guard", name: str | None = "guard-server") -> Any:
    from agentcat.types import AgentCatData

    return AgentCatData(project_id=project, options=AgentCatOptions(), server_name=name)


def test_v8_the_guard_empties_itself_at_1000_keys(clock: Clock) -> None:
    for index in range(server_info.MAX_GUARD_KEYS):
        assert server_info._due(_data(name=f"server-{index}"), server_info.LIST_REQUEST)
    assert len(server_info._memory.attempts) == server_info.MAX_GUARD_KEYS
    assert not server_info._due(_data(name="server-0"), server_info.LIST_REQUEST)

    assert server_info._due(_data(name="one-more"), server_info.LIST_REQUEST)

    assert list(server_info._memory.attempts) == [("proj_guard", "one-more")]


def test_v8_a_server_with_no_name_has_a_key_of_its_own(clock: Clock) -> None:
    assert server_info._due(_data(name=None), server_info.LIST_REQUEST)
    assert not server_info._due(_data(name=None), server_info.LIST_REQUEST)
    assert server_info._due(_data(name="named"), server_info.LIST_REQUEST)


def test_v10_a_check_with_no_event_loop_sends_nothing_and_raises_nothing(
    server_info_posts: Any,
) -> None:
    def source() -> Any:
        raise AssertionError("the source is read by the build only")

    server_info.check(_data(), server_info.LIST_REQUEST, source)

    assert server_info._tasks == set()
    assert server_info_posts.bodies == []


async def test_v10_the_check_does_no_work_on_the_request_path(
    server_info_posts: Any,
) -> None:
    read: list[str] = []

    def source() -> Any:
        read.append("source")
        return server_info.ServerInfoSource("", "", {})

    server_info.check(_data(), server_info.LIST_REQUEST, source)

    assert read == []
    await settle()
    assert read == ["source"]


# ── the limits of one list ───────────────────────────────────────────────────


async def test_v6_a_list_of_more_than_100_pages_fails() -> None:
    calls = 0

    async def fetch(cursor: Any) -> Any:
        nonlocal calls
        calls += 1
        return [{"name": f"tool-{calls}"}], f"cursor-{calls}"

    with pytest.raises(server_info._ListFailed):
        await server_info._collect(fetch)

    assert calls == server_info.MAX_PAGES


async def test_v6_a_list_that_takes_too_long_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_info, "LIST_TIMEOUT_SECONDS", 0.2)

    async def fetch(cursor: Any) -> Any:
        await asyncio.sleep(0.05)
        return [{"name": "tool"}], f"cursor-{time.monotonic()}"

    with pytest.raises(asyncio.TimeoutError):
        await server_info._collect(fetch)


async def test_v2_every_page_is_kept_in_order() -> None:
    pages = {None: (["a", "b"], "2"), "2": (["c"], "3"), "3": (["d"], None)}

    async def fetch(cursor: Any) -> Any:
        page, following = pages[cursor]
        return [{"name": name} for name in page], following

    assert names(await server_info._collect(fetch)) == ["a", "b", "c", "d"]


async def test_v10_a_build_that_takes_too_long_sends_nothing(
    server_info_posts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_info, "BUILD_TIMEOUT_SECONDS", 0.2)

    async def slow(cursor: Any) -> Any:
        await asyncio.sleep(0.15)
        return [{"name": "tool"}], None

    def source() -> Any:
        return server_info.ServerInfoSource("", "", {"tools": slow, "prompts": slow})

    server_info.check(_data(), server_info.LIST_REQUEST, source)
    await settle()

    assert server_info_posts.bodies == []


# ── the send ─────────────────────────────────────────────────────────────────


@FLAVORS
async def test_v13_a_413_is_answered_with_a_body_without_schemas(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    server_info_posts.statuses = [413, 202]
    demo = build_demo(flavor)
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client)
        await settle()

    assert len(server_info_posts.bodies) == 2
    first, second = server_info_posts.bodies
    assert all("inputSchema" in tool for tool in first["features"]["tools"])
    for tool in second["features"]["tools"]:
        assert "inputSchema" not in tool
        assert "outputSchema" not in tool
    for key in ("name", "title", "description"):
        assert [tool.get(key) for tool in second["features"]["tools"]] == [
            tool.get(key) for tool in first["features"]["tools"]
        ]
    assert second["features"]["tools"][1]["description"] == LONG_DESCRIPTION
    assert {k: v for k, v in second["features"].items() if k != "tools"} == {
        k: v for k, v in first["features"].items() if k != "tools"
    }
    assert {k: v for k, v in second.items() if k != "features"} == {
        k: v for k, v in first.items() if k != "features"
    }


@FLAVORS
async def test_v13_a_second_413_ends_the_attempt(
    flavor: Flavor, project: str, server_info_posts: Any
) -> None:
    server_info_posts.statuses = [413, 413, 202]
    demo = build_demo(flavor)
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client)
        await settle()
        await call(client)
        await settle()

    assert len(server_info_posts.bodies) == 2


class FakeApi:
    """Stands in for the network under the API client; answers each request."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.requests: list[dict[str, Any]] = []
        monkeypatch.setattr(urllib3.PoolManager, "request", self._request)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.requests.append({"method": method, "url": url, **kwargs})
        status = self.statuses.pop(0) if self.statuses else 202
        answer = b'{"status": "accepted"}' if status == 202 else b'{"error": "no"}'
        return urllib3.response.HTTPResponse(
            body=io.BytesIO(answer),
            status=status,
            headers={"Content-Type": "application/json"},
            preload_content=False,
        )


# A local address that nothing listens on. A client under test is built for
# this host or for a custom one, never for its default host, and `FakeApi`
# answers in place of the network either way.
SINK_URL = "http://127.0.0.1:9"


def fake_api(monkeypatch: pytest.MonkeyPatch, *statuses: int) -> FakeApi:
    """A fake network, and API clients of the test queue that point at the sink."""
    api = FakeApi(monkeypatch, *statuses)
    queue_module.event_queue.configure(SINK_URL)
    return api


@FLAVORS
async def test_v14_the_post_goes_to_the_custom_base_url(
    flavor: Flavor, project: str, server_info_posts: Any, monkeypatch: Any
) -> None:
    api = FakeApi(monkeypatch)
    demo = build_demo(flavor)
    track(demo.server, project, AgentCatOptions(api_base_url="https://custom.example"))
    # `track` gave the queue clients for the custom URL. The events of this
    # test stay off the fake network, so its one request is the heartbeat.
    queue_module.event_queue.api_client = MagicMock()
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client)
        await settle()

    # The recorder took the heartbeat. The real sender now gets the same body,
    # with the fake network under the client that `track` configured.
    assert api.requests == []
    assert server_info._post(server_info_posts.bodies[0]) == 202

    assert len(api.requests) == 1
    request = api.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://custom.example/v1/public/server-info/"
    assert request["timeout"].total == server_info.SEND_TIMEOUT_SECONDS == 10
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert headers["content-type"] == "application/json"
    assert "authorization" not in headers
    body = json.loads(request["body"])
    assert body == server_info_posts.bodies[0]
    assert set(body) == BODY_KEYS
    assert body["project_id"] == project


def _small_body() -> dict[str, Any]:
    return {
        "project_id": "proj_send",
        "server_name": "send-server",
        "server_version": "1.0.0",
        "server_title": "",
        "instructions": "",
        "features": {
            "tools": [
                {
                    "name": "alpha",
                    "description": "Alpha tool.",
                    "inputSchema": {"type": "object", "default": None},
                    "outputSchema": {"type": "object"},
                }
            ]
        },
    }


def test_v14_the_post_uses_the_api_client_of_the_event_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = fake_api(monkeypatch)
    queue = queue_module.event_queue

    assert queue.server_info_api.api_client is queue.api_client.api_client
    assert server_info._post(_small_body()) == 202

    assert api.requests[0]["url"] == f"{SINK_URL}/v1/public/server-info/"
    assert json.loads(api.requests[0]["body"]) == _small_body()


@pytest.mark.parametrize("status", [400, 413, 500, 503])
def test_v13_the_sender_reports_the_status_of_the_answer(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_api(monkeypatch, status)

    assert server_info._post(_small_body()) == status


def test_v10_a_queue_with_no_server_info_client_sends_nothing() -> None:
    assert queue_module.event_queue.server_info_api is None

    with pytest.raises(RuntimeError):
        server_info._post(_small_body())


def test_v13_a_413_leads_to_one_smaller_post(server_info_posts: Any) -> None:
    server_info_posts.statuses = [413, 202]

    server_info._deliver(_small_body(), None, time.monotonic() + 30)

    assert server_info_posts.bodies == [
        _small_body(),
        {
            **_small_body(),
            "features": {"tools": [{"name": "alpha", "description": "Alpha tool."}]},
        },
    ]


@pytest.mark.parametrize("status", [400, 500, 503])
def test_v10_any_other_answer_ends_the_attempt(
    status: int, server_info_posts: Any
) -> None:
    server_info_posts.statuses = [status]

    server_info._deliver(_small_body(), None, time.monotonic() + 30)

    assert len(server_info_posts.bodies) == 1


def test_v10_no_answer_ends_the_attempt() -> None:
    attempts: list[Any] = []

    def no_answer(body: dict[str, Any]) -> int:
        attempts.append(body)
        raise urllib3.exceptions.ConnectTimeoutError(None, SINK_URL, "no answer")

    server_info._send = no_answer

    server_info._deliver(_small_body(), None, time.monotonic() + 30)

    assert len(attempts) == 1


def test_v10_a_heartbeat_that_is_ready_too_late_is_not_sent(
    server_info_posts: Any,
) -> None:
    server_info._deliver(_small_body(), None, time.monotonic() - 1)

    assert server_info_posts.bodies == []


def test_v12_redaction_stops_at_the_time_limit_of_the_build(
    server_info_posts: Any,
) -> None:
    seen: list[str] = []

    def slow(text: str) -> str:
        seen.append(text)
        time.sleep(0.05)
        return text

    server_info._deliver(_small_body(), slow, time.monotonic() + 0.01)

    assert len(seen) == 1
    assert server_info_posts.bodies == []


# ── the log ──────────────────────────────────────────────────────────────────


@FLAVORS
async def test_the_log_counts_the_heartbeat_and_holds_none_of_its_text(
    flavor: Flavor, project: str, server_info_posts: Any, monkeypatch: Any
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(server_info, "write_to_log", lines.append)
    demo = build_demo(flavor)
    demo.gates["prompts"].mode = "fail"
    track(demo.server, project)
    settled_memory()

    async with flavor.client(demo.server) as client:
        await call(client)
        await settle()

    size = len(json.dumps(server_info_posts.bodies[0]).encode())
    assert lines == [
        "Server info heartbeat: the prompts list failed (RuntimeError), "
        "so it is left out",
        f"Server info heartbeat sent | 4 tools | {size} bytes",
    ]
