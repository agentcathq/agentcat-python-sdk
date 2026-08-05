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
"""Example: Stateless server factory with the official MCP SDK (mcp 2.x).

This is the expected 2026 deployment shape: the server is built in a factory
and served with stateless HTTP, where nothing survives between requests.
track() mutates the instance it is given, so it runs INSIDE the factory, on
the instance about to serve traffic (see the "Track inside your server
factory" section of the repo README).

It demonstrates that:

- module-level AgentCat state (publisher, logger, diagnostics) initializes
  once no matter how many servers you track; per-server state is weakly keyed
  and released when a server goes away — a factory does not leak;
- correlation survives statelessness, because the session_id handle travels
  on the wire (echoed back by the agent) rather than in server memory;
- shutdown is process-wide: there is no handle to hold, the event queue
  drains itself at exit.

Usage:

    uv run --no-project examples/officialsdk/factory/main.py

Serves stateless Streamable HTTP on http://localhost:8090/mcp.
"""

import os

from mcp.server.mcpserver import MCPServer

import agentcat

PORT = 8090

PROJECT_ID = (
    os.environ.get("AGENTCAT_PROJECT_ID")
    or os.environ.get("MCPCAT_PROJECT_ID")
    or "proj_YOUR_PROJECT_ID"
)


def create_server() -> MCPServer:
    """Build and track a server instance.

    In a per-worker or multi-tenant deployment this factory runs once per
    instance. track() never raises — analytics must never take a customer
    server down — so there is no error path to handle here.
    """
    server = MCPServer("officialsdk-factory-example", version="1.0.0")

    @server.tool(description="Echo back the input text")
    def echo(text: str) -> str:
        return text

    @server.tool(description="Count the number of characters in the input text")
    def count_chars(text: str) -> str:
        return str(len(text))

    agentcat.track(server, PROJECT_ID)
    return server


def main() -> None:
    server = create_server()
    print(f"Stateless MCP server listening on http://localhost:{PORT}/mcp")
    # stateless_http=True builds a fresh transport per REQUEST — no session
    # state survives between calls. AgentCat still correlates them: the
    # session_id handle rides on the wire, and its injection registries are
    # rebuilt on demand from the server's own tool list if a call arrives at
    # an instance that never served a tools/list.
    server.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=PORT,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
