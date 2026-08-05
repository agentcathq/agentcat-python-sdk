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
"""Example: Full AgentCat v2 options with community FastMCP (v4).

Demonstrates the options beyond the basic 3-line integration:

- ``identify`` — attach actor identity to every captured event
- ``enable_agent_tracking`` — inject a required agent_id parameter so
  parallel agents working one session can be told apart
- ``redact_sensitive_information`` — strip sensitive data (here: emails)
  before it leaves the process
- ``debug_mode`` — verbose logging to ~/agentcat.log
- commented out: ``resolve_session_id`` hook mode, and opt-outs for the
  injected context parameter and the get_more_tools tool

Usage:

    uv run --no-project examples/fastmcp/advanced/main.py

Serves Streamable HTTP on http://localhost:8095/mcp.
"""

import os
import re

from fastmcp import FastMCP

import agentcat
from agentcat import AgentCatOptions, UserIdentity

PORT = 8095

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def identify_user(request, extra):
    """Attribute this call's event to an actor.

    Runs on EVERY tool call, uncached, and stamps only that call's event —
    keep it cheap and make no network calls. ``request`` carries the call's
    params (``request.name``, ``request.arguments``); a real implementation
    would derive the actor from auth data rather than hardcoding one. If this
    raises or returns None, the event publishes anonymously.
    """
    return UserIdentity(
        user_id="user-123",
        user_name="John Doe",
        user_data={"plan": "pro"},
    )


def redact_emails(text: str) -> str:
    """Strip email addresses from all captured data before it leaves the process."""
    return EMAIL_RE.sub("[REDACTED_EMAIL]", text)


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
    server = FastMCP("fastmcp-advanced-example", version="1.0.0")

    project_id = (
        os.environ.get("AGENTCAT_PROJECT_ID")
        or os.environ.get("MCPCAT_PROJECT_ID")
        or "proj_YOUR_PROJECT_ID"
    )
    agentcat.track(
        server,
        project_id,
        AgentCatOptions(
            # Write debug logs to ~/agentcat.log. The default (None) defers to
            # the AGENTCAT_DEBUG_MODE env var; an explicit True/False wins.
            debug_mode=True,
            # Also inject a required agent_id parameter into every tool so
            # parallel agents on one session can be told apart. Off by
            # default; a call that omits it is never rejected server-side.
            enable_agent_tracking=True,
            identify=identify_user,
            redact_sensitive_information=redact_emails,
            # Both injected extras are ON by default; uncomment to opt out.
            # The "context" parameter powers user-intent analytics:
            #   enable_tool_call_context=False,
            # get_more_tools lets an agent report capabilities you don't
            # offer yet:
            #   enable_report_missing=False,
            #
            # Hook mode — you own correlation; no session_id parameter is
            # injected anywhere and no session instructions are shown to the
            # agent. Return your own ID (trace ID, workflow ID, a header) and
            # AgentCat derives the same ses_ session from it
            # deterministically. `extra` is the request context; on HTTP
            # transports `extra.request` is the incoming HTTP request:
            #   resolve_session_id=lambda request, extra: (
            #       extra.request.headers.get("x-correlation-id")
            #   ),
        ),
    )

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
