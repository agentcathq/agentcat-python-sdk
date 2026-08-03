"""Type definitions for AgentCat."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, NotRequired, Optional, TypedDict, Union

from agentcat_api import PublishEventRequest
from pydantic import field_validator

from agentcat.modules.constants import (
    AGENTCAT_CUSTOM_EVENT_TYPE,
    DEFAULT_CONTEXT_DESCRIPTION,
)

# Type alias for identify function.
#
# The first argument is the tool call's REQUEST PARAMS — the object carrying
# `.name` and `.arguments` — never the enclosing JSON-RPC request. That is the
# one shape every adapter can produce: mcp 2.x hands its handler `(ctx, params)`
# with no request object in scope, and community FastMCP's `context.message` is
# params as well, so the official 1.x adapter unwraps `request.params` to match.
# Typed `Any` rather than a protocol because the concrete class differs per
# generation (`mcp.types.CallToolRequestParams` on the official SDKs,
# `fastmcp`'s own model on community). The second argument is the adapter's
# request context, whatever it holds on that flavor.
#
# `event_tags`, `event_properties` and `resolve_session_id` all receive this same
# pair — see EventTagsFunction / EventPropertiesFunction / ResolveSessionIdFunction.
IdentifyFunction = Callable[[Any, Any], Optional["UserIdentity"]]
# Type alias for redaction function
RedactionFunction = Callable[[str], str | Awaitable[str]]
# Type alias for event_tags callback — returns str:str map attached to every auto-captured event.
# Accepts sync or async callables (mirrors RedactionFunction).
EventTagsFunction = Callable[
    [Any, Any],
    Optional[dict[str, str]] | Awaitable[Optional[dict[str, str]]],
]
# Type alias for event_properties callback — returns JSON-serializable map attached to every auto-captured event.
# Accepts sync or async callables.
EventPropertiesFunction = Callable[
    [Any, Any],
    Optional[dict[str, Any]] | Awaitable[Optional[dict[str, Any]]],
]
# Type alias for the resolve_session_id hook — returns the caller-managed
# session ID, or None. Accepts sync or async callables (mirrors
# RedactionFunction).
ResolveSessionIdFunction = Callable[[Any, Any], str | None | Awaitable[str | None]]


@dataclass
class UserIdentity:
    """User identification data."""

    user_id: str
    user_name: str | None
    user_data: dict[str, str] | None


class Event(PublishEventRequest):
    # The generated client marks project_id as required on the wire, but the SDK
    # constructs events before the project ID is known: event_queue merges it in
    # at publish time, and telemetry-only mode sends events without one.
    project_id: Optional[str] = None

    @field_validator("event_type")
    @classmethod
    def event_type_validate_enum(cls, value: str | None) -> str | None:
        """Validate against what THIS SDK publishes, not the generated enum.

        `agentcat_api` 1.0.0 enforces the OpenAPI spec's `event_type` list,
        which predates `agentcat:custom` — the backend has accepted that type
        since TS 2.0, but the generated model rejects it. Left alone, every
        custom event would fail construction inside the publish path's
        try/except and vanish without a trace. `EventType` below is the source
        of truth for the (two) types v2 emits; it is resolved at call time, so
        forward-declaring it here is fine.

        The NAME is load-bearing: pydantic keys collected validators by
        attribute name, so this supersedes the generated one only because it
        matches `agentcat_api`'s exactly. Renaming it to something more
        idiomatic silently reinstates the stale enum — and silently re-breaks
        custom events. `tests/test_publish_custom_event.py` has the tripwire.
        """
        if value is None or value in {member.value for member in EventType}:
            return value
        raise ValueError(
            "must be one of " + str(sorted(member.value for member in EventType))
        )


# Error tracking types


class StackFrame(TypedDict, total=False):
    """Stack frame information for error tracking."""

    filename: str
    abs_path: str
    function: str  # Function name or "<module>"
    module: str
    lineno: int
    in_app: bool
    context_line: NotRequired[str]


class ChainedErrorData(TypedDict, total=False):
    """Chained exception data (from __cause__ or __context__)."""

    message: str
    type: NotRequired[str | None]
    stack: NotRequired[str]
    frames: NotRequired[list[StackFrame]]


class ErrorData(TypedDict, total=False):
    """Complete error information for an exception."""

    message: str
    type: NotRequired[
        str | None
    ]  # Exception class name (e.g., "ValueError", "TypeError")
    stack: NotRequired[str]
    frames: NotRequired[list[StackFrame]]
    chained_errors: NotRequired[list[ChainedErrorData]]
    platform: str  # Platform identifier (always "python")


class EventType(str, Enum):
    """The event types AgentCat 2.0 publishes.

    Two, and only two. `mcp:initialize`, `mcp:tools/list` and
    `agentcat:identify` are gone (changelog §3.1): tools/list is still
    intercepted for schema injection but emits nothing, and the actor the
    `identify` hook returns rides on the tool-call event instead.
    """

    MCP_TOOLS_CALL = "mcp:tools/call"
    AGENTCAT_CUSTOM = AGENTCAT_CUSTOM_EVENT_TYPE


class UnredactedEvent(Event):
    redaction_fn: RedactionFunction | None = None


# Telemetry Exporter Configuration Types


class OTLPExporterConfig(TypedDict, total=False):
    """Configuration for OpenTelemetry Protocol (OTLP) exporter."""

    type: Literal["otlp"]
    endpoint: str  # Optional, defaults to http://localhost:4318/v1/traces
    protocol: Literal["http/protobuf", "grpc"]  # Optional, defaults to http/protobuf
    headers: dict[str, str]  # Optional custom headers
    compression: Literal["gzip", "none"]  # Optional compression


class DatadogExporterConfig(TypedDict):
    """Configuration for Datadog exporter."""

    type: Literal["datadog"]
    api_key: str  # Required - Datadog API key
    site: str  # Required - Datadog site (e.g., datadoghq.com, datadoghq.eu)
    service: str  # Required - Service name for Datadog
    env: Optional[str]  # Optional environment name


class SentryExporterConfig(TypedDict):
    """Configuration for Sentry exporter."""

    type: Literal["sentry"]
    dsn: str  # Required - Sentry DSN
    environment: Optional[str]  # Optional environment name
    release: Optional[str]  # Optional release version
    enable_tracing: Optional[bool]  # Optional, defaults to True


# Union type for all exporter configurations
ExporterConfig = Union[OTLPExporterConfig, DatadogExporterConfig, SentryExporterConfig]


@dataclass
class AgentCatOptions:
    """Configuration options for AgentCat."""

    enable_report_missing: bool = True
    enable_tracing: bool = True
    enable_tool_call_context: bool = True
    custom_context_description: str = DEFAULT_CONTEXT_DESCRIPTION
    identify: IdentifyFunction | None = None
    redact_sensitive_information: RedactionFunction | None = None
    exporters: dict[str, ExporterConfig] | None = None
    debug_mode: bool = False
    api_base_url: str | None = None
    # Disables AgentCat's internal SDK diagnostics — anonymous, metadata-only
    # setup/error reporting used to detect failed installs. On by default; also
    # disable-able via the DISABLE_DIAGNOSTICS env var. Automatically disabled in
    # test environments (PYTEST_CURRENT_TEST / PYTEST_VERSION set) so test suites
    # never send anything; set DISABLE_DIAGNOSTICS=false to re-enable there. Never
    # sends event payloads or user data; the local ~/agentcat.log is unaffected.
    disable_diagnostics: bool = False
    # Callback invoked on every auto-captured event (one per tool call) to
    # attach string key-value tags. Tags are intended for
    # structured metadata you'll filter or group by in the AgentCat dashboard
    # (e.g. trace IDs, environments, regions). Validated client-side: keys
    # must be <=32 chars matching [a-zA-Z0-9$_.:\- ], values must be strings
    # <=200 chars without newlines, max 50 entries per event. Invalid entries
    # are dropped with a warning logged to ~/agentcat.log when debug_mode=True.
    # May be sync or async. Receives the same (request, extra) arguments as
    # `identify`. If the callback raises or returns None/{}, tags are omitted.
    event_tags: EventTagsFunction | None = None
    # Callback invoked on every auto-captured event to attach JSON-serializable
    # metadata (device info, feature flags, nested context). No validation
    # beyond standard JSON types — note: the event is serialized via
    # model_dump_json() in the queue, so values must be JSON-serializable
    # (stricter than the TypeScript SDK). May be sync or async. If the callback
    # raises or returns None, properties are omitted.
    event_properties: EventPropertiesFunction | None = None
    # Default False. Set True to inject a required agent_id parameter into every
    # tool. Agents self-generate the value (model|harness|nonce, e.g.
    # "opus-4.80-1m|claude-code|k3n9x"); it is echoed back in _mcp_instructions
    # and stamped on events as tags. Omission never rejects a call server-side —
    # the event is simply published without agent identity. The intended
    # enforcement is client-side: a strict schema-validating MCP client will
    # refuse to send a call that omits a required agent_id in the first place.
    enable_agent_tracking: bool = False
    # Hook mode: you manage session state. When configured, AgentCat injects no
    # session_id parameter and prompts no session instructions; the returned value is
    # combined with the project ID into a deterministic ses_ KSUID. None returns
    # and raises mint silently — a configured hook should answer every request.
    # May be sync or async. Receives the same (request, extra) arguments as
    # `identify`.
    resolve_session_id: ResolveSessionIdFunction | None = None


@dataclass
class AgentCatData:
    """Per-server tracking state (spec §3.3).

    Nothing here is per-request. v1 kept a session ID, an idle clock, a cached
    metadata object and a registry of tools on this object; 2.0 resolves all of
    that per call, so what survives is the project, the customer's options, and
    the engine state that is genuinely per-server.
    """

    project_id: str | None
    options: AgentCatOptions

    # v2 engine state
    # Per-tool set of parameter names AgentCat injected into the schema
    # (context/session_id/agent_id), so they can be stripped before the tool runs.
    injected_params_registry: dict[str, set[str]] | None = None
    # Tools whose results receive an _mcp_instructions injection.
    output_injection_registry: set[str] | None = None
    # Tools whose OWN input schema declares `session_id` — the customer's
    # parameter, never ours to read at call time. Membership only ever grows;
    # a tool that once declared the name stays foreign until the process
    # restarts, which is the conservative direction, since the alternative is
    # adopting a value that is not ours into an unredactable field.
    declared_session_params: set[str] = field(default_factory=set)
    # Tools whose `session_id` collision has already been reported, so the
    # per-listing pipeline logs it once rather than for the life of the
    # process.
    reported_conflicts: set[str] = field(default_factory=set)
    # The server's original tools/list source, kept so wrapped lists can be
    # rebuilt from the unmodified definitions.
    original_list_source: Any = None
    # Server identity captured at track time; stamped onto every event at
    # publish time, since there is no session cache to read it from.
    server_name: str | None = None
    server_version: str | None = None


# Custom event types for the publish_custom_event function
class CustomEventData(TypedDict, total=False):
    """Optional fields describing a custom event."""

    # Session ID to attribute this event to.
    session_id: str
    resource_name: str
    parameters: Any
    response: Any
    message: str
    duration: int  # milliseconds
    is_error: bool
    error: Any
    tags: dict[str, str]
    properties: dict[str, Any]
