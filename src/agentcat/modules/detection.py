"""Per-object server flavor classification (spec §8.1).

Decides which adapter wraps a customer's server using only signals readable
off the object in hand: class name, defining module prefix, and attribute
presence. It never imports version-specific MCP symbols and never raises
into the customer's process.

Two probe strengths, deliberately different:

- ``_probe`` (presence): the name resolves, or its getter raises something
  other than ``AttributeError`` — the attribute exists even when a lazy or
  proxy getter is unhappy. ``hasattr`` would re-raise those, so it is never
  used here.
- ``_get`` (retrieval): the value must actually come back. Used where the
  classifier hands the value onward (handler tables, the wrapped lowlevel
  object) — a table we cannot read is a shape we cannot adapt.

Every ``Detection`` carries all 13 fingerprint probes (spec §8.1); for
``UNKNOWN`` shapes the fingerprint is the payload of the fleet-drift
diagnostics beacon.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ServerFlavor(str, Enum):
    """Diagnostics-beacon wire strings; keep stable across releases."""

    LOWLEVEL_V1 = "lowlevel-v1"
    LOWLEVEL_V2 = "lowlevel-v2"
    OFFICIAL_FASTMCP_V1 = "official-fastmcp-v1"
    MCPSERVER_V2 = "mcpserver-v2"
    COMMUNITY_V3 = "community-v3"
    COMMUNITY_V4 = "community-v4"
    COMMUNITY_V2_UNSUPPORTED = "community-v2-unsupported"
    UNKNOWN = "unknown"


@dataclass
class Detection:
    """``lowlevel`` is the object the adapters wrap (``_mcp_server``,
    ``_lowlevel_server``, or the bare server itself); ``None`` for community
    flavors (adapted via middleware) and ``UNKNOWN`` (returned untracked)."""

    flavor: ServerFlavor
    lowlevel: Any | None
    fingerprint: dict[str, bool]


_MISSING = object()

# What the lowlevel-v2 handler-registration seam is called, best-known name
# first. It has been spelled both ways on the 2.x line — public
# ``add_request_handler`` in 2.0, private ``_add_request_handler`` on the
# development line before it — and a build that ships only the other spelling
# must not fall through to UNKNOWN and be returned untracked with no error.
# `adapters.lowlevel_v2` re-arms whichever one it finds, so this tuple is the
# single definition of that seam.
HANDLER_REGISTRATION_NAMES = ("add_request_handler", "_add_request_handler")


def _probe(server: Any, name: str) -> bool:
    try:
        getattr(server, name)
    except AttributeError:
        return False
    except Exception:
        return True
    return True


def _get(server: Any, name: str) -> Any:
    try:
        return getattr(server, name)
    except Exception:
        return _MISSING


def _dir_names(server: Any) -> list[str]:
    try:
        return dir(server)
    except Exception:
        return []


def _class_info(server: Any) -> tuple[str, str]:
    try:
        cls = type(server)
        name = getattr(cls, "__name__", "")
        module = getattr(cls, "__module__", "")
    except Exception:
        return "", ""
    return (
        name if isinstance(name, str) else "",
        module if isinstance(module, str) else "",
    )


def _fingerprint(server: Any, class_name: str) -> dict[str, bool]:
    return {
        "is_fastmcp_class": "FastMCP" in class_name,
        "has_local_provider": _probe(server, "_local_provider"),
        "has_add_middleware": _probe(server, "add_middleware"),
        "has_middleware": _probe(server, "middleware"),
        "has_tool_manager": _probe(server, "_tool_manager"),
        "has_mcp_server_attr": _probe(server, "_mcp_server"),
        "has_lowlevel_server_attr": _probe(server, "_lowlevel_server"),
        "has_extensions": (
            _probe(server, "_extensions") or _probe(server, "add_extension")
        ),
        "has_request_state_security": _probe(server, "_request_state_security"),
        "has_request_handlers": _probe(server, "request_handlers"),
        "has_private_request_handlers": _probe(server, "_request_handlers"),
        "has_add_request_handler": any(
            _probe(server, name) for name in HANDLER_REGISTRATION_NAMES
        ),
        "has_request_context": _probe(server, "request_context"),
    }


def _classify(
    server: Any, class_name: str, module: str
) -> tuple[ServerFlavor, Any | None]:
    is_fastmcp_class = "FastMCP" in class_name

    if module.startswith("fastmcp") and is_fastmcp_class:
        # fastmcp 2.x kept the official `_tool_manager` architecture; it is
        # unsupported and must win before the v3/v4 probe set looks, because
        # only the module prefix separates it from official FastMCP v1.
        if _probe(server, "_mcp_server") and _probe(server, "_tool_manager"):
            return ServerFlavor.COMMUNITY_V2_UNSUPPORTED, None
        if (
            _probe(server, "_local_provider")
            and _probe(server, "add_middleware")
            and _probe(server, "middleware")
            and not _probe(server, "_tool_manager")
        ):
            if (
                _probe(server, "add_extension")
                or _probe(server, "_extensions")
                or _probe(server, "_request_state_security")
            ):
                return ServerFlavor.COMMUNITY_V4, None
            return ServerFlavor.COMMUNITY_V3, None

    if module.startswith("mcp.server.fastmcp") and is_fastmcp_class:
        mcp_server = _get(server, "_mcp_server")
        if (
            mcp_server is not _MISSING
            and mcp_server is not None
            and _probe(server, "_tool_manager")
        ):
            return ServerFlavor.OFFICIAL_FASTMCP_V1, mcp_server

    lowlevel_server = _get(server, "_lowlevel_server")
    if (
        lowlevel_server is not _MISSING
        and lowlevel_server is not None
        and _probe(server, "_tool_manager")
    ):
        return ServerFlavor.MCPSERVER_V2, lowlevel_server

    names = _dir_names(server)
    # `request_context` via dir() only: on a real lowlevel v1 Server it is a
    # property that raises outside a request, so it must never be evaluated.
    if (
        isinstance(_get(server, "request_handlers"), dict)
        and "request_context" in names
    ):
        return ServerFlavor.LOWLEVEL_V1, server

    if (
        isinstance(_get(server, "_request_handlers"), dict)
        and any(_probe(server, name) for name in HANDLER_REGISTRATION_NAMES)
        and "request_context" not in names
    ):
        return ServerFlavor.LOWLEVEL_V2, server

    return ServerFlavor.UNKNOWN, None


def detect_server(server: Any) -> Detection:
    class_name, module = _class_info(server)
    flavor, lowlevel = _classify(server, class_name, module)
    return Detection(
        flavor=flavor,
        lowlevel=lowlevel,
        fingerprint=_fingerprint(server, class_name),
    )
