# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "agentcat",
#     "mcp>=1.8,<2",
# ]
#
# [tool.uv.sources]
# agentcat = { path = "../../..", editable = true }
# ///
"""Example: AgentCat with the official MCP SDK 1.x (mcp.server.fastmcp.FastMCP).

agentcat supports both official-SDK generations from one install; this is the
1.x shape a large installed base still runs. The integration is identical to
the modern example — the same track() call — only the server class and its
serving API differ. (FastMCP 1.x takes no version parameter, so this server
reports no version.)

The PEP 723 header pins mcp to the 1.x major (>=1.8 for Streamable HTTP).
`uv run` resolves this into its own isolated environment, so running it never
re-syncs or destroys the project's .venv — the mcp-legacy / mcp-modern group
conflict in pyproject.toml does not apply to script environments.

Usage:

    uv run --no-project examples/officialsdk/legacy/main.py

Serves Streamable HTTP on http://localhost:8093/mcp.
"""

import os

from mcp.server.fastmcp import FastMCP

import agentcat

PORT = 8093


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
    # Host and port ride on FastMCP settings kwargs in 1.x; run() reads them.
    server = FastMCP("officialsdk-legacy-example", host="127.0.0.1", port=PORT)

    # --- AgentCat: 3 lines to add analytics ---
    project_id = (
        os.environ.get("AGENTCAT_PROJECT_ID")
        or os.environ.get("MCPCAT_PROJECT_ID")
        or "proj_YOUR_PROJECT_ID"
    )
    agentcat.track(server, project_id)
    # --- end AgentCat ---

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
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
