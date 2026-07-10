"""PostHog exporter for AgentCat telemetry."""

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from ...modules.constants import AGENTCAT_SOURCE
from ...modules.logging import write_to_log
from ...thirdparty.ksuid import Ksuid
from ...types import Event, EventType, PostHogExporterConfig
from . import Exporter

DEFAULT_POSTHOG_HOST = "https://us.i.posthog.com"

_PREFIX_RE = re.compile(r"^[a-z]+_")

# Map AgentCat event types to PostHog event names
_EVENT_NAME_MAPPING: dict[str, str] = {
    EventType.MCP_TOOLS_CALL.value: "mcp_tool_call",
    EventType.MCP_TOOLS_LIST.value: "mcp_tools_list",
    EventType.MCP_INITIALIZE.value: "mcp_initialize",
    EventType.MCP_RESOURCES_READ.value: "mcp_resource_read",
    EventType.MCP_RESOURCES_LIST.value: "mcp_resources_list",
    EventType.MCP_PROMPTS_GET.value: "mcp_prompt_get",
    EventType.MCP_PROMPTS_LIST.value: "mcp_prompts_list",
}


def to_uuidv7(prefixed_id: str) -> str:
    """Generate a deterministic UUIDv7 from a prefixed KSUID (e.g. ses_xxx).

    Uses the KSUID's embedded timestamp for the UUIDv7 timestamp portion and
    a SHA-256 hash of the full ID for the random bits (mirrors the TypeScript
    SDK's toUUIDv7 for cross-SDK determinism).
    """
    prefixed_id = prefixed_id or ""
    # Strip prefix (ses_, evt_, etc.) and parse KSUID
    ksuid_str = _PREFIX_RE.sub("", prefixed_id)
    try:
        ksuid = Ksuid.from_base62(ksuid_str)
        timestamp_ms = int(ksuid.timestamp * 1000)
    except Exception:
        # Fallback: if KSUID parsing fails, use current time
        timestamp_ms = int(time.time() * 1000)

    # Hash the full ID for deterministic random bits
    digest = hashlib.sha256(prefixed_id.encode("utf-8")).digest()

    buf = bytearray(16)

    # Bytes 0-5: 48-bit Unix timestamp in milliseconds
    buf[0:6] = timestamp_ms.to_bytes(6, "big")

    # Byte 6: version 7 (0111) + high 4 bits of rand_a from hash
    buf[6] = 0x70 | (digest[0] & 0x0F)
    # Byte 7: low 8 bits of rand_a from hash
    buf[7] = digest[1]

    # Byte 8: variant 10 + high 6 bits of rand_b from hash
    buf[8] = 0x80 | (digest[2] & 0x3F)
    # Bytes 9-15: remaining rand_b from hash
    buf[9:16] = digest[3:10]

    hex_str = buf.hex()
    return "-".join(
        [
            hex_str[0:8],
            hex_str[8:12],
            hex_str[12:16],
            hex_str[16:20],
            hex_str[20:32],
        ]
    )


def _get_distinct_id(event: Event) -> str:
    return event.identify_actor_given_id or event.session_id or "anonymous"


def _get_timestamp(event: Event) -> str:
    timestamp = event.timestamp or datetime.now(timezone.utc)
    return timestamp.isoformat()


