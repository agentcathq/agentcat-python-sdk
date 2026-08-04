# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "agentcat",
#     "mcp>=2,<3",
# ]
#
# [tool.uv.sources]
# agentcat = { path = "../../..", editable = true }
# ///
"""Example: Minimal AgentCat integration with the official MCP SDK (mcp 2.x).

This shows the simplest possible AgentCat setup — one track() call. Every tool
call is captured automatically, and AgentCat correlates the calls belonging to
one session through the session_id parameter it adds to each tool schema and
mints back to the agent on its first call.

The PEP 723 header above makes this file self-contained: `uv run` resolves it
into an isolated, cached environment (mcp 2.x plus agentcat from this checkout)
without ever touching the project's .venv — safe to run even from a
legacy-synced checkout (see CONTRIBUTING.md).

Usage:

    uv run --no-project examples/officialsdk/basic/main.py

Serves Streamable HTTP on http://localhost:8091/mcp.
"""

import os

from mcp.server.mcpserver import MCPServer

import agentcat

PORT = 8091


# A three-level call chain so error_test produces a realistic chained
# stack trace for AgentCat's exception capture.
def process_data(data: str) -> str:
    if not data:
        raise ValueError("input must not be empty")
    raise ValueError(f"data processing failed for {data!r}: invalid payload structure")


def validate_input(data: str) -> str:
    try:
        return process_data(data)
    except ValueError as e:
        raise ValueError("validation error") from e


def dangerous_operation(data: str) -> str:
    try:
        return validate_input(data)
    except ValueError as e:
        raise RuntimeError("dangerous operation aborted") from e


def main() -> None:
    server = MCPServer("officialsdk-basic-example", version="1.0.0")

    # --- AgentCat: 3 lines to add analytics ---
    project_id = (
        os.environ.get("AGENTCAT_PROJECT_ID")
        or os.environ.get("MCPCAT_PROJECT_ID")
        or "proj_YOUR_PROJECT_ID"
    )
    agentcat.track(server, project_id)
    # --- end AgentCat ---
    #
    # track() never raises — a misconfiguration logs to ~/agentcat.log and the
    # server comes back untracked rather than down. There is no shutdown handle
    # to hold: the event queue is process-wide and drains itself at exit. Tools
    # registered after track() are picked up automatically.

    @server.tool(description="Echo back the input text")
    def echo(text: str) -> str:
        return text

    @server.tool(description="Reverse the input text")
    def reverse(text: str) -> str:
        return text[::-1]

    @server.tool(description="Count the number of characters in the input text")
    def count_chars(text: str) -> str:
        return str(len(text))

    @server.tool(description="Always errors — use this to test stack trace capture")
    def error_test(text: str) -> str:
        return dangerous_operation(text)

    print(f"MCP server listening on http://localhost:{PORT}/mcp")
    server.run(transport="streamable-http", host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
