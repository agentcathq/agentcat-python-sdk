"""Test utilities for AgentCat tests."""

import functools
import os
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

LOG_FILE = "agentcat.log"

MCP_VERSION = tuple(int(p) for p in version("mcp").split(".")[:3] if p.isdigit())
MCP_MAJOR = MCP_VERSION[0]

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

# ── Upstream capability gates ────────────────────────────────────────────────
# Both of these were added by mcp 1.10.0 and both are probed on the capability
# rather than compared against a version, so a backport would be honoured and
# the probe cannot drift from what the test actually needs.
#
# The rest of the old-mcp work reaches past era-specific spellings, because the
# thing wanted was there under another name. These two are different: the seam
# is absent, so there is nothing to reach for. AgentCat still runs below them —
# it degrades to the surfaced message with no exception type, and mirrors no
# structured mint-back — which is why these gate tests rather than the package.

# `Server._make_error_result` is the seam `modules/adapters/_inner_tap.py`
# hooks to recover a handler's real exception before the SDK folds it into an
# `isError` result. mcp 1.10.0 introduced it (PR #1005, "Add schema validation
# to lowlevel server"), together with the lowlevel input validation whose
# message that same PR surfaces.
try:
    from mcp.server.lowlevel import Server as _LowlevelServer

    HAS_LOWLEVEL_ERROR_SEAM = hasattr(_LowlevelServer, "_make_error_result")
except Exception:  # pragma: no cover - import guard
    HAS_LOWLEVEL_ERROR_SEAM = False

NEEDS_LOWLEVEL_ERROR_SEAM = pytest.mark.skipif(
    not HAS_LOWLEVEL_ERROR_SEAM,
    reason=(
        "needs Server._make_error_result (mcp>=1.10) — below it the lowlevel "
        "server catches the handler's exception inline and the tap has no seam"
    ),
)

# Structured tool output as DECLARED fields — `Tool.outputSchema`,
# `CallToolResult.structuredContent`, and `mcp.server.fastmcp` deriving the
# schema from a return annotation — also arrived in mcp 1.10.0.
try:
    import mcp.types as _mcp_types

    HAS_STRUCTURED_OUTPUT = "outputSchema" in _mcp_types.Tool.model_fields
except Exception:  # pragma: no cover - import guard
    HAS_STRUCTURED_OUTPUT = False

NEEDS_STRUCTURED_OUTPUT = pytest.mark.skipif(
    not HAS_STRUCTURED_OUTPUT,
    reason="needs declared structured tool output (mcp>=1.10)",
)

# mcp 1.2.x awaits each incoming message before reading the next, so a server
# never has two tool calls in flight; "Made message handling concurrent"
# (da53a97e) landed in v1.3.0. Any proof built on simultaneous calls therefore
# cannot be satisfied on 1.2 no matter what AgentCat does — measured: peak
# in-flight is 1 of 5 on 1.2.1 and 5 of 5 on 1.9.4.
#
# A version compare rather than a probe, unusually: the capability is a
# scheduling property of the session loop with no symbol to inspect, and
# actually measuring it would mean booting a server at import time.
NEEDS_CONCURRENT_DISPATCH = pytest.mark.skipif(
    MCP_VERSION < (1, 3),
    reason="mcp<1.3 handles messages serially; simultaneous calls never overlap",
)


# Whether the installed community FastMCP models an error result at all.
# `ToolResult.is_error` arrived in fastmcp 3.4 (PR #4217); below it a plain
# `ToolResult` dumps three fields and carries no error key in either spelling.
# Probed on the model, so it cannot drift from what the assertions need.
try:
    from fastmcp.tools import ToolResult as _ToolResult

    FASTMCP_TOOLRESULT_HAS_IS_ERROR = "is_error" in _ToolResult.model_fields
except Exception:  # pragma: no cover - community extra not installed
    FASTMCP_TOOLRESULT_HAS_IS_ERROR = False


@functools.lru_cache(maxsize=1)
def _error_tool_result_class() -> Any:
    """Built on first use: subclassing needs `ToolResult` to exist, which it
    does not on a leg with no community FastMCP installed."""
    from fastmcp.tools import ToolResult
    from mcp.types import CallToolResult

    class ErrorToolResult(ToolResult):  # type: ignore[misc]
        """A tool result that reports an error WITHOUT anything having raised.

        FastMCP 3.4 added `ToolResult.is_error` and taught `to_mcp_result` to
        answer with a `CallToolResult` carrying `isError` (PR #4217). Below
        that the field does not exist and `to_mcp_result` has no branch that
        can set `isError` at all, so the scenario is inexpressible with the
        stock model — yet it is exactly what an error-handling middleware, and
        FastMCP's own proxy provider, produce.

        Both halves are supplied because both are read: the adapter takes
        `getattr(result, "is_error", False)` off the OBJECT
        (`adapters/community.py`), while the client sees only the WIRE. A
        double that set one and not the other would pass one assertion and
        quietly fail the other.

        `__init__` is an explicit signature on the 3.x line with no `**kwargs`,
        so the redeclared field cannot arrive through it — hence the wrapper.
        On 3.4+ this subclass is a no-op that defers to `super()`.
        """

        is_error: bool = False

        def __init__(self, *args: Any, is_error: bool = False, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self.is_error = is_error

        def to_mcp_result(self) -> Any:
            if not self.is_error:
                return super().to_mcp_result()
            return CallToolResult(
                content=self.content,
                structuredContent=self.structured_content,
                isError=True,
                _meta=self.meta,
            )

    return ErrorToolResult


def read_only_hint(tool: Any) -> Any:
    """`readOnlyHint` off a tool, whether or not this SDK models annotations.

    AgentCat hands `annotations` over as a plain mapping so it never has to
    import a type whose availability moved between generations. From mcp 1.7
    the `Tool.annotations` field exists and pydantic coerces the mapping into
    the model; below it there is no such field and the mapping is carried
    verbatim as an extra. Both spellings mean the same thing to a client.
    """
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return None
    if isinstance(annotations, dict):
        return annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    return getattr(
        annotations, "readOnlyHint", getattr(annotations, "read_only_hint", None)
    )


def error_tool_result(**kwargs: Any) -> Any:
    """An `is_error` tool result on every community FastMCP 3.x and 4.x.

    Use instead of `ToolResult(..., is_error=True)`, which is a TypeError
    below fastmcp 3.4.
    """
    return _error_tool_result_class()(**kwargs)


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
