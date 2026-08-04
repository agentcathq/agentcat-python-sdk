# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "agentcat",
#     "fastmcp>=4.0.0b1,<5",
#     "fastmcp-slim>=4.0.0b1,<5",
# ]
#
# [tool.uv.sources]
# agentcat = { path = "../../..", editable = true }
# ///
"""Example: Minimal AgentCat integration with community FastMCP (v4).

This shows the simplest possible AgentCat setup — one track() call — on the
community FastMCP framework. Every tool call is captured automatically, and
AgentCat correlates the calls belonging to one session through the session_id
parameter it adds to each tool schema and mints back to the agent on its
first call.

The explicit fastmcp-slim pin matters: fastmcp 4 is a prerelease, and uv only
honors prerelease versions named on DIRECT dependencies — the transitive
fastmcp-slim==4.0.0b1 pin would be rejected without it.

Usage:

    uv run --no-project examples/fastmcp/basic/main.py

Serves Streamable HTTP on http://localhost:8094/mcp.
"""

import os

from fastmcp import FastMCP

import agentcat

PORT = 8094


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
    server = FastMCP("fastmcp-basic-example", version="1.0.0")

    # --- AgentCat: 3 lines to add analytics ---
    project_id = (
        os.environ.get("AGENTCAT_PROJECT_ID")
        or os.environ.get("MCPCAT_PROJECT_ID")
        or "proj_YOUR_PROJECT_ID"
    )
    agentcat.track(server, project_id)
    # --- end AgentCat ---

    @server.tool
    def echo(text: str) -> str:
        """Echo back the input text."""
        return text

    @server.tool
    def reverse(text: str) -> str:
        """Reverse the input text."""
        return text[::-1]

    @server.tool
    def count_chars(text: str) -> str:
        """Count the number of characters in the input text."""
        return str(len(text))

    @server.tool
    def error_test(text: str) -> str:
        """Always errors — use this to test stack trace capture."""
        return dangerous_operation(text)

    print(f"MCP server listening on http://localhost:{PORT}/mcp")
    server.run(transport="http", host="127.0.0.1", port=PORT, show_banner=False)


if __name__ == "__main__":
    main()
