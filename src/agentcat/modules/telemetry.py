"""Telemetry manager for exporting events to observability platforms."""

from typing import Dict, Optional
from ..types import (
    Event,
    ExporterConfig,
    OTLPExporterConfig,
    DatadogExporterConfig,
    SentryExporterConfig,
)
from .exporters import Exporter
from .logging import write_to_log


class TelemetryManager:
    """Manages telemetry exporters and coordinates event export."""

    def __init__(self, exporter_configs: dict[str, ExporterConfig]):
        """
        Initialize the telemetry manager with configured exporters.

        Args:
            exporter_configs: Dictionary of exporter configurations
        """
        self.exporters: Dict[str, Exporter] = {}
        self._initialize_exporters(exporter_configs)

    def _initialize_exporters(self, configs: dict[str, ExporterConfig]) -> None:
        """Initialize all configured exporters."""
        for name, config in configs.items():
            try:
                exporter = self._create_exporter(name, config)
                if exporter:
                    self.exporters[name] = exporter
                    write_to_log(
                        f"Initialized telemetry exporter: {name} (type: {config['type']})"
                    )
            except Exception as e:
                write_to_log(f"Failed to initialize exporter {name}: {e}")

    def _create_exporter(self, name: str, config: ExporterConfig) -> Optional[Exporter]:
        """
        Factory method to create appropriate exporter based on type.

        Args:
            name: Name of the exporter
            config: Exporter configuration

        Returns:
            Exporter instance or None if type is unknown
        """
        exporter_type = config.get("type")

        if exporter_type == "otlp":
            from .exporters.otlp import OTLPExporter

            return OTLPExporter(config)
        elif exporter_type == "datadog":
            from .exporters.datadog import DatadogExporter

            return DatadogExporter(config)
        elif exporter_type == "sentry":
            from .exporters.sentry import SentryExporter

            return SentryExporter(config)
        else:
            write_to_log(f"Unknown exporter type: {exporter_type}")
            return None

    def export(self, event: Event) -> None:
        """
        Export event to all configured exporters.

        Args:
            event: Event to export
        """
        if not self.exporters:
            return

        # Export to each exporter synchronously
        for name, exporter in self.exporters.items():
            self._safe_export(name, exporter, event)

    def _safe_export(self, name: str, exporter: Exporter, event: Event) -> None:
        """
        Safely export an event, catching and logging any errors.

        Args:
            name: Name of the exporter
            exporter: Exporter instance
            event: Event to export
        """
        try:
            exporter.export(event)
            write_to_log(f"Successfully exported event {event.id} to {name}")
        except Exception as e:
            # Log error but don't propagate - telemetry should never crash the main app
            write_to_log(f"Telemetry export failed for {name}: {e}")

    def get_exporter_count(self) -> int:
        """Get the number of active exporters."""
        return len(self.exporters)

    def destroy(self) -> None:
        """Clean up any resources used by exporters."""
        # Currently no cleanup needed, but this is here for future use
        # (e.g., if we add connection pooling or background threads)
        pass
