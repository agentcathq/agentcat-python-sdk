"""AgentCat - Analytics Tool for MCP Servers."""

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .utils import get_agentcat_version

# Guarded: on installs without distribution metadata (vendored source trees,
# PYTHONPATH, PyInstaller bundles) the lookup fails — that must degrade the
# version string, never crash the customer's import.
__version__ = get_agentcat_version() or "0.0.0"

from .modules.constants import AGENTCAT_CUSTOM_EVENT_TYPE
from .modules.detection import Detection, ServerFlavor, detect_server
from .modules.diagnostics import init_diagnostics
from .modules.internal import get_server_tracking_data, set_server_tracking_data
from .modules.logging import set_debug_mode, write_to_log
from .modules.validation import validate_tags
from .types import (
    AgentCatData,
    AgentCatOptions,
    CustomEventData,
    EventPropertiesFunction,
    EventTagsFunction,
    IdentifyFunction,
    RedactionFunction,
    ResolveSessionIdFunction,
    UnredactedEvent,
    UserIdentity,
)

# Flavors an adapter already exists for. Everything else is logged and returned
# untracked — track() never raises, so an unsupported server degrades to a
# no-op rather than breaking the customer's process at import/startup time.
_LOWLEVEL_V1_FLAVORS = (ServerFlavor.LOWLEVEL_V1, ServerFlavor.OFFICIAL_FASTMCP_V1)
_LOWLEVEL_V2_FLAVORS = (ServerFlavor.LOWLEVEL_V2, ServerFlavor.MCPSERVER_V2)
# Both community generations share one middleware; the era selects its field
# bridge and names the generation an installed middleware was built for. This
# map is also the single answer to "is this a community flavor", so a later
# generation is one entry here rather than a branch in two places.
#
# The values are `adapters.community.ERA_V3` / `ERA_V4`, spelled out rather
# than imported: importing that module at module scope would pull the event
# queue — and its worker thread, executor and signal handlers — into every
# `import agentcat`. Every adapter import in this file is deferred for the same
# reason. Only the V4 pairing is pinned by a test that runs
# (`tests/test_community_v4_handles.py`); the V3 one is not, because the suite
# that could assert it is collected under the OTHER mcp major, where `ERA_V3`
# is unreachable from a modern-env test. The era is a log/diagnostic label, so
# a wrong pairing would misreport rather than misbehave.
_COMMUNITY_ERAS = {ServerFlavor.COMMUNITY_V3: 3, ServerFlavor.COMMUNITY_V4: 4}


def track(
    server: Any, project_id: str | None = None, options: AgentCatOptions | None = None
) -> Any:
    """
    Initialize AgentCat tracking with optional telemetry export.

    Args:
        server: MCP server instance to track
        project_id: AgentCat project ID (optional if using telemetry-only mode)
        options: Configuration options including telemetry exporters

    Returns:
        The server instance, tracked when its shape is one AgentCat supports.

    Never raises. A misconfiguration (no project_id and no exporters), an
    unsupported server generation, or an unrecognized object is logged to
    ~/agentcat.log and mirrored to SDK diagnostics; the server comes back
    untracked so an analytics problem can never take a customer's MCP server
    down. This is a 2.0 breaking change — 1.x raised ValueError/TypeError.

    Example:
        Attach custom metadata to every auto-captured event using
        `event_tags` (string key-value pairs, validated) and
        `event_properties` (flexible JSON). See
        https://docs.agentcat.com/sdk/event-tags-properties.

        >>> import os, agentcat
        >>> agentcat.track(server, "proj_abc123", agentcat.AgentCatOptions(
        ...     event_tags=lambda req, ctx: {"env": os.environ.get("APP_ENV", "dev")},
        ...     event_properties=lambda req, ctx: {"feature_flags": ["dark_mode"]},
        ... ))
    """
    try:
        if options is None:
            options = AgentCatOptions()

        if options.debug_mode is not None:
            set_debug_mode(options.debug_mode)

        # Initialize internal diagnostics before anything can fail, so even an
        # invalid setup still emits a failure beacon. Never throws into the host.
        init_diagnostics(project_id, disabled=options.disable_diagnostics)

        _apply_tracking(server, project_id, options)
    except Exception as e:
        write_to_log(f"Error initializing AgentCat: {e}")

    return server


