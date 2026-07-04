"""Base exporter interface for telemetry exporters."""

from abc import ABC, abstractmethod
from ...types import Event


class Exporter(ABC):
    """Abstract base class for telemetry exporters."""

    @abstractmethod
    def export(self, event: Event) -> None:
        """
        Export an event to the telemetry backend.

        Args:
            event: The AgentCat event to export

        Note:
            This method should handle all errors internally and never
            raise exceptions that could affect the main MCP server.
        """
        pass
