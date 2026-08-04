"""Event queue implementation for AgentCat.

Process-safety contract: importing this module must be side-effect free
beyond object construction — no threads, no signal handlers, no atexit
hooks. Workers start lazily on first publish, are always daemon, and are
never drained at interpreter exit: telemetry may be lost on shutdown, the
customer's process may never be delayed or redirected by it.
"""

import queue
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .telemetry import TelemetryManager

from agentcat_api import ApiClient, Configuration, EventsApi
from agentcat.modules.constants import AGENTCAT_API_URL, EVENT_ID_PREFIX

from ..types import Event, UnredactedEvent
from ..utils import generate_prefixed_ksuid, get_agentcat_version
from .internal import get_server_tracking_data
from .logging import write_to_log
from .redaction import redact_event
from .sanitization import sanitize_event
from .truncation import truncate_event

# Stamped on every event. Same value 1.x reported, resolved once at import
# instead of rebuilt per event inside a per-server session cache.
SDK_LANGUAGE = f"Python {sys.version_info.major}.{sys.version_info.minor}"

# Bound on a single publish HTTP attempt so a black-holed connection can
# wedge a worker for at most this long. Lives here, not constants.py — that
# file is byte-parity with the TS SDK and this knob is Python-only.
PUBLISH_TIMEOUT_SECONDS = 10


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

        # Workers start lazily on first add() so constructing the queue —
        # which happens at module import — spawns no threads and is safe
        # from any thread, main or not.
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._workers_started = False
        self._active = 0  # workers currently inside _process_event

    def _ensure_workers(self) -> None:
        """Start the consumer threads on first use."""
        if self._workers_started:
            return
        with self._lock:
            if self._workers_started or self._shutdown:
                return
            for i in range(self.concurrency):
                t = threading.Thread(
                    target=self._worker,
                    daemon=True,
                    name=f"agentcat-event-worker-{i}",
                )
                t.start()
                self._workers.append(t)
            self._workers_started = True

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

        self._ensure_workers()
        try:
            # Try to add without blocking
            self.queue.put_nowait(event)
        except queue.Full:
            # Queue is full, drop the new event
            write_to_log(
                f"Event queue full, dropping event {event.id or 'unknown'} of type {event.event_type}"
            )

    def _worker(self) -> None:
        """Consumer thread: pulls events straight off the bounded queue.

        No intermediate executor — when every worker is busy the bounded
        queue fills and add() drops, so memory is genuinely capped at
        max_queue_size events.
        """
        while not self._shutdown_event.is_set():
            try:
                event = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            with self._lock:
                self._active += 1
            try:
                self._process_event(event)
            except Exception as e:
                write_to_log(f"Worker thread error (continuing): {e}")
            finally:
                with self._lock:
                    self._active -= 1

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
            except (Exception, SystemExit) as error:
                # SystemExit included: a customer hook calling sys.exit() must
                # not kill the worker thread.
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
            # Synchronous API call, bounded so a black-holed connection
            # cannot wedge this worker indefinitely
            self.api_client.publish_event(
                publish_event_request=event,
                _request_timeout=PUBLISH_TIMEOUT_SECONDS,
            )
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
                f"Failed to send event {event.id}, retrying... [Error: {error}]"
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
        with self._lock:
            active = self._active
        return {
            "queueLength": self.queue.qsize(),
            "activeRequests": active,
            "isProcessing": active > 0,
        }

    def destroy(self) -> None:
        """Stop workers and stop accepting events. Bounded; for tests and
        explicit shutdown — nothing registers it at exit anymore. A worker
        wedged in a customer hook outlives the join as a daemon and dies
        with the process.
        """
        self._shutdown = True
        self._shutdown_event.set()

        # Shared 1s budget across all joins, never per-thread.
        deadline = time.monotonic() + 1.0
        for t in self._workers:
            if t is threading.current_thread():
                continue  # destroy() called from a worker
            t.join(timeout=max(0.0, deadline - time.monotonic()))

        remaining = self.queue.qsize()
        if remaining > 0:
            write_to_log(f"Event queue destroyed with {remaining} events unprocessed.")

        # Flush any buffered SDK diagnostics on the way out. Lazy import to avoid
        # an import cycle; never let diagnostics break shutdown.
        try:
            from agentcat.modules.diagnostics import flush_diagnostics

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


# Global event queue instance. Constructing it starts no threads (workers
# are lazy), so this module is importable from any thread. The SDK installs
# no signal handlers and no exit hooks for events: the customer's process
# owns its own shutdown, and undelivered telemetry is dropped by design.
event_queue = EventQueue()


def set_event_queue(new_queue: EventQueue) -> None:
    """Replace the global event queue instance (for testing)."""
    global event_queue
    # Destroy the old queue first
    event_queue.destroy()
    event_queue = new_queue


def publish_event(server: Any, event: UnredactedEvent) -> None:
    """Publish an event to the queue.

    Everything about the CALL is already on the event: the call path resolved
    the actor, the client identity (design §7) and the handle tags per request
    and stamped them there. This adds only what is per-SERVER or per-INSTALL —
    project, server identity captured at track time, SDK language, SDK version
    — and never merges anything over a field the event already carries. v1
    merged a per-server metadata cache on top of the event, which silently
    overwrote the ladder's client name/version on any server that kept one.
    """
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

    stamped = {
        **event.model_dump(exclude_none=True),
        "project_id": data.project_id,
        "sdk_language": SDK_LANGUAGE,
        "agentcat_version": get_agentcat_version(),
        "server_name": data.server_name,
        "server_version": data.server_version,
    }

    full_event = UnredactedEvent(
        **stamped,
        redaction_fn=data.options.redact_sensitive_information,
    )

    event_queue.add(full_event)