def _apply_tracking(
    server: Any, project_id: str | None, options: AgentCatOptions
) -> None:
    """Detect the server's shape and install the matching adapter."""
    if not project_id and not options.exporters:
        write_to_log(
            "Warning: Failed to track server - neither project_id nor exporters "
            "were provided. Use project_id for AgentCat, exporters for "
            "telemetry-only mode, or both. Server returned untracked."
        )
        return

    detection = detect_server(server)
    is_lowlevel_v1 = detection.flavor in _LOWLEVEL_V1_FLAVORS
    is_lowlevel_v2 = detection.flavor in _LOWLEVEL_V2_FLAVORS

    community_era = _COMMUNITY_ERAS.get(detection.flavor)

    if is_lowlevel_v1 or is_lowlevel_v2:
        # The adapter wraps the lowlevel object, which for official FastMCP is
        # its `_mcp_server` and for MCPServer its `_lowlevel_server` — that is
        # also where the tracking data is keyed.
        target = detection.lowlevel
    elif community_era is not None:
        # Community flavors are adapted via middleware on the server itself.
        target = server
    else:
        _log_unsupported(detection)
        return

    write_to_log(
        f"AgentCat setup started | project {project_id or '(telemetry-only)'} | "
        f"server {detection.flavor.value}"
    )

    if options.exporters:
        from agentcat.modules.event_queue import set_telemetry_manager
        from agentcat.modules.telemetry import TelemetryManager

        set_telemetry_manager(TelemetryManager(options.exporters))
        write_to_log(f"Telemetry initialized with {len(options.exporters)} exporter(s)")

    # Server identity is captured once, here: it is static for the life of the
    # server and there is no session cache left to re-read it from at publish
    # time.
    data = AgentCatData(
        project_id=project_id,
        options=options,
        server_name=getattr(target, "name", None),
        server_version=getattr(target, "version", None),
    )
    set_server_tracking_data(target, data)

    # Resolve API base URL: option > new env var > legacy env var > default
    api_base_url = (
        options.api_base_url
        or os.environ.get("AGENTCAT_API_URL")
        or os.environ.get("MCPCAT_API_URL")
    )
    if api_base_url:
        from agentcat.modules.event_queue import event_queue

        event_queue.configure(api_base_url)

    # The high-level object that owns the lowlevel one, when the two differ.
    # Only the inner tap needs it: on an official FastMCP or an MCPServer the
    # customer's tool sits below a tool manager the lowlevel object cannot
    # reach, and that is where a raised exception has to be recorded before the
    # SDK flattens it into an isError result.
    facade = server if server is not target else None

    if is_lowlevel_v1:
        from agentcat.modules.adapters.lowlevel_v1 import install_lowlevel_v1

        install_lowlevel_v1(target, data, facade)
    elif is_lowlevel_v2:
        from agentcat.modules.adapters.lowlevel_v2 import install_lowlevel_v2

        install_lowlevel_v2(target, data, facade)
    elif community_era is not None:
        from agentcat.modules.adapters.community import install_community

        install_community(server, data, era=community_era)

    # Metadata-only setup-complete beacon (INFO). A start-without-complete
    # signals a failed setup.
    write_to_log(
        f"AgentCat setup complete | project {project_id or '(telemetry-only)'} | "
        f"tracing={options.enable_tracing} "
        f"context={options.enable_tool_call_context} "
        f"report_missing={options.enable_report_missing} "
        f"agent_tracking={options.enable_agent_tracking} "
        f"hook_mode={options.resolve_session_id is not None} "
        f"exporters={len(options.exporters) if options.exporters else 0}"
    )


def _log_unsupported(detection: Detection) -> None:
    """Explain why a server was not tracked, loudly enough to reach diagnostics.

    write_to_log tees every entry to the SDK diagnostics sink, so the
    unrecognized-shape line below IS the fleet-drift beacon: the full probe
    fingerprint travels with it, which is how a new upstream server shape gets
    noticed across the installed base (cross-SDK changelog 6.7).

    Every flavor the classifier can name now has an adapter, so the only two
    outcomes left are the one shape that is deliberately refused (FastMCP 2.x)
    and the fingerprint beacon. A generation newer than this build classifies
    as UNKNOWN rather than as itself, so it reaches the beacon — which carries
    more than a "not supported yet" line could.
    """
    if detection.flavor is ServerFlavor.COMMUNITY_V2_UNSUPPORTED:
        write_to_log(
            "Warning: Failed to track server - FastMCP 2.x is not supported by "
            "agentcat>=2. Pin agentcat<2 or upgrade to FastMCP 3.x or 4.x. "
            "Server returned untracked."
        )
    else:
        write_to_log(
            "Warning: Failed to track server - Unrecognized MCP server shape; "
            f"server returned untracked | fingerprint={detection.fingerprint}"
        )