class PostHogExporter(Exporter):
    """Exports AgentCat events to PostHog via the /batch capture API."""

    def __init__(self, config: PostHogExporterConfig):
        """
        Initialize PostHog exporter.

        Args:
            config: PostHog exporter configuration
        """
        self.config = config
        host = (config.get("host") or DEFAULT_POSTHOG_HOST).rstrip("/")
        self.batch_url = f"{host}/batch"
        self.api_key = config["api_key"]
        self.enable_ai_tracing = config.get("enable_ai_tracing", False)

        # Create session for connection pooling
        self.session = requests.Session()

        write_to_log(f"PostHogExporter: Initialized with endpoint {self.batch_url}")

    def export(self, event: Event) -> None:
        """
        Export an event to PostHog.

        Args:
            event: AgentCat event to export
        """
        try:
            batch: list[dict[str, Any]] = []

            # Compute the deterministic UUIDs once per event (KSUID parse +
            # SHA-256 each time) and share them across the builders.
            session_uuid = to_uuidv7(event.session_id)

            # Always send the regular event
            batch.append(self.build_capture_event(event, session_uuid))

            # Send $exception event alongside if this is an error
            if event.is_error and event.error:
                batch.append(self.build_exception_event(event, session_uuid))

            # Send $ai_span for tool calls when AI tracing is enabled
            if (
                self.enable_ai_tracing
                and event.event_type == EventType.MCP_TOOLS_CALL.value
            ):
                batch.append(
                    self.build_ai_span_event(event, session_uuid, to_uuidv7(event.id))
                )

            write_to_log(
                f"PostHogExporter: Sending {len(batch)} event(s) for {event.id}"
            )

            response = self.session.post(
                self.batch_url,
                headers={"Content-Type": "application/json"},
                json={"api_key": self.api_key, "batch": batch},
                timeout=10,
            )

            if not response.ok:
                write_to_log(
                    f"PostHog export failed - Status: {response.status_code}, "
                    f"Body: {response.text}"
                )
            else:
                write_to_log(f"PostHog export success - Event: {event.id}")
        except Exception as error:
            write_to_log(f"PostHog export error: {error}")

    def build_capture_event(self, event: Event, session_uuid: str) -> dict[str, Any]:
        """Build the regular PostHog capture event."""
        properties: dict[str, Any] = {
            "$session_id": session_uuid,
            "source": AGENTCAT_SOURCE,
        }

        if event.resource_name:
            properties["resource_name"] = event.resource_name
            if event.event_type == EventType.MCP_TOOLS_CALL.value:
                properties["tool_name"] = event.resource_name
        if event.duration is not None:
            properties["duration_ms"] = event.duration
        if event.server_name:
            properties["server_name"] = event.server_name
        if event.server_version:
            properties["server_version"] = event.server_version
        if event.client_name:
            properties["client_name"] = event.client_name
        if event.client_version:
            properties["client_version"] = event.client_version
        if event.project_id:
            properties["project_id"] = event.project_id
        if event.user_intent:
            properties["user_intent"] = event.user_intent
        if event.is_error is not None:
            properties["is_error"] = event.is_error

        if event.parameters is not None:
            properties["parameters"] = event.parameters
        if event.response is not None:
            properties["response"] = event.response

        # Set person properties from identity data
        person_props: dict[str, Any] = {}
        if event.identify_actor_name:
            person_props["name"] = event.identify_actor_name
        if event.identify_data:
            person_props.update(event.identify_data)
        if person_props:
            properties["$set"] = person_props

        # Spread customer-defined tags directly (can override AgentCat defaults)
        if event.tags:
            properties.update(event.tags)

        # Spread customer-defined properties directly (can override AgentCat defaults)
        if event.properties:
            properties.update(event.properties)

        return {
            "event": self.map_event_type(event.event_type),
            "distinct_id": _get_distinct_id(event),
            "properties": properties,
            "timestamp": _get_timestamp(event),
            "type": "capture",
        }

    def build_exception_event(self, event: Event, session_uuid: str) -> dict[str, Any]:
        """Build a PostHog $exception event for error events."""
        properties: dict[str, Any] = {
            "$exception_source": "backend",
            "$session_id": session_uuid,
        }

        error = event.error or {}
        if isinstance(error, dict):
            if error.get("message"):
                properties["$exception_message"] = error["message"]
            if error.get("type"):
                properties["$exception_type"] = error["type"]
            if error.get("stack"):
                properties["$exception_stacktrace"] = error["stack"]

        # Add tool/resource context
        if event.resource_name:
            properties["resource_name"] = event.resource_name
            if event.event_type == EventType.MCP_TOOLS_CALL.value:
                properties["tool_name"] = event.resource_name
        if event.server_name:
            properties["server_name"] = event.server_name
        if event.server_version:
            properties["server_version"] = event.server_version
        if event.client_name:
            properties["client_name"] = event.client_name
        if event.client_version:
            properties["client_version"] = event.client_version

        return {
            "event": "$exception",
            "distinct_id": _get_distinct_id(event),
            "properties": properties,
            "timestamp": _get_timestamp(event),
            "type": "capture",
        }

    def build_ai_span_event(
        self, event: Event, session_uuid: str, span_uuid: str
    ) -> dict[str, Any]:
        """Build a PostHog $ai_span event for AI observability views."""
        properties: dict[str, Any] = {
            "$ai_session_id": f"agentcat_{event.session_id}",
            "$ai_trace_id": session_uuid,
            "$ai_span_id": span_uuid,
            "$ai_span_name": event.resource_name or "unknown_tool",
            "$ai_is_error": event.is_error or False,
            "$session_id": session_uuid,
            "source": AGENTCAT_SOURCE,
        }

        if event.duration is not None:
            properties["$ai_latency"] = event.duration / 1000
        if event.is_error and event.error:
            properties["$ai_error"] = event.error
        if event.parameters is not None:
            properties["$ai_input_state"] = event.parameters
        if event.response is not None:
            properties["$ai_output_state"] = event.response
        if event.server_name:
            properties["server_name"] = event.server_name
        if event.client_name:
            properties["client_name"] = event.client_name

        # Spread customer tags directly (can override AgentCat defaults)
        if event.tags:
            properties.update(event.tags)

        # Spread customer properties directly (can override AgentCat defaults)
        if event.properties:
            properties.update(event.properties)

        return {
            "event": "$ai_span",
            "distinct_id": _get_distinct_id(event),
            "properties": properties,
            "timestamp": _get_timestamp(event),
            "type": "capture",
        }

    def map_event_type(self, event_type: str | None) -> str:
        """Map AgentCat event types to PostHog event names."""
        event_type = event_type or "unknown"
        mapped = _EVENT_NAME_MAPPING.get(event_type)
        if mapped:
            return mapped
        stripped = event_type.removeprefix("mcp:").replace("/", "_")
        return f"mcp_{stripped}"
