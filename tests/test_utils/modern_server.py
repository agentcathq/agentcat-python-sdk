"""Server and client factories for the modern official SDK (mcp 2.x).

The legacy siblings (`todo_server.py`, `client.py`) are built on mcp 1.x APIs
that 2.x removed — `mcp.server.fastmcp`, `mcp.shared.memory`. This module is
their 2.x counterpart: a bare lowlevel `Server`, an `MCPServer`, and the
in-process `Client` the modern SDK ships for exactly this purpose.

Only imported from modern-gated test modules (see `tests/conftest.py`), so the
`mcp` imports are module-scope here.
"""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.client import Client
from mcp.server import Server
from mcp.server.mcpserver import MCPServer

from tests.test_utils.delivery import attach_delivery_recorder

ADD_TODO_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}
ADD_TODO_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}


def _reject_unexpected(arguments: dict[str, Any], allowed: set[str]) -> None:
    """Fail loudly on any argument the tool never declared.

    The point of the strip is that AgentCat's injected parameters never reach
    the customer's tool; a handler that quietly ignored them would let a strip
    regression pass unnoticed.
    """
    unexpected = sorted(set(arguments) - allowed)
    if unexpected:
        raise ValueError(f"unexpected arguments: {unexpected}")


def create_lowlevel_todo_server(name: str = "todo-server-v2") -> Server[Any]:
    """A bare lowlevel v2 `Server` with a todo tool set.

    `complete_todo` reports failure the way the 2026 SDK does — an `is_error`
    result, not a raised exception — so the error path under test is the one
    real servers take.
    """
    todos: list[str] = []

    async def on_list_tools(
        ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="add_todo",
                    description="Add a new todo item.",
                    input_schema=dict(ADD_TODO_INPUT_SCHEMA),
                    output_schema=dict(ADD_TODO_OUTPUT_SCHEMA),
                ),
                types.Tool(
                    name="complete_todo",
                    description="Mark a todo item as completed.",
                    input_schema={
                        "type": "object",
                        "properties": {"id": {"type": "integer"}},
                        "required": ["id"],
                    },
                ),
            ]
        )

    async def on_call_tool(
        ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        arguments = dict(params.arguments or {})
        if params.name == "add_todo":
            _reject_unexpected(arguments, {"text"})
            todos.append(str(arguments["text"]))
            message = f'Added todo: "{arguments["text"]}" with ID {len(todos)}'
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=message)],
                structured_content={"result": message},
            )
        if params.name == "complete_todo":
            _reject_unexpected(arguments, {"id"})
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"Todo with ID {arguments.get('id')} not found",
                    )
                ],
                is_error=True,
            )
        raise ValueError(f"Unknown tool: {params.name}")

    return Server(name, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def create_mcpserver_todo_server(name: str = "todo-mcpserver") -> MCPServer:
    """An `MCPServer` with the same tool set, served through `_lowlevel_server`.

    Its tool bodies are typed functions, so — unlike the lowlevel factory above
    — they cannot police their own arguments: measured on mcp 2.0, the
    `_tool_manager` DROPS an undeclared argument silently rather than failing
    the call, so an un-stripped `session_id` would pass unnoticed. The delivery
    recorder installed below is what makes the strip observable; read it with
    `tests.test_utils.delivery.delivered_arguments_for(server, "add_todo")`.
    """
    todos: list[str] = []

    server = MCPServer(name)

    @server.tool()
    def add_todo(text: str) -> str:
        """Add a new todo item."""
        todos.append(text)
        return f'Added todo: "{text}" with ID {len(todos)}'

    @server.tool()
    def list_todos() -> str:
        """List all todo items."""
        return "\n".join(todos) if todos else "No todos found"

    attach_delivery_recorder(server, server._tool_manager)
    return server


def create_modern_client(server: Any, **kwargs: Any) -> Client:
    """An in-process `Client` for a v2 `Server` or `MCPServer`.

    Used as an async context manager, exactly like the SDK's own tests do it,
    so every assertion covers the real wire path (params validation, result
    serialization, per-version outbound sieve) rather than a hand-called
    handler.
    """
    return Client(server, **kwargs)
