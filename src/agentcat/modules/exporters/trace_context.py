"""
Shared trace context management for all exporters.
Maintains one trace ID per session for proper observability tool correlation.
"""

import hashlib
import secrets
from typing import Optional


class TraceContext:
    """Manages trace and span ID generation for all exporters."""

    def get_trace_id(self, session_id: Optional[str] = None) -> str:
        """
        Get or create a trace ID for a session.
        Returns the same trace ID for all events in a session.

        Args:
            session_id: Optional session identifier

        Returns:
            32-character hex trace ID
        """
        if not session_id:
            # No session, return random trace ID
            return secrets.token_hex(16)

        # Hash session ID to get deterministic trace ID
        return hashlib.sha256(session_id.encode()).hexdigest()[:32]

    def get_span_id(self, event_id: Optional[str] = None) -> str:
        """
        Generate a span ID from event ID.
        Returns deterministic span ID based on event ID.

        Args:
            event_id: Optional event identifier

        Returns:
            16-character hex span ID
        """
        if not event_id:
            # No event ID, return random span ID
            return secrets.token_hex(8)

        # Hash event ID to get deterministic span ID
        return hashlib.sha256(event_id.encode()).hexdigest()[:16]

    def get_datadog_trace_id(self, session_id: Optional[str] = None) -> str:
        """
        Get Datadog-compatible numeric trace ID.

        Args:
            session_id: Optional session identifier

        Returns:
            Numeric string trace ID for Datadog
        """
        hex_id = self.get_trace_id(session_id)
        # Take last 16 chars (64 bits) and convert to decimal
        return str(int(hex_id[16:32], 16))

    def get_datadog_span_id(self, event_id: Optional[str] = None) -> str:
        """
        Get Datadog-compatible numeric span ID.

        Args:
            event_id: Optional event identifier

        Returns:
            Numeric string span ID for Datadog
        """
        hex_id = self.get_span_id(event_id)
        # Convert full 16 chars to decimal
        return str(int(hex_id, 16))


# Export singleton instance
trace_context = TraceContext()
