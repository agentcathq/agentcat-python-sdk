"""Sentry exporter for AgentCat telemetry."""

import json
import re
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
import requests

from ...types import Event, SentryExporterConfig
from ...modules.constants import AGENTCAT_SOURCE
from ...modules.logging import write_to_log
from ...modules.session import get_agentcat_version
from . import Exporter
from .trace_context import trace_context


class SentryExporter(Exporter):
    """Exports AgentCat events to Sentry as logs, transactions, and error events."""

    def __init__(self, config: SentryExporterConfig):
        """
        Initialize Sentry exporter.

        Args:
            config: Sentry exporter configuration
        """
        self.config = config
        self.dsn = config["dsn"]
        self.environment = config.get("environment", "production")
        self.release = config.get("release")
        self.enable_tracing = config.get("enable_tracing", False)

        # Parse DSN
        self.parsed_dsn = self.parse_dsn(self.dsn)

        # Build envelope endpoint
        protocol = self.parsed_dsn["protocol"]
        host = self.parsed_dsn["host"]
        port = f":{self.parsed_dsn['port']}" if self.parsed_dsn.get("port") else ""
        path = self.parsed_dsn.get("path", "")
        project_id = self.parsed_dsn["project_id"]

        self.endpoint = f"{protocol}://{host}{port}{path}/api/{project_id}/envelope/"

        # Build auth header
        sdk_version = get_agentcat_version() or "unknown"
        self.auth_header = f"Sentry sentry_version=7, sentry_client=agentcat/{sdk_version}, sentry_key={self.parsed_dsn['public_key']}"

        # Create session for connection pooling
        self.session = requests.Session()

        write_to_log(f"SentryExporter: Initialized with endpoint {self.endpoint}")

    def parse_dsn(self, dsn: str) -> Dict[str, str]:
        """
        Parse Sentry DSN to extract components.

        Args:
            dsn: Sentry DSN string

        Returns:
            Dictionary with DSN components

        Raises:
            ValueError: If DSN is invalid
        """
        # DSN format: protocol://publicKey@host[:port]/path/projectId
        regex = r"^(https?):\/\/([a-f0-9]+)@([\w.-]+)(:\d+)?(\/.*)?\/(\d+)$"
        match = re.match(regex, dsn)

        if not match:
            raise ValueError(f"Invalid Sentry DSN: {dsn}")

        return {
            "protocol": match.group(1),
            "public_key": match.group(2),
            "host": match.group(3),
            "port": match.group(4)[1:]
            if match.group(4)
            else None,  # Remove leading ':'
            "path": match.group(5) or "",
            "project_id": match.group(6),
        }

    def export(self, event: Event) -> None:
        """
        Export an event to Sentry.

        Args:
            event: AgentCat event to export
        """
        try:
            # ALWAYS send log
            log = self.event_to_log(event)
            log_envelope = self.create_log_envelope(log)

            write_to_log(f"SentryExporter: Sending log for event {event.id} to Sentry")

            log_response = self.session.post(
                self.endpoint,
                headers={
                    "X-Sentry-Auth": self.auth_header,
                    "Content-Type": "application/x-sentry-envelope",
                },
                data=log_envelope,
                timeout=10,
            )

            if not log_response.ok:
                write_to_log(
                    f"Sentry log export failed - Status: {log_response.status_code}"
                )
            else:
                write_to_log(f"Sentry log export success - Event: {event.id}")

            # OPTIONALLY send transaction for performance monitoring
            if self.enable_tracing:
                transaction = self.event_to_transaction(event)
                transaction_envelope = self.create_transaction_envelope(transaction)

                write_to_log(
                    f"SentryExporter: Sending transaction {transaction['event_id']} to Sentry"
                )

                transaction_response = self.session.post(
                    self.endpoint,
                    headers={
                        "X-Sentry-Auth": self.auth_header,
                        "Content-Type": "application/x-sentry-envelope",
                    },
                    data=transaction_envelope,
                    timeout=10,
                )

                if not transaction_response.ok:
                    write_to_log(
                        f"Sentry transaction export failed - Status: {transaction_response.status_code}"
                    )
                else:
                    write_to_log(
                        f"Sentry transaction export success - Event: {event.id}"
                    )

            # ALWAYS send error event for Issue creation if this is an error
            if event.is_error:
                # Use transaction if available for better context
                transaction = (
                    self.event_to_transaction(event) if self.enable_tracing else None
                )
                error_event = self.event_to_error_event(event, transaction)
                error_envelope = self.create_error_envelope(error_event)

                write_to_log(
                    f"SentryExporter: Sending error event {error_event['event_id']} to Sentry for Issue creation"
                )

                error_response = self.session.post(
                    self.endpoint,
                    headers={
                        "X-Sentry-Auth": self.auth_header,
                        "Content-Type": "application/x-sentry-envelope",
                    },
                    data=error_envelope,
                    timeout=10,
                )

                if not error_response.ok:
                    write_to_log(
                        f"Sentry error export failed - Status: {error_response.status_code}"
                    )
                else:
                    write_to_log(f"Sentry error export success - Event: {event.id}")

        except Exception as error:
            write_to_log(f"Sentry export error: {error}")

    def event_to_log(self, event: Event) -> Dict[str, Any]:
        """
        Convert AgentCat event to Sentry log format.

        Args:
            event: AgentCat event

        Returns:
            Sentry log dictionary
        """
        timestamp = (
            event.timestamp.timestamp()
            if event.timestamp
            else datetime.now().timestamp()
        )
        trace_id = trace_context.get_trace_id(event.session_id)

        # Generate deterministic event_id for Sentry
        event_id = trace_context.get_span_id(event.id) + trace_context.get_span_id(
            event.id
        )

        # Build message
        message = (
            f"MCP {event.event_type or 'event'}: {event.resource_name}"
            if event.resource_name
            else f"MCP {event.event_type or 'event'}"
        )

        return {
            "timestamp": timestamp,
            "trace_id": trace_id,
            "event_id": event_id,
            "level": "error" if event.is_error else "info",
            "body": message,
            "attributes": self.build_log_attributes(event),
        }

    def build_log_attributes(self, event: Event) -> Dict[str, Dict[str, Any]]:
        """
        Build log attributes for Sentry.

        Args:
            event: AgentCat event

        Returns:
            Attributes dictionary
        """
        attributes: Dict[str, Dict[str, Any]] = {}

        if event.event_type:
            attributes["eventType"] = {"value": event.event_type, "type": "string"}
        if event.resource_name:
            attributes["resourceName"] = {
                "value": event.resource_name,
                "type": "string",
            }
        if event.server_name:
            attributes["serverName"] = {"value": event.server_name, "type": "string"}
        if event.client_name:
            attributes["clientName"] = {"value": event.client_name, "type": "string"}
        if event.session_id:
            attributes["sessionId"] = {"value": event.session_id, "type": "string"}
        if event.project_id:
            attributes["projectId"] = {"value": event.project_id, "type": "string"}
        if event.duration is not None:
            attributes["duration_ms"] = {"value": event.duration, "type": "double"}
        if event.identify_actor_given_id:
            attributes["actorId"] = {
                "value": event.identify_actor_given_id,
                "type": "string",
            }
        if event.identify_actor_name:
            attributes["actorName"] = {
                "value": event.identify_actor_name,
                "type": "string",
            }
        if event.user_intent:
            attributes["userIntent"] = {"value": event.user_intent, "type": "string"}
        if event.server_version:
            attributes["serverVersion"] = {
                "value": event.server_version,
                "type": "string",
            }
        if event.client_version:
            attributes["clientVersion"] = {
                "value": event.client_version,
                "type": "string",
            }
        if event.is_error is not None:
            attributes["isError"] = {"value": event.is_error, "type": "boolean"}

        return attributes

    def create_log_envelope(self, log: Dict[str, Any]) -> str:
        """
        Create Sentry log envelope.

        Args:
            log: Log dictionary

        Returns:
            Envelope string
        """
        # Envelope header
        envelope_header = {
            "event_id": log["event_id"],
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }

        # Item header with ALL MANDATORY fields
        item_header = {
            "type": "log",
            "item_count": 1,  # MANDATORY - must match number of logs
            "content_type": "application/vnd.sentry.items.log+json",  # MANDATORY - exact string
        }

        # Payload with CORRECT key
        payload = {
            "items": [log]  # Changed from 'logs' to 'items'
        }

        # Build envelope with TRAILING NEWLINE
        return (
            "\n".join(
                [
                    json.dumps(envelope_header),
                    json.dumps(item_header),
                    json.dumps(payload),
                ]
            )
            + "\n"
        )  # Added required trailing newline

    def event_to_transaction(self, event: Event) -> Dict[str, Any]:
        """
        Convert AgentCat event to Sentry transaction.

        Args:
            event: AgentCat event

        Returns:
            Sentry transaction dictionary
        """
        # Calculate timestamps
        end_timestamp = (
            event.timestamp.timestamp()
            if event.timestamp
            else datetime.now().timestamp()
        )
        start_timestamp = (
            end_timestamp - (event.duration / 1000) if event.duration else end_timestamp
        )

        trace_id = trace_context.get_trace_id(event.session_id)
        span_id = trace_context.get_span_id(event.id)

        # Build transaction name
        transaction_name = (
            f"{event.event_type or 'mcp'} - {event.resource_name}"
            if event.resource_name
            else (event.event_type or "mcp.event")
        )

        return {
            "type": "transaction",
            "event_id": trace_context.get_span_id(event.id)
            + trace_context.get_span_id(),
            "timestamp": end_timestamp,
            "start_timestamp": start_timestamp,
            "transaction": transaction_name,
            "contexts": self.build_contexts(
                event,
                {
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "op": event.event_type or "mcp.event",
                    "status": "internal_error" if event.is_error else "ok",
                },
            ),
            "tags": self.build_tags(event),
            "extra": self.build_extra(event),
        }

    def build_tags(self, event: Event) -> Dict[str, str]:
        """
        Build tags for Sentry transaction/error.

        Args:
            event: AgentCat event

        Returns:
            Tags dictionary
        """
        tags: Dict[str, str] = {
            "source": AGENTCAT_SOURCE,
        }

        if self.environment:
            tags["environment"] = self.environment
        if self.release:
            tags["release"] = self.release
        if event.event_type:
            tags["event_type"] = event.event_type
        if event.resource_name:
            tags["resource"] = event.resource_name
        if event.server_name:
            tags["server_name"] = event.server_name
        if event.client_name:
            tags["client_name"] = event.client_name
        if event.identify_actor_given_id:
            tags["actor_id"] = event.identify_actor_given_id

        # Namespace customer tags to avoid collisions with Sentry reserved fields
        if getattr(event, "tags", None):
            for key, value in event.tags.items():
                tags[f"agentcat.{key}"] = value

        return tags

    def build_contexts(
        self, event: Event, trace_ctx: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build Sentry contexts dict, embedding customer properties under `agentcat`."""
        contexts: Dict[str, Any] = {"trace": trace_ctx}
        if getattr(event, "properties", None):
            contexts["agentcat"] = event.properties
        return contexts

    def build_extra(self, event: Event) -> Dict[str, Any]:
        """
        Build extra data for Sentry transaction/error.

        Args:
            event: AgentCat event

        Returns:
            Extra data dictionary
        """
        extra: Dict[str, Any] = {}

        if event.session_id:
            extra["session_id"] = event.session_id
        if event.project_id:
            extra["project_id"] = event.project_id
        if event.user_intent:
            extra["user_intent"] = event.user_intent
        if event.identify_actor_name:
            extra["actor_name"] = event.identify_actor_name
        if event.server_version:
            extra["server_version"] = event.server_version
        if event.client_version:
            extra["client_version"] = event.client_version
        if event.duration is not None:
            extra["duration_ms"] = event.duration
        if event.error:
            extra["error"] = event.error

        return extra

    def event_to_error_event(
        self, event: Event, transaction: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Convert AgentCat event to Sentry error event.

        Args:
            event: AgentCat event
            transaction: Optional transaction for context

        Returns:
            Sentry error event dictionary
        """
        # Extract error message
        error_message = "Unknown error"
        error_type = "ToolCallError"

        if event.error:
            if isinstance(event.error, str):
                error_message = event.error
            elif isinstance(event.error, dict):
                if "message" in event.error:
                    error_message = str(event.error["message"])
                elif "error" in event.error:
                    error_message = str(event.error["error"])
                else:
                    error_message = json.dumps(event.error)
                if "type" in event.error:
                    error_type = str(event.error["type"])

        # Use same trace context as the transaction for correlation (if available)
        trace_id = (
            transaction["contexts"]["trace"]["trace_id"]
            if transaction
            else trace_context.get_trace_id(event.session_id)
        )
        span_id = trace_context.get_span_id(event.id)

        timestamp = (
            transaction["timestamp"]
            if transaction
            else (
                event.timestamp.timestamp()
                if event.timestamp
                else datetime.now().timestamp()
            )
        )

        return {
            "type": "event",
            "event_id": trace_context.get_span_id(event.id)
            + trace_context.get_span_id(),
            "timestamp": timestamp,
            "level": "error",
            "exception": {
                "values": [
                    {
                        "type": error_type,
                        "value": error_message,
                        "mechanism": {"type": "mcp_tool_call", "handled": False},
                    }
                ]
            },
            "contexts": {
                **self.build_contexts(
                    event,
                    {
                        "trace_id": trace_id,  # Same trace ID as transaction/log for correlation
                        "span_id": span_id,
                        "parent_span_id": transaction["contexts"]["trace"]["span_id"]
                        if transaction
                        else None,  # Link to transaction span if available
                        "op": transaction["contexts"]["trace"]["op"]
                        if transaction
                        else (event.event_type or "mcp.event"),
                    },
                ),
                "mcp": {
                    "resource_name": event.resource_name,
                    "session_id": event.session_id,
                    "event_type": event.event_type,
                    "user_intent": event.user_intent,
                },
            },
            "tags": self.build_tags(event),
            "extra": self.build_extra(event),
            "transaction": transaction["transaction"]
            if transaction
            else (
                f"{event.event_type or 'mcp'} - {event.resource_name}"
                if event.resource_name
                else (event.event_type or "mcp.event")
            ),
        }

    def create_transaction_envelope(self, transaction: Dict[str, Any]) -> str:
        """
        Create Sentry transaction envelope.

        Args:
            transaction: Transaction dictionary

        Returns:
            Envelope string
        """
        # Envelope header
        envelope_header = {
            "event_id": transaction["event_id"],
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }

        # Item header for transaction
        item_header = {"type": "transaction"}

        # Build envelope (newline-separated JSON)
        return "\n".join(
            [
                json.dumps(envelope_header),
                json.dumps(item_header),
                json.dumps(transaction),
            ]
        )

    def create_error_envelope(self, error_event: Dict[str, Any]) -> str:
        """
        Create Sentry error event envelope.

        Args:
            error_event: Error event dictionary

        Returns:
            Envelope string
        """
        # Envelope header
        envelope_header = {
            "event_id": error_event["event_id"],
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }

        # Item header for error event
        item_header = {"type": "event", "content_type": "application/json"}

        # Build envelope (newline-separated JSON)
        return "\n".join(
            [
                json.dumps(envelope_header),
                json.dumps(item_header),
                json.dumps(error_event),
            ]
        )
