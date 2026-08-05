"""Helpers every adapter needs, in one place so they cannot drift apart.

A few small things, extracted because four adapters (lowlevel v1/v2, community
v3/v4) each want them and two of them encode rules that must never differ
between eras: a non-dict ``response`` silently drops the whole event, and
per-server install state must not outlive the server.

Nothing version-specific from ``mcp``/``fastmcp`` is imported here, so this
module loads under either SDK major.
"""

from __future__ import annotations

import time
from typing import Any

from agentcat.modules.internal import get_server_tracking_data
from agentcat.modules.logging import write_to_log
from agentcat.types import AgentCatData

# Where an adapter's per-server install state is kept: on the server, in one
# private attribute. See `install_state`.
STATE_ATTR = "_agentcat_install_state"


def now_ms() -> int:
    """A monotonic millisecond stamp for measuring one call's duration."""
    return int(time.monotonic() * 1000)


def install_state(server: Any) -> dict[str, Any] | None:
    """This server's install state, created on first use. None if unstorable.

    Every value an adapter puts in here closes over the server — the handler
    wrappers do, and so does a customer handler bound to a
    ``FastMCP``/``MCPServer`` facade that owns the lowlevel object — so a
    module-level ``WeakKeyDictionary`` would be kept alive by its own values
    and the weak key would never die.

    That is not a bounded cost on the topology this generation documents: 2026
    factories build a fresh server per request and ``track()`` runs inside the
    factory, with the explicit requirement that "per-server state lives in maps
    that don't outlive the server object" (cross-SDK changelog §6.8). A
    module-level map means one immortal server — plus its handler table, its
    deep-copied tool schemas, its ``AgentCatData`` and its injection registries
    — PER REQUEST, which is a linear leak.

    Held on the server, the state's lifetime is exactly the server's. The
    reference cycle it forms with the wrappers is the same one the server's own
    handler table already holds, and the cyclic collector takes both.

    What the state is FOR is idempotence: a repeated ``track()`` finds its
    predecessor's wrapper in the handler table and takes the customer's handler
    from the recorded original instead of wrapping the wrapper. A stacked pass
    would inject session_id on the inside, find it already present on the outside,
    never record it as strippable, and hand the customer's tool a parameter it
    never declared.
    """
    existing = getattr(server, STATE_ATTR, None)
    if isinstance(existing, dict):
        return existing
    state: dict[str, Any] = {}
    try:
        setattr(server, STATE_ATTR, state)
    except Exception as e:
        # Nothing that reaches an adapter can refuse an attribute — re-arming
        # sets one too — but a server that did would leave no way to recognize
        # our own wrapper on a second track(), and a stacked wrapper is worse
        # than no tracking.
        write_to_log(
            "Warning: could not store AgentCat state on this server, so a "
            f"repeated track() could not stay idempotent; not tracking it - {e}"
        )
        return None
    return state


def current_tracking_data(server: Any, fallback: AgentCatData) -> AgentCatData:
    """The tracking data as of this request, so a re-``track()`` takes effect.

    Adapters must never capture the data at install time: a second
    ``track(server, ...)`` stores a fresh ``AgentCatData``, and a handler still
    reading the first one would serve stale options forever. ``fallback`` is
    the install-time data, used only if the lookup finds nothing.
    """
    try:
        live = get_server_tracking_data(server)
    except Exception:
        live = None
    return live if live is not None else fallback


def response_payload(result: Any) -> dict[str, Any] | None:
    """A JSON-safe dict for the event's ``response`` field, or None.

    ``PublishEventRequest.response`` is ``Optional[Dict[str, Any]]``: handing
    it anything else fails pydantic construction and silently drops the whole
    event, so a non-dict dump is discarded here instead.

    **The dump is era-native, deliberately.** No ``by_alias``, so the keys are
    whatever the result model this generation produced calls them: official
    mcp 1.x publishes ``isError`` / ``structuredContent``, while mcp 2.x and
    both community eras publish ``is_error`` / ``structured_content``. The
    divergence is RULED IN, not an oversight:

    - it is not a regression — v1 community already published FastMCP's
      snake_case dump, so normalizing would CHANGE data the backend has been
      receiving rather than fix it;
    - ``to_mcp_result()`` is not a drop-in normalizer: it returns a bare list
      or a tuple for a non-error result, and this field must be a dict;
    - the wire result the agent receives is untouched either way. This is the
      analytics payload, not the tool's answer.

    ``tests/test_response_shape.py`` pins the actual spelling per flavor.
    Adding ``by_alias`` here is a breaking change to published data; change
    this ruling first if you mean to make it.
    """
    try:
        dumped = result.model_dump(mode="json")
    except Exception as e:
        write_to_log(f"Warning: could not serialize tool result for the event - {e}")
        return None
    return dumped if isinstance(dumped, dict) else None
