"""Type definitions for AgentCat."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional, Set, TypedDict, Literal, Union, NotRequired
from agentcat_api import PublishEventRequest
from pydantic import BaseModel

from agentcat.modules.constants import DEFAULT_CONTEXT_DESCRIPTION

# Type alias for identify function
IdentifyFunction = Callable[[dict[str, Any], Any], Optional["UserIdentity"]]
# Type alias for redaction function
RedactionFunction = Callable[[str], str | Awaitable[str]]
# Type alias for the event-level redaction hook — receives the full Event and
# returns a modified Event, or None to drop the event entirely.
# Accepts sync or async callables (mirrors RedactionFunction).
EventRedactionFunction = Callable[
    ["Event"],
    Union["Event", None, Awaitable[Union["Event", None]]],
]
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


@dataclass
class UserIdentity:
    """User identification data."""

    user_id: str
    user_name: str | None
    user_data: dict[str, str] | None


class SessionInfo(BaseModel):
    """Session information for tracking."""

    ip_address: Optional[str] = None
    sdk_language: Optional[str] = None
    agentcat_version: Optional[str] = None
    server_name: Optional[str] = None
    server_version: Optional[str] = None
    client_name: Optional[str] = None
    client_version: Optional[str] = None
    identify_actor_given_id: Optional[str] = None  # Actor ID for agentcat:identify events
    identify_actor_name: Optional[str] = None  # Actor name for agentcat:identify events
    identify_data: Optional[dict[str, Any]] = None


class Event(PublishEventRequest):
    # The generated client marks project_id as required on the wire, but the SDK
    # constructs events before the project ID is known: event_queue merges it in
    # at publish time, and telemetry-only mode sends events without one.
    project_id: Optional[str] = None


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
    """MCP event types."""

    MCP_PING = "mcp:ping"
    MCP_INITIALIZE = "mcp:initialize"
    MCP_COMPLETION_COMPLETE = "mcp:completion/complete"
    MCP_LOGGING_SET_LEVEL = "mcp:logging/setLevel"
    MCP_PROMPTS_GET = "mcp:prompts/get"
    MCP_PROMPTS_LIST = "mcp:prompts/list"
    MCP_RESOURCES_LIST = "mcp:resources/list"
    MCP_RESOURCES_TEMPLATES_LIST = "mcp:resources/templates/list"
    MCP_RESOURCES_READ = "mcp:resources/read"
    MCP_RESOURCES_SUBSCRIBE = "mcp:resources/subscribe"
    MCP_RESOURCES_UNSUBSCRIBE = "mcp:resources/unsubscribe"
    MCP_TOOLS_CALL = "mcp:tools/call"
    MCP_TOOLS_LIST = "mcp:tools/list"
    AGENTCAT_IDENTIFY = "agentcat:identify"


class UnredactedEvent(Event):
    redaction_fn: RedactionFunction | None = None
    redact_event_fn: EventRedactionFunction | None = None  # Whole-event redaction hook


@dataclass
class ToolRegistration:
    """Metadata about a registered tool."""

    name: str
    registered_at: datetime
    tracked: bool = False
    wrapped: bool = False


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
    # Event-level redaction hook invoked with the full event (inspect
    # resource_name, event_type, parameters, response, etc.) before it is
    # published. Return a modified event, or None to drop the event entirely.
    # May be sync or async. Runs before redact_sensitive_information, so it
    # sees raw, unredacted values; the string-level hook, sanitization, and
    # truncation still run on its output. The system-managed fields id,
    # session_id, project_id, event_type, and timestamp cannot be changed
    # (id may be None at hook time). If the hook raises, the event is dropped
    # and the error is logged to ~/agentcat.log.
    redact_event: EventRedactionFunction | None = None
    exporters: dict[str, ExporterConfig] | None = None
    debug_mode: bool = False
    api_base_url: str | None = None
    stateless: bool | None = None
    # Disables AgentCat's internal SDK diagnostics — anonymous, metadata-only
    # setup/error reporting used to detect failed installs. On by default; also
    # disable-able via the DISABLE_DIAGNOSTICS env var. Automatically disabled in
    # test environments (PYTEST_CURRENT_TEST / PYTEST_VERSION set) so test suites
    # never send anything; set DISABLE_DIAGNOSTICS=false to re-enable there. Never
    # sends event payloads or user data; the local ~/agentcat.log is unaffected.
    disable_diagnostics: bool = False
    # Callback invoked on every auto-captured event (initialize, tools/list,
    # tools/call) to attach string key-value tags. Tags are intended for
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


@dataclass
class AgentCatData:
    """Internal data structure for tracking."""

    project_id: str | None
    session_id: str
    session_info: SessionInfo
    last_activity: datetime
    options: AgentCatOptions

    # Dynamic tracking fields (initialized on demand)
    tool_registry: Dict[str, ToolRegistration] = field(default_factory=dict)
    wrapped_tools: Set[str] = field(default_factory=set)
    tracker_initialized: bool = False
    monkey_patched: bool = False
    is_stateless: bool = False
