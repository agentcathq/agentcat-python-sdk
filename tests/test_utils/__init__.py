"""Test utilities for AgentCat tests."""

import os
from importlib.metadata import version
from pathlib import Path

import pytest

LOG_FILE = "agentcat.log"

MCP_MAJOR = int(version("mcp").split(".")[0])

# For the integration tests inside otherwise era-agnostic modules. `conftest.py`
# gates whole FILES by era; a module that mixes plain unit tests with a class
# built on `create_todo_server()` / `create_test_client()` (both mcp 1.x-only)
# would lose its unit tests to that gate, so it marks just the class instead.
LEGACY_ONLY = pytest.mark.skipif(
    MCP_MAJOR >= 2,
    reason="built on the mcp 1.x FastMCP + in-memory client harness",
)

# The other half of the same gate, for a module whose eras belong side by side:
# `test_inner_tap.py` proves one contract on every generation, so splitting it
# across the two conftest-gated trees would hide the parity it exists to show.
MODERN_ONLY = pytest.mark.skipif(
    MCP_MAJOR < 2,
    reason="built on the mcp 2.x MCPServer + in-process Client harness",
)


def sid(label: str) -> str:
    """A valid 27-char session ID that still reads as its label in failures.

    `resolve_handles` only honors IDs shaped like the ones this SDK issues, so
    a fixture cannot be `"ses_parent"` any more. Real KSUIDs are opaque; test
    fixtures should not be, hence the label survives in the body.
    """
    body = ("".join(c for c in label if c.isalnum()) + "0" * 27)[:27]
    return f"ses_{body}"


def cleanup_log_file():
    """Remove the log file if it exists."""
    if os.path.exists(LOG_FILE):
        os.unlink(LOG_FILE)


@pytest.fixture(autouse=True)
def setup_test_environment():
    """Set up and tear down test environment."""
    # Clean up before test
    cleanup_log_file()

    yield

    # Clean up after test
    cleanup_log_file()
