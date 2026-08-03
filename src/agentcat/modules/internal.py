"""Internal data storage for AgentCat."""

import inspect
import weakref
from typing import Any, Dict, Optional

from ..types import AgentCatData, UnredactedEvent

# The classifier's own presence probe, shared rather than copied — see
# `_get_server_key`. detection.py imports nothing from this module, so this
# does not close a cycle.
from .detection import _probe as _attribute_present
from .logging import write_to_log
from .validation import validate_tags

# WeakKeyDictionary to store data associated with server instances
_server_data_map: weakref.WeakKeyDictionary[Any, AgentCatData] = (
    weakref.WeakKeyDictionary()
)


def _get_server_key(server: Any) -> Any:
    """The object a server's tracking data is stored under.

    ``track()`` installs the adapter on the LOWLEVEL server for both official
    facades — an ``mcp.server.fastmcp.FastMCP``'s ``_mcp_server`` and an
    ``MCPServer``'s ``_lowlevel_server`` — and stores the data there, so a
    lookup holding the facade has to make the same hop. Without it,
    ``publish_custom_event(mcpserver, ...)`` looked up an object nothing was
    ever filed under and dropped the event as "not a tracked server".

    Applies detection.py's ``OFFICIAL_FASTMCP_V1`` and ``MCPSERVER_V2`` rules
    inline rather than calling ``detect_server``: this runs on every request,
    and a 13-probe sweep to answer one question is not worth paying for. Both
    rules are reproduced in full — class name, module prefix, a non-None inner
    server, and a ``_tool_manager``. The ``_tool_manager`` half is also what
    keeps a community FastMCP — which is tracked on ITSELF — out of both
    branches.

    The two probe strengths are the classifier's, not lookalikes, because the
    difference decides where data lands. ``_tool_manager`` is a PRESENCE test
    and reuses ``detection._probe`` itself: an attribute that exists but whose
    lazy or proxy getter raises, or that holds ``None``, still means "this is a
    facade". Reading it with ``getattr(..., None) is not None`` instead looked
    like a harmless tightening and was not — it made this function classify a
    facade the classifier calls ``MCPSERVER_V2`` as an ordinary server, so
    ``track()`` filed the data under ``_lowlevel_server`` while every later
    lookup keyed on the facade. That silent lost lookup is the exact bug this
    function exists to prevent, so the predicate is shared rather than
    re-spelled.

    The inner server is a RETRIEVAL test, matching detection's ``_get``, and
    ``is not None`` rather than truthiness: a facade whose ``_mcp_server``
    defines ``__bool__`` or ``__len__`` falsily is still the object ``track()``
    filed the data under.
    """
    try:
        cls = type(server)
        module = getattr(cls, "__module__", "")
        is_official_fastmcp = "FastMCP" in getattr(
            cls, "__name__", ""
        ) and module.startswith("mcp.server.fastmcp")
        has_tool_manager = _attribute_present(server, "_tool_manager")
        if is_official_fastmcp and has_tool_manager:
            mcp_server = getattr(server, "_mcp_server", None)
            if mcp_server is not None:
                return mcp_server
        lowlevel = getattr(server, "_lowlevel_server", None)
        if lowlevel is not None and has_tool_manager:
            return lowlevel
    except Exception:
        pass
    return server


def set_server_tracking_data(server: Any, data: AgentCatData) -> None:
    """Store AgentCat data for a server instance."""
    key = _get_server_key(server)
    _server_data_map[key] = data


def get_server_tracking_data(server: Any) -> AgentCatData | None:
    """Retrieve AgentCat data for a server instance."""
    key = _get_server_key(server)
    return _server_data_map.get(key, None)


def reset_server_tracking_data(server: Any) -> None:
    """Reset tracking data for a specific server (mainly for testing)."""
    key = _get_server_key(server)
    if key in _server_data_map:
        del _server_data_map[key]
        write_to_log(f"Reset tracking data for server {id(key)}")


def reset_all_tracking_data() -> None:
    """Reset all server tracking data (mainly for testing)."""
    _server_data_map.clear()
    write_to_log("Reset all server tracking data")


async def resolve_event_tags(
    data: AgentCatData, request: Any, extra: Any
) -> Optional[Dict[str, str]]:
    """Resolve the event_tags callback and return validated tags.

    Accepts sync or async callbacks. Returns None if no callback configured,
    callback returns nullish, or callback raises.
    """
    callback = data.options.event_tags if data and data.options else None
    if callback is None:
        return None

    try:
        result = callback(request, extra)
        if inspect.iscoroutine(result):
            result = await result
    except Exception as e:
        write_to_log(f"event_tags callback error: {e}")
        return None

    if not result:
        return None

    return validate_tags(result)


async def resolve_event_properties(
    data: AgentCatData, request: Any, extra: Any
) -> Optional[Dict[str, Any]]:
    """Resolve the event_properties callback and return the result.

    Accepts sync or async callbacks. Returns None if no callback configured,
    callback returns nullish, or callback raises.
    """
    callback = data.options.event_properties if data and data.options else None
    if callback is None:
        return None

    try:
        result = callback(request, extra)
        if inspect.iscoroutine(result):
            result = await result
    except Exception as e:
        write_to_log(f"event_properties callback error: {e}")
        return None

    return result or None


async def attach_event_metadata(
    event: UnredactedEvent,
    data: Optional[AgentCatData],
    request: Any,
    extra: Any,
) -> None:
    """Attach customer-defined tags and properties to an event before publish.

    Safe no-op if data is None or callbacks are unset. Failures in either
    callback are logged and swallowed — event is still published.
    """
    if data is None:
        return

    tags = await resolve_event_tags(data, request, extra)
    if tags:
        event.tags = tags

    properties = await resolve_event_properties(data, request, extra)
    if properties:
        event.properties = properties