def publish_custom_event(
    server_or_session_id: Any,
    project_id: str,
    event_data: CustomEventData | None = None,
) -> None:
    """Publish one `agentcat:custom` event for work that is not a tool call.

    A background job, a webhook, a checkout step — anything the customer wants
    on the same timeline as the tool calls AgentCat captures automatically.

    Args:
        server_or_session_id: a tracked MCP server, or a session ID string. A server
            tracked with `enable_tracing=False` publishes nothing, the same
            silence its tool calls keep; a session ID string carries no options to
            consult, so that form always publishes.
        project_id: the AgentCat project. Used as-is in the session-ID-string
            form; in the tracked-server form the project captured at `track()`
            time wins, so a telemetry-only server still publishes.
        event_data: optional `CustomEventData`. `session_id` attributes the event
            to a session **verbatim** — never validated, trimmed, prefixed or
            derived, since it is a deliberate server-side call rather than an
            agent's guess, so a
            caller's own correlation ID arrives exactly as given. It takes
            precedence over a session ID string. Without one the event publishes
            without a session: handles are per-request in v2, so a tracked server
            has no ambient session to fall back on.

    Fire-and-forget: never raises, and never lets one unusable field cost the
    whole event. Two different rules get it there. A `parameters`, `response`
    or `properties` value that is not a dict is WRAPPED — `response="shipped"`
    is recorded as `{"value": "shipped"}` — because the wire field holds an
    object and the payload is the caller's to keep (a non-dict `error` becomes
    `{"message": ...}`, the shape its consumers read). A mistyped
    `resource_name`, `message`, `duration`, `is_error` or `tags` is DROPPED,
    because those wire fields are strictly typed and a near miss carries no
    payload worth preserving. Both cases log; the event publishes either way.

    TS parity — `publishCustomEvent`, design §3.4.

    Example:
        >>> import agentcat
        >>> agentcat.publish_custom_event(server, "proj_abc123", {
        ...     "session_id": "ses_2cOHEO0LYGADMzRvWTXXVbbgxgm",
        ...     "resource_name": "checkout",
        ...     "message": "order confirmed",
        ... })
    """
    try:
        _publish_custom_event(server_or_session_id, project_id, event_data)
    except Exception as e:
        write_to_log(f"Warning: failed to publish custom event - {e}")


def _publish_custom_event(
    server_or_session_id: Any, project_id: str, event_data: CustomEventData | None
) -> None:
    """The body of `publish_custom_event`, minus the never-raises wrapper."""
    # Imported here, not at module scope: importing the event queue constructs
    # the global queue (worker thread, executor, signal handlers), which must
    # not happen merely because someone imported `agentcat`.
    from agentcat.modules import event_queue as queue_module
    from agentcat.utils import get_agentcat_version

    data: Mapping[str, Any] = event_data if isinstance(event_data, dict) else {}
    if event_data is not None and not isinstance(event_data, dict):
        write_to_log(
            "publish_custom_event: event_data is not a dict "
            f"(got {type(event_data).__name__}); publishing without it"
        )

    session_id = _verbatim_session_id(data.get("session_id"))
    project = _wire_scalar("project_id", project_id, str)
    server: Any = None
    if isinstance(server_or_session_id, str):
        session_id = session_id or server_or_session_id
        if not project:
            write_to_log(
                "publish_custom_event: project_id is required when the first "
                "argument is a session ID string; event dropped"
            )
            return
    else:
        server = server_or_session_id
        tracking = _tracking_data(server)
        if tracking is None:
            write_to_log(
                "publish_custom_event: first argument is neither a tracked "
                f"server nor a session ID string (got {type(server).__name__}); "
                "event dropped. Call agentcat.track() first, or pass a session ID."
            )
            return
        if not tracking.options.enable_tracing:
            # Every other event type is gated on this inside the adapters, and
            # TS gates it inside publishEvent itself; Python's publish_event
            # has no gate, so this entry point carries its own. A customer who
            # turned tracing off does not start emitting a new event type.
            write_to_log(
                "publish_custom_event: tracing is disabled for this server "
                "(enable_tracing=False); event dropped"
            )
            return

    tags = data.get("tags")
    if tags is not None and not isinstance(tags, dict):
        write_to_log(
            "publish_custom_event: dropping 'tags' - expected dict, "
            f"got {type(tags).__name__}"
        )
        tags = None

    error = data.get("error")
    if error is not None and not isinstance(error, dict):
        # `error` has a known shape (`ErrorData`) and consumers read its
        # message, so a bare value becomes one rather than a nested payload.
        error = {"message": str(error)}

    event = UnredactedEvent(
        session_id=session_id or None,
        project_id=project or None,
        event_type=AGENTCAT_CUSTOM_EVENT_TYPE,
        timestamp=datetime.now(timezone.utc),
        resource_name=_wire_scalar("resource_name", data.get("resource_name"), str),
        user_intent=_wire_scalar("message", data.get("message"), str),
        duration=_wire_scalar("duration", data.get("duration"), int),
        is_error=_wire_scalar("is_error", data.get("is_error"), bool),
        parameters=_wire_payload("parameters", data.get("parameters")),
        response=_wire_payload("response", data.get("response")),
        properties=_wire_payload("properties", data.get("properties")),
        error=error,
        tags=validate_tags(tags) if isinstance(tags, dict) else None,
        sdk_language=queue_module.SDK_LANGUAGE,
        agentcat_version=get_agentcat_version(),
    )

    if server is not None:
        # The tracked path stamps the project, the server identity captured at
        # track() time and the customer's redaction hook — the same publish
        # every tool-call event goes through.
        queue_module.publish_event(server, event)
    else:
        queue_module.event_queue.add(event)

    where = (
        f"for session {event.session_id}" if event.session_id else "without a session"
    )
    write_to_log(f"Published custom event {where} | {AGENTCAT_CUSTOM_EVENT_TYPE}")


