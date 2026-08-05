"""Todo server implementation for testing (official MCP SDK 1.x).

Import-safe under mcp 2.x, which removed `FastMCP` and renamed `McpError`, so
a module that mixes era-agnostic unit tests with a `create_todo_server()`
integration class can still be collected there — the factory raises, the unit
tests run. `test_utils.modern_server` is the 2.x counterpart.
"""

from tests.test_utils.delivery import attach_delivery_recorder

try:
    from mcp.server import FastMCP
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    HAS_FASTMCP = True
except ImportError:  # mcp 2.x
    FastMCP = None
    McpError = None
    ErrorData = None
    HAS_FASTMCP = False


# Standard JSON-RPC error codes
INVALID_PARAMS = -32602


class CustomTestError(Exception):
    """Custom exception type for testing exception capture."""

    pass


class Todo:
    """Todo item."""

    def __init__(self, id: int, text: str, completed: bool = False):
        self.id = id
        self.text = text
        self.completed = completed


def create_todo_server():
    """Create a todo server for testing.

    Every tool body here is a typed function, so none of them can police its
    own arguments: measured on mcp 1.29, `mcp.server.fastmcp`'s tool manager
    DROPS an undeclared argument silently rather than failing the call. A test
    that sends AgentCat's injected parameters and asserts "no error" therefore
    proves nothing about the strip. The delivery recorder installed below is
    what makes it observable — read it with
    `tests.test_utils.delivery.delivered_arguments_for(server, "add_todo")`.
    """
    if FastMCP is None:
        raise ImportError(
            "FastMCP is not available in this MCP version. Use create_low_level_todo_server() instead."
        )
    # Fix deprecation warning by not passing version as kwarg
    server = FastMCP("todo-server")

    todos: list[Todo] = []
    next_id = 1

    @server.tool()
    def add_todo(text: str) -> str:
        """Add a new todo item."""
        nonlocal next_id
        todo = Todo(next_id, text)
        todos.append(todo)
        next_id += 1
        return f'Added todo: "{text}" with ID {todo.id}'

    @server.tool()
    def list_todos() -> str:
        """List all todo items."""
        if not todos:
            return "No todos found"

        todo_list = []
        for todo in todos:
            status = "✓" if todo.completed else "○"
            todo_list.append(f"{todo.id}: {todo.text} {status}")

        return "\n".join(todo_list)

    @server.tool()
    def complete_todo(id: int) -> str:
        """Mark a todo item as completed."""
        for todo in todos:
            if todo.id == id:
                todo.completed = True
                return f'Completed todo: "{todo.text}"'

        raise ValueError(f"Todo with ID {id} not found")

    @server.tool()
    def tool_that_raises(error_type: str = "value") -> str:
        """A tool that raises Python exceptions for testing."""
        if error_type == "value":
            raise ValueError("Test value error from tool")
        elif error_type == "runtime":
            raise RuntimeError("Test runtime error from tool")
        elif error_type == "custom":
            raise CustomTestError("Test custom error from tool")
        return "Should not reach here"

    @server.tool()
    def tool_with_mcp_error() -> str:
        """A tool that returns an MCP protocol error."""
        error = ErrorData(code=INVALID_PARAMS, message="Invalid parameters")
        raise McpError(error)

    attach_delivery_recorder(server, server._tool_manager)

    # Store original handlers for testing
    server._original_handlers = {
        "add_todo": add_todo,
        "list_todos": list_todos,
        "complete_todo": complete_todo,
        "tool_that_raises": tool_that_raises,
        "tool_with_mcp_error": tool_with_mcp_error,
    }

    return server
