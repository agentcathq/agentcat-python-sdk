"""Observe what a facade's tool manager was actually handed.

The one seam on the official facades where a strip regression is visible.

A typed tool body cannot witness the strip. `def add_todo(text: str)` can only
ever report `text` — an argument that arrived and was dropped on the way in
leaves no trace in the body, so `seen["text"] = text` inside the function is
the same value whether or not AgentCat stripped anything.

And both official tool managers DO drop an undeclared argument silently.
Measured on mcp 1.29 (`mcp.server.fastmcp.FastMCP`) and mcp 2.0 (`MCPServer`):
``call_tool("add_todo", {"text": "hi", "session_id": "ses_X"})`` returns the
tool's normal result with no error. Only community FastMCP raises. So a test
that sends an extra parameter to a typed body and asserts "no error" proves
nothing about the strip — it passes identically with the strip disabled.

Wrapping the manager makes the delivered dict observable. Both eras spell the
seam identically (``call_tool(name, arguments, ...)``), and the wrapper is
installed by the server factories BEFORE ``track()``, so the adapter's inner
tap wraps the same attribute afterwards and sits above this recorder — what it
records is therefore post-strip delivery.

**Either order works, so do not "fix" a caller that installs this AFTER
``track()``** (`test_dynamic_tracking.py` does). The strip does not happen at
the tool manager at all: it runs at the lowlevel request-handler seam
(`modules/callpath.py`), which is above the manager on every flavor. So the
arguments reaching `call_tool` are already stripped no matter where this
wrapper sits in the manager's own decorator stack. The before-``track()``
ordering is a convention for the shared factories, not a correctness
requirement.
"""

from __future__ import annotations

from typing import Any

# Where each factory parks its recording, so tests have one name to read.
DELIVERED_ATTR = "delivered_arguments"


def record_delivered_arguments(
    manager: Any, seen: list[tuple[str, dict[str, Any]]]
) -> None:
    """Append ``(tool_name, arguments)`` to `seen` for every call `manager` runs."""
    original = manager.call_tool

    async def recording(name: str, arguments: dict[str, Any], *args: Any, **kw: Any):
        seen.append((name, dict(arguments or {})))
        return await original(name, arguments, *args, **kw)

    manager.call_tool = recording


def attach_delivery_recorder(server: Any, manager: Any) -> list[tuple[str, dict]]:
    """Record `manager`'s deliveries onto ``server.delivered_arguments``.

    Returns the same list the attribute holds, so a factory can hand it back
    directly. Call before ``track()``.
    """
    seen: list[tuple[str, dict[str, Any]]] = []
    record_delivered_arguments(manager, seen)
    setattr(server, DELIVERED_ATTR, seen)
    return seen


def delivered(server: Any) -> list[tuple[str, dict[str, Any]]]:
    """What `server`'s tool manager has been handed so far."""
    return getattr(server, DELIVERED_ATTR)


def delivered_arguments_for(server: Any, tool_name: str) -> list[dict[str, Any]]:
    """Just the argument dicts `tool_name` was called with, in order."""
    return [args for name, args in delivered(server) if name == tool_name]
