"""Event queue implementation for AgentCat."""

import atexit
import queue
import signal
import os
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .telemetry import TelemetryManager

from agentcat_api import ApiClient, Configuration, EventsApi
from mcpcat.modules.constants import AGENTCAT_API_URL, EVENT_ID_PREFIX

from ..types import Event, UnredactedEvent
from ..utils import generate_prefixed_ksuid
from .compatibility import get_mcp_compatible_error_message
from .internal import get_server_tracking_data
from .logging import write_to_log
from .redaction import redact_event
from .sanitization import sanitize_event
from .truncation import truncate_event
from .session import get_session_info, set_last_activity


class EventQueue:
    """Manages event queue and sending to AgentCat API."""

    def __init__(self, api_client=None):
        self.queue: queue.Queue[UnredactedEvent] = queue.Queue(maxsize=10000)
        self.max_retries = 3
        self.max_queue_size = 10000  # Prevent unbounded growth
        self.concurrency = 5  # Max parallel requests

        # Allow injection of api_client for testing
        if api_client is None:
            config = Configuration(host=AGENTCAT_API_URL)
            api_client_instance = ApiClient(configuration=config)
            self.api_client = EventsApi(api_client=api_client_instance)
        else:
            self.api_client = api_client

        self._shutdown = False
        self._shutdown_event = threading.Event()

        # Thread pool for processing events
        self.executor = ThreadPoolExecutor(max_workers=self.concurrency)

        # Start worker thread
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    def configure(self, api_base_url: str) -> None:
        """Reconfigure the API client with a new base URL."""
        config = Configuration(host=api_base_url)
        api_client_instance = ApiClient(configuration=config)
        self.api_client = EventsApi(api_client=api_client_instance)

    def add(self, event: UnredactedEvent) -> None:
        """Add event to queue."""
        if self._shutdown:
            write_to_log("Queue is shutting down, event dropped")
            return

        try:
            # Try to add without blocking
            self.queue.put_nowait(event)
        except queue.Full:
            # Queue is full, drop the new event
            write_to_log(
                f"Event queue full, dropping event {event.id or 'unknown'} of type {event.event_type}"
            )

    def _worker(self) -> None:
        """Worker thread that processes events from the queue."""
        while not self._shutdown_event.is_set():
            try:
                # Wait for an event with timeout
                event = self.queue.get(timeout=0.1)

                # Submit event processing to thread pool
                # The executor will queue it if all workers are busy
                try:
                    self.executor.submit(self._process_event, event)
                except Exception as e:
                    write_to_log(f"Failed to submit event for processing: {e}")
                    # Put the event back in the queue if possible
                    try:
                        self.queue.put_nowait(event)
                    except queue.Full:
                        write_to_log(
                            f"Could not requeue event {event.id or 'unknown'} - queue full"
                        )

            except queue.Empty:
                continue
            except Exception as e:
                write_to_log(f"Worker thread error (continuing): {e}")
                # Sleep briefly to avoid tight error loops
                time.sleep(0.1)

    def _process_event(self, event: UnredactedEvent) -> None:
        """Process a single event."""
        if event and event.redaction_fn:
            # Redact sensitive information if a redaction function is provided
            try:
                if not event.id:
                    event.id = generate_prefixed_ksuid(EVENT_ID_PREFIX)
                redacted_event = redact_event(event, event.redaction_fn)
                # The redacted event is already the full event object, not a dict
                event = redacted_event
                event.redaction_fn = None  # Clear the function to avoid reprocessing
            except Exception as error:
                write_to_log(
                    f"WARNING: Dropping event {event.id or 'unknown'} due to redaction failure: {error}"
                )
                return  # Skip this event if redaction fails

        try:
            event = sanitize_event(event)
        except Exception as error:
            write_to_log(
                f"WARNING: Sanitization failed for event {event.id or 'unknown'}, sending unsanitized: {error}"
            )
        try:
            event = truncate_event(event)
        except Exception as error:
            write_to_log(
                f"WARNING: Truncation failed for event {event.id or 'unknown'}, sending untruncated: {error}"
            )

        if event:
            event.id = event.id or generate_prefixed_ksuid("evt")

            # Send to AgentCat API only if project_id exists
            if event.project_id:
                self._send_event(event)

            # Export to telemetry backends if configured
            if _telemetry_manager:
                try:
                    _telemetry_manager.export(event)
                except Exception as e:
                    write_to_log(f"Telemetry export submission failed: {e}")

            if not event.project_id and not _telemetry_manager:
                # Warn if we have neither AgentCat nor telemetry configured
                write_to_log(
                    "Warning: Event has no project_id and no telemetry exporters configured"
                )

    def _send_event(self, event: Event, retries: int = 0) -> None:
        """Send event to API."""
        try:
            # Synchronous API call
            self.api_client.publish_event(publish_event_request=event)
            write_to_log(
                f"Successfully sent event {event.id} | {event.event_type} | "
                f"session {event.session_id} | {event.project_id} | "
                f"{event.duration} ms | {event.identify_actor_given_id or 'anonymous'}"
            )
        except Exception as error:
            if self._shutdown_event.is_set():
                write_to_log(
                    f"Dropping event {event.id} retry due to shutdown"
                )
                return
            write_to_log(
                f"Failed to send event {event.id}, retrying... [Error: {get_mcp_compatible_error_message(error)}]"
            )
            if retries < self.max_retries:
                # Exponential backoff: 1s, 2s, 4s
                # Use shutdown_event.wait so backoff is interruptible
                if self._shutdown_event.wait(timeout=2**retries):
                    write_to_log(
                        f"Dropping event {event.id} retry due to shutdown"
                    )
                    return
                self._send_event(event, retries + 1)
            else:
                write_to_log(
                    f"Failed to send event {event.id} after {self.max_retries} retries"
                )

    def get_stats(self) -> dict[str, Any]:
        """Get queue stats for monitoring."""
        return {
            "queueLength": self.queue.qsize(),
            "activeRequests": self.executor._threads.__len__(),  # Number of active threads
            "isProcessing": self.executor._threads.__len__() > 0,
        }

    def destroy(self) -> None:
        """Graceful shutdown - wait for active requests."""
        # Stop accepting new events
        self._shutdown = True
        self._shutdown_event.set()

        # Determine wait time based on queue state
        if self.queue.qsize() > 0:
            # If there are events in queue, wait 5 seconds
            wait_time = 5.0
            write_to_log(
                f"Shutting down with {self.queue.qsize()} events in queue, waiting up to {wait_time}s"
            )
        else:
            # If queue is empty, just wait 1 second for in-flight requests
            wait_time = 1.0
            write_to_log(f"Queue empty, waiting {wait_time}s for in-flight requests")

        # Wait for the specified time
        time.sleep(wait_time)

        # Shutdown executor, cancelling any queued (not yet running) tasks
        self.executor.shutdown(wait=True, cancel_futures=True)

        # Log final status
        remaining = self.queue.qsize()
        if remaining > 0:
            write_to_log(f"Shutdown complete. {remaining} events were not processed.")

        # Flush any buffered SDK diagnostics on the way out. Lazy import to avoid
        # an import cycle; never let diagnostics break shutdown.
        try:
            from mcpcat.modules.diagnostics import flush_diagnostics

            flush_diagnostics()
        except Exception:
            pass


