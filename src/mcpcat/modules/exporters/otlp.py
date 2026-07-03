"""OpenTelemetry Protocol (OTLP) exporter for MCPCat telemetry."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from ...types import Event, OTLPExporterConfig
from ...modules.constants import AGENTCAT_SOURCE
from ...modules.logging import write_to_log
from . import Exporter
from .trace_context import trace_context


class OTLPExporter(Exporter):
    """Exports MCPCat events to OpenTelemetry collectors via OTLP."""

    def __init__(self, config: OTLPExporterConfig):
        """
        Initialize OTLP exporter.

        Args:
            config: OTLP exporter configuration
        """
        self.protocol = config.get("protocol", "http/protobuf")

        # Set default endpoint based on protocol
        if "endpoint" in config:
            self.endpoint = config["endpoint"]
        else:
            if self.protocol == "grpc":
                self.endpoint = "http://localhost:4317"
            else:
                self.endpoint = "http://localhost:4318/v1/traces"

        # Set up headers
        self.headers = {
            "Content-Type": "application/json",  # Using JSON for easier debugging
        }
        if "headers" in config:
            self.headers.update(config["headers"])

        # Create session for connection pooling
        self.session = requests.Session()
        self.session.headers.update(self.headers)

        write_to_log(f"OTLP exporter initialized with endpoint: {self.endpoint}")

    def export(self, event: Event) -> None:
        """
        Export an event to OTLP collector.

        Args:
            event: MCPCat event to export
        """
        try:
            # Convert event to OTLP span format
            span = self._convert_to_otlp_span(event)

            # Create OTLP JSON format request
            otlp_request = {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": self._get_resource_attributes(event)
                        },
                        "scopeSpans": [
                            {
                                "scope": {
                                    "name": "agentcat",
                                    "version": event.agentcat_version or "0.1.0",
                                },
                                "spans": [span],
                            }
                        ],
                    }
                ]
            }

            # Send to OTLP collector
            response = self.session.post(self.endpoint, json=otlp_request, timeout=5)
            response.raise_for_status()

            write_to_log(f"Successfully exported event to OTLP: {event.id}")

        except requests.exceptions.RequestException as e:
            write_to_log(f"OTLP export failed: {e}")
        except Exception as e:
            write_to_log(f"OTLP export error: {e}")

    def _convert_to_otlp_span(self, event: Event) -> Dict[str, Any]:
        """
        Convert MCPCat event to OTLP span format.

        Args:
            event: MCPCat event

        Returns:
            OTLP span dictionary
        """
        # Convert timestamp to nanoseconds
        if event.timestamp:
            start_time_nanos = int(event.timestamp.timestamp() * 1_000_000_000)
        else:
            start_time_nanos = int(datetime.now().timestamp() * 1_000_000_000)

        # Calculate end time based on duration
        end_time_nanos = start_time_nanos
        if event.duration:
            end_time_nanos += event.duration * 1_000_000  # duration is in ms

        return {
            "traceId": trace_context.get_trace_id(event.session_id),
            "spanId": trace_context.get_span_id(event.id),
            "name": event.event_type or "mcp.event",
            "kind": 2,  # SPAN_KIND_SERVER
            "startTimeUnixNano": str(start_time_nanos),
            "endTimeUnixNano": str(end_time_nanos),
            "attributes": self._get_span_attributes(event),
            "status": {
                "code": 2 if getattr(event, "is_error", False) else 1  # ERROR : OK
            },
        }

    def _get_resource_attributes(self, event: Event) -> List[Dict[str, Any]]:
        """
        Get resource-level attributes for OTLP.

        Args:
            event: MCPCat event

        Returns:
            List of attribute key-value pairs
        """
        attributes = []

        if event.server_name:
            attributes.append(
                {"key": "service.name", "value": {"stringValue": event.server_name}}
            )

        if event.server_version:
            attributes.append(
                {
                    "key": "service.version",
                    "value": {"stringValue": event.server_version},
                }
            )

        # Add SDK information
        attributes.append(
            {"key": "telemetry.sdk.name", "value": {"stringValue": "agentcat-python"}}
        )

        if event.agentcat_version:
            attributes.append(
                {
                    "key": "telemetry.sdk.version",
                    "value": {"stringValue": event.agentcat_version},
                }
            )

        return attributes

    def _get_span_attributes(self, event: Event) -> List[Dict[str, Any]]:
        """
        Get span-level attributes for OTLP.

        Args:
            event: MCPCat event

        Returns:
            List of attribute key-value pairs
        """
        attributes: List[Dict[str, Any]] = [
            {"key": "source", "value": {"stringValue": AGENTCAT_SOURCE}},
        ]

        # Add MCP-specific attributes
        if event.event_type:
            attributes.append(
                {"key": "mcp.event_type", "value": {"stringValue": event.event_type}}
            )

        if event.session_id:
            attributes.append(
                {"key": "mcp.session_id", "value": {"stringValue": event.session_id}}
            )

        if event.project_id:
            attributes.append(
                {"key": "mcp.project_id", "value": {"stringValue": event.project_id}}
            )

        # Add resource name (for tools, prompts, resources)
        if event.resource_name:
            attributes.append(
                {
                    "key": "mcp.resource_name",
                    "value": {"stringValue": event.resource_name},
                }
            )

        # Add user intent if available
        if event.user_intent:
            attributes.append(
                {"key": "mcp.user_intent", "value": {"stringValue": event.user_intent}}
            )

        # Add actor information
        if event.identify_actor_given_id:
            attributes.append(
                {
                    "key": "mcp.actor_id",
                    "value": {"stringValue": event.identify_actor_given_id},
                }
            )

        if event.identify_actor_name:
            attributes.append(
                {
                    "key": "mcp.actor_name",
                    "value": {"stringValue": event.identify_actor_name},
                }
            )

        # Add client information
        if event.client_name:
            attributes.append(
                {"key": "mcp.client_name", "value": {"stringValue": event.client_name}}
            )

        if event.client_version:
            attributes.append(
                {
                    "key": "mcp.client_version",
                    "value": {"stringValue": event.client_version},
                }
            )

        # Add customer-defined tags as individual attributes
        for key, value in (getattr(event, "tags", None) or {}).items():
            attributes.append(
                {"key": f"agentcat.tag.{key}", "value": {"stringValue": value}}
            )

        # Add customer-defined properties as JSON
        if getattr(event, "properties", None):
            attributes.append(
                {
                    "key": "agentcat.properties",
                    "value": {"stringValue": json.dumps(event.properties)},
                }
            )

        # Filter out empty attributes
        return [attr for attr in attributes if attr["value"].get("stringValue")]
