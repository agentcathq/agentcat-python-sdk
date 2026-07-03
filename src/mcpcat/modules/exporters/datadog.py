"""Datadog exporter for AgentCat telemetry."""

import json
import re
from datetime import datetime
from typing import Dict, List, Any
import requests

from ...types import Event, DatadogExporterConfig
from ...modules.constants import AGENTCAT_SOURCE
from ...modules.logging import write_to_log
from . import Exporter
from .trace_context import trace_context


_DD_TAG_KEY_SANITIZE = re.compile(r"[\s:,]+")


def _sanitize_dd_tag_key(key: str) -> str:
    return _DD_TAG_KEY_SANITIZE.sub("_", key.lower())


def _sanitize_dd_tag_value(value: str) -> str:
    return value.replace(",", "_")


class DatadogExporter(Exporter):
    """Exports AgentCat events to Datadog logs and metrics."""

    def __init__(self, config: DatadogExporterConfig):
        """
        Initialize Datadog exporter.

        Args:
            config: Datadog exporter configuration
        """
        self.config = config
        self.api_key = config["api_key"]
        self.site = config["site"]
        self.service = config["service"]
        self.env = config.get("env", "production")

        # Build API endpoints based on site
        site_clean = (
            self.site.replace("https://", "").replace("http://", "").rstrip("/")
        )
        self.logs_url = f"https://http-intake.logs.{site_clean}/api/v2/logs"
        self.metrics_url = f"https://api.{site_clean}/api/v1/series"

        # Create session for connection pooling
        self.session = requests.Session()

        write_to_log(f"Datadog exporter initialized for service: {self.service}")

    def export(self, event: Event) -> None:
        """
        Export an event to Datadog logs and metrics.

        Args:
            event: AgentCat event to export
        """
        write_to_log("DatadogExporter: Sending event immediately to Datadog")

        # Convert event to log and metrics
        log = self.event_to_log(event)
        metrics = self.event_to_metrics(event)

        # Debug: Log the metrics payload (metadata only — count, not contents)
        write_to_log(f"DatadogExporter: Metrics URL: {self.metrics_url}")
        write_to_log(f"DatadogExporter: Sending {len(metrics)} metric series")

        # Send logs and metrics synchronously
        self._send_logs([log])
        self._send_metrics(metrics)

    def _send_logs(self, logs: List[Dict[str, Any]]) -> None:
        """Send logs to Datadog."""
        try:
            response = self.session.post(
                self.logs_url,
                headers={
                    "DD-API-KEY": self.api_key,
                    "Content-Type": "application/json",
                },
                json=logs,
                timeout=10,
            )

            if not response.ok:
                write_to_log(
                    f"Datadog logs failed - Status: {response.status_code}"
                )
            else:
                write_to_log(f"Datadog logs success - Status: {response.status_code}")
        except Exception as err:
            write_to_log(f"Datadog logs network error: {err}")

    def _send_metrics(self, metrics: List[Dict[str, Any]]) -> None:
        """Send metrics to Datadog."""
        try:
            response = self.session.post(
                self.metrics_url,
                headers={
                    "DD-API-KEY": self.api_key,
                    "Content-Type": "application/json",
                },
                json={"series": metrics},
                timeout=10,
            )

            if not response.ok:
                write_to_log(
                    f"Datadog metrics failed - Status: {response.status_code}"
                )
            else:
                write_to_log(
                    f"Datadog metrics success - Status: {response.status_code}"
                )
        except Exception as err:
            write_to_log(f"Datadog metrics network error: {err}")

    def event_to_log(self, event: Event) -> Dict[str, Any]:
        """
        Convert AgentCat event to Datadog log format.

        Args:
            event: AgentCat event

        Returns:
            Datadog log dictionary
        """
        tags: List[str] = []

        # Add basic tags
        if self.env:
            tags.append(f"env:{self.env}")
        if event.event_type:
            tags.append(f"event_type:{event.event_type.replace('/', '.')}")
        if event.resource_name:
            tags.append(f"resource:{event.resource_name}")
        if event.is_error:
            tags.append("error:true")

        tags.append(f"source:{AGENTCAT_SOURCE}")

        # Add customer-defined tags (namespaced to avoid collisions with reserved Datadog tags)
        if getattr(event, "tags", None):
            for key, value in event.tags.items():
                tags.append(
                    f"agentcat.{_sanitize_dd_tag_key(key)}:{_sanitize_dd_tag_value(value)}"
                )

        # Get timestamp in milliseconds
        timestamp_ms = (
            int(event.timestamp.timestamp() * 1000)
            if event.timestamp
            else int(datetime.now().timestamp() * 1000)
        )

        log: Dict[str, Any] = {
            "message": f"{event.event_type or 'unknown'} - {event.resource_name or 'unknown'}",
            "service": self.service,
            "ddsource": AGENTCAT_SOURCE,
            "ddtags": ",".join(tags),
            "timestamp": timestamp_ms,
            "status": "error" if event.is_error else "info",
            "dd": {
                "trace_id": trace_context.get_datadog_trace_id(event.session_id),
                "span_id": trace_context.get_datadog_span_id(event.id),
            },
            "mcp": {
                "session_id": event.session_id,
                "event_id": event.id,
                "event_type": event.event_type,
                "resource": event.resource_name,
                "duration_ms": event.duration,
                "user_intent": event.user_intent,
                "actor_id": event.identify_actor_given_id,
                "actor_name": event.identify_actor_name,
                "client_name": event.client_name,
                "client_version": event.client_version,
                "server_name": event.server_name,
                "server_version": event.server_version,
                "is_error": event.is_error,
                "error": event.error,
                "tags": getattr(event, "tags", None),
                "properties": getattr(event, "properties", None),
            },
        }

        # Add error at root level if it exists
        if event.is_error and event.error:
            log["error"] = {
                "message": (
                    event.error
                    if isinstance(event.error, str)
                    else json.dumps(event.error)
                )
            }

        return log

    def event_to_metrics(self, event: Event) -> List[Dict[str, Any]]:
        """
        Convert AgentCat event to Datadog metrics.

        Args:
            event: AgentCat event

        Returns:
            List of Datadog metric dictionaries
        """
        metrics: List[Dict[str, Any]] = []

        # Get timestamp in seconds (Unix timestamp)
        timestamp = (
            int(event.timestamp.timestamp())
            if event.timestamp
            else int(datetime.now().timestamp())
        )

        tags: List[str] = [f"service:{self.service}"]

        # Add optional tags
        if self.env:
            tags.append(f"env:{self.env}")
        if event.event_type:
            tags.append(f"event_type:{event.event_type.replace('/', '.')}")
        if event.resource_name:
            tags.append(f"resource:{event.resource_name}")

        # Event count metric
        metrics.append(
            {
                "metric": "mcp.events.count",
                "type": "count",
                "points": [[timestamp, 1]],
                "tags": tags,
            }
        )

        # Duration metric (only if duration exists)
        if event.duration is not None:
            metrics.append(
                {
                    "metric": "mcp.event.duration",
                    "type": "gauge",
                    "points": [[timestamp, event.duration]],
                    "tags": tags,
                }
            )

        # Error count metric
        if event.is_error:
            metrics.append(
                {
                    "metric": "mcp.errors.count",
                    "type": "count",
                    "points": [[timestamp, 1]],
                    "tags": tags,
                }
            )

        return metrics