def _tracking_data(server: Any) -> AgentCatData | None:
    """This server's tracking data, or None if it is not a server at all.

    The lookup is keyed by weak reference, so an int, a string or a dict raises
    rather than missing — all three mean the same thing here.
    """
    try:
        return get_server_tracking_data(server)
    except Exception:
        return None


def _verbatim_session_id(value: Any) -> str | None:
    """`event_data["session_id"]` if it can be sent as-is, else nothing.

    Verbatim cuts both ways: a non-string is not stringified into a session ID the
    caller never chose, it is dropped and the event lands untethered.
    """
    if value is None or isinstance(value, str):
        return value
    write_to_log(
        f"publish_custom_event: ignoring non-string session_id ({type(value).__name__})"
    )
    return None


def _wire_scalar(field: str, value: Any, kind: type) -> Any:
    """A value the strict wire field can hold, or None.

    `PublishEventRequest` types these `StrictStr`/`StrictInt`/`StrictBool`, so a
    near miss (`1` for a bool, `12.5` for an int) fails construction and takes
    the entire event with it. One unusable field is not worth an event.
    """
    if value is None or (
        isinstance(value, kind) and (kind is bool or not isinstance(value, bool))
    ):
        return value
    write_to_log(
        f"publish_custom_event: dropping '{field}' - expected {kind.__name__}, "
        f"got {type(value).__name__}"
    )
    return None


def _wire_payload(field: str, value: Any) -> dict[str, Any] | None:
    """A dict for the dict-shaped wire fields, wrapping whatever is not one.

    `parameters`/`response`/`properties` are `Optional[Dict[str, Any]]` while
    `CustomEventData` types them `Any`, so a string or a list is a plausible
    input rather than a bug. The adapters discard a non-dict `response`
    (`adapters/_common.response_payload`) because there it is a serialization
    anomaly with no author; here it is exactly what the caller asked to record,
    so it is kept.
    """
    if value is None or isinstance(value, dict):
        return value
    write_to_log(
        f"publish_custom_event: wrapping non-dict '{field}' "
        f"({type(value).__name__}) as {{'value': ...}}"
    )
    return {"value": value}


__all__ = [
    # Main API
    "track",
    "publish_custom_event",
    # Configuration
    "AgentCatOptions",
    # Type for publish_custom_event payloads
    "CustomEventData",
    # Types for identify functionality
    "UserIdentity",
    "IdentifyFunction",
    # Type for redaction functionality
    "RedactionFunction",
    # Types for event metadata callbacks
    "EventTagsFunction",
    "EventPropertiesFunction",
    "ResolveSessionIdFunction",
]