# Global telemetry manager instance (optional)
_telemetry_manager: Optional["TelemetryManager"] = None


def set_telemetry_manager(manager: Optional["TelemetryManager"]) -> None:
    """
    Set the global telemetry manager instance.

    Args:
        manager: TelemetryManager instance or None to disable telemetry
    """
    global _telemetry_manager
    _telemetry_manager = manager
    if manager:
        write_to_log(
            f"Telemetry manager set with {manager.get_exporter_count()} exporter(s)"
        )


# Global event queue instance
event_queue = EventQueue()


def _shutdown_handler(signum, frame):
    """Handle shutdown signals."""

    write_to_log("Received shutdown signal, gracefully shutting down...")

    # Reset signal handlers to default behavior to avoid recursive calls
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    # Perform graceful shutdown
    event_queue.destroy()

    # Force exit after graceful shutdown
    os._exit(0)


def set_event_queue(new_queue: EventQueue) -> None:
    """Replace the global event queue instance (for testing)."""
    global event_queue
    # Destroy the old queue first
    event_queue.destroy()
    event_queue = new_queue


# Register shutdown handlers
signal.signal(signal.SIGINT, _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)
atexit.register(lambda: event_queue.destroy())


def publish_event(server: Any, event: UnredactedEvent) -> None:
    """Publish an event to the queue."""
    if not event.duration:
        if event.timestamp:
            event.duration = int(
                (datetime.now(timezone.utc).timestamp() - event.timestamp.timestamp())
                * 1000
            )
        else:
            event.duration = None

    data = get_server_tracking_data(server)
    if not data:
        write_to_log(
            "Warning: Server tracking data not found. Event will not be published."
        )
        return

    session_info = get_session_info(server, data)

    # Create full event with all required fields
    # Merge event data with session info
    event_data = event.model_dump(exclude_none=True)
    session_data = session_info.model_dump(exclude_none=True)

    # Merge data, ensuring project_id from data takes precedence
    merged_data = {**event_data, **session_data}
    merged_data["project_id"] = (
        data.project_id
    )  # Override with tracking data's project_id

    full_event = UnredactedEvent(
        **merged_data,
        redaction_fn=data.options.redact_sensitive_information,
    )

    set_last_activity(server)
    event_queue.add(full_event)
