"""Test event queue functionality."""

import queue
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

from agentcat.modules.event_queue import EventQueue, publish_event
from agentcat.modules.logging import write_to_log
from agentcat.types import Event, AgentCatData, AgentCatOptions, UnredactedEvent


class TestEventQueue:
    """Test EventQueue class."""

    def test_init(self):
        """Test EventQueue initialization: construction starts no threads."""
        eq = EventQueue()

        assert isinstance(eq.queue, queue.Queue)
        assert eq.queue.maxsize > 0
        assert eq.max_retries > 0
        assert eq.max_queue_size > 0
        assert eq.concurrency > 0
        assert eq._shutdown is False
        assert isinstance(eq._shutdown_event, threading.Event)
        # Workers are lazy: nothing runs until the first add(), so importing
        # the module (which constructs the global queue) is thread-safe.
        assert eq._workers == []
        assert eq._workers_started is False

    def test_workers_start_lazily_and_are_daemon(self):
        """First add() starts exactly `concurrency` daemon workers."""
        eq = EventQueue()
        eq._process_event = lambda event: None  # never hit the network

        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )
        eq.add(event)

        assert eq._workers_started is True
        assert len(eq._workers) == eq.concurrency
        assert all(t.daemon for t in eq._workers)

        eq.add(event)
        assert len(eq._workers) == eq.concurrency  # no growth on later adds

        eq.destroy()

    def test_add_event_success(self):
        """Test adding event to queue successfully."""
        eq = EventQueue()
        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        eq.add(event)

        assert eq.queue.qsize() == 1
        assert eq.queue.get_nowait() == event

    def test_add_event_when_shutdown(self):
        """Test adding event when queue is shutting down."""
        eq = EventQueue()
        eq._shutdown = True
        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        with patch("agentcat.modules.event_queue.write_to_log") as mock_log:
            eq.add(event)
            assert mock_log.called
            assert any(
                "shutting down" in str(call).lower() for call in mock_log.call_args_list
            )

        assert eq.queue.qsize() == 0

    def test_add_event_queue_full(self):
        """Test adding event when queue is full."""
        eq = EventQueue()
        eq.queue = queue.Queue(maxsize=2)  # Small queue for testing
        eq._workers_started = True  # keep workers off so the queue stays full

        # Fill the queue
        event1 = UnredactedEvent(
            id="1",
            event_type="mcp:tools/call",
            project_id="p1",
            session_id="s1",
            timestamp=datetime.now(timezone.utc),
        )
        event2 = UnredactedEvent(
            id="2",
            event_type="mcp:tools/call",
            project_id="p1",
            session_id="s1",
            timestamp=datetime.now(timezone.utc),
        )
        event3 = UnredactedEvent(
            id="3",
            event_type="mcp:tools/call",
            project_id="p1",
            session_id="s1",
            timestamp=datetime.now(timezone.utc),
        )

        eq.queue.put_nowait(event1)
        eq.queue.put_nowait(event2)

        with patch("agentcat.modules.event_queue.write_to_log") as mock_log:
            eq.add(event3)
            assert mock_log.called
            assert any(
                "full" in str(call).lower() and "dropping" in str(call).lower()
                for call in mock_log.call_args_list
            )

        # Check that new event was dropped and old events remain
        assert eq.queue.qsize() == 2
        assert eq.queue.get_nowait() == event1
        assert eq.queue.get_nowait() == event2

    def test_add_event_queue_full_drops_new_event(self):
        """Test that new events are dropped when queue is full."""
        eq = EventQueue()
        eq.queue = queue.Queue(maxsize=1)
        eq._workers_started = True  # keep workers off so the queue stays full

        # Fill the queue
        event1 = UnredactedEvent(
            id="event-1",
            event_type="mcp:tools/call",
            project_id="p1",
            session_id="s1",
            timestamp=datetime.now(timezone.utc),
        )
        event2 = UnredactedEvent(
            id="event-2",
            event_type="mcp:tools/call",
            project_id="p1",
            session_id="s1",
            timestamp=datetime.now(timezone.utc),
        )

        eq.queue.put_nowait(event1)

        with patch("agentcat.modules.event_queue.write_to_log") as mock_log:
            eq.add(event2)

        # Event2 should not have been added
        assert eq.queue.qsize() == 1
        assert eq.queue.get_nowait() == event1

        # Check that drop was logged with event details
        assert mock_log.called
        log_message = str(mock_log.call_args_list[0])
        assert "event-2" in log_message
        assert "mcp:tools/call" in log_message

    @patch("agentcat.modules.event_queue.redact_event")
    def test_process_event_with_redaction(self, mock_redact):
        """Test processing event with redaction function."""
        eq = EventQueue()
        mock_redaction_fn = MagicMock()

        # Create a redacted event
        redacted_event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            parameters={"secret": "[REDACTED]"},
            user_intent="redacted intent",
            redaction_fn=None,  # This should be cleared after redaction
        )

        # Mock redact_event_sync to return the redacted event
        mock_redact.return_value = redacted_event

        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            parameters={"secret": "password123"},
            user_intent="original intent",
            redaction_fn=mock_redaction_fn,
        )

        with patch.object(eq, "_send_event") as mock_send:
            eq._process_event(event)

            mock_redact.assert_called_once_with(event, mock_redaction_fn)
            # The send_event should be called with the redacted event
            called_event = mock_send.call_args[0][0]
            assert called_event.parameters == {"secret": "[REDACTED]"}
            assert called_event.user_intent == "redacted intent"
            assert called_event.redaction_fn is None
            mock_send.assert_called_once()

    def test_process_event_redacts_for_real(self):
        """The same path with nothing mocked.

        `test_process_event_with_redaction` above patches `redact_event`, so it
        proves the queue calls it and nothing about what it does — which is how
        a `redact_event` that returned its pydantic input untouched survived the
        whole v2 branch with the README advertising it as a security control.
        """
        eq = EventQueue()

        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            parameters={"arguments": {"token": "hunter2"}},
            user_intent="spend hunter2",
            redaction_fn=lambda s: s.replace("hunter2", "[REDACTED]"),
        )

        with patch.object(eq, "_send_event") as mock_send:
            eq._process_event(event)

        sent = mock_send.call_args[0][0]
        assert sent.parameters == {"arguments": {"token": "[REDACTED]"}}
        assert sent.user_intent == "spend [REDACTED]"
        assert sent.redaction_fn is None
        # Protected: the handle still identifies the task on the dashboard.
        assert sent.session_id == "session-123"

    @patch("agentcat.modules.event_queue.redact_event")
    @patch("agentcat.modules.event_queue.write_to_log")
    def test_process_event_redaction_failure(self, mock_log, mock_redact):
        """Test processing event when redaction fails."""
        eq = EventQueue()
        mock_redaction_fn = MagicMock()
        mock_redact.side_effect = Exception("Redaction error")

        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            redaction_fn=mock_redaction_fn,
        )

        with patch.object(eq, "_send_event") as mock_send:
            eq._process_event(event)

            assert mock_log.called
            # Check for WARNING and redaction failure message
            log_message = str(mock_log.call_args_list[0])
            assert "WARNING" in log_message
            assert "redaction failure" in log_message
            assert "test-id" in log_message
            mock_send.assert_not_called()

    @patch("agentcat.modules.event_queue.apply_event_redaction")
    def test_process_event_with_event_redaction(self, mock_apply):
        """Test processing event with the event-level redaction hook."""
        eq = EventQueue()
        mock_event_redaction_fn = MagicMock()

        redacted_event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            resource_name="dropped-and-replaced",
            event_redaction_fn=None,  # This should be cleared after redaction
        )
        mock_apply.return_value = redacted_event

        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            resource_name="original",
            event_redaction_fn=mock_event_redaction_fn,
        )

        with patch.object(eq, "_send_event") as mock_send:
            eq._process_event(event)

            mock_apply.assert_called_once_with(event, mock_event_redaction_fn)
            called_event = mock_send.call_args[0][0]
            assert called_event.resource_name == "dropped-and-replaced"
            assert called_event.event_redaction_fn is None
            mock_send.assert_called_once()

    def test_process_event_event_redaction_drops_event(self):
        """A hook returning None drops the event before it's ever sent."""
        eq = EventQueue()

        def drop_it(event):
            return None

        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            event_redaction_fn=drop_it,
        )

        with patch.object(eq, "_send_event") as mock_send:
            eq._process_event(event)
            mock_send.assert_not_called()

    @patch("agentcat.modules.event_queue.apply_event_redaction")
    @patch("agentcat.modules.event_queue.write_to_log")
    def test_process_event_event_redaction_failure(self, mock_log, mock_apply):
        """Test processing event when the event-level hook raises."""
        eq = EventQueue()
        mock_event_redaction_fn = MagicMock()
        mock_apply.side_effect = Exception("Event redaction error")

        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            event_redaction_fn=mock_event_redaction_fn,
        )

        with patch.object(eq, "_send_event") as mock_send:
            eq._process_event(event)

            assert mock_log.called
            log_message = str(mock_log.call_args_list[0])
            assert "WARNING" in log_message
            assert "event redaction failure" in log_message
            assert "test-id" in log_message
            mock_send.assert_not_called()

    def test_process_event_event_hook_runs_before_string_hook(self):
        """Ordering: the event hook must see raw values, before the string
        hook ever runs — nothing mocked, both hooks wired for real."""
        eq = EventQueue()
        order = []

        def event_hook(event):
            order.append(("event_hook", event.parameters))
            return event

        def string_hook(s):
            order.append(("string_hook", s))
            return "[REDACTED]"

        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            parameters={"secret": "raw-value"},
            event_redaction_fn=event_hook,
            redaction_fn=string_hook,
        )

        with patch.object(eq, "_send_event"):
            eq._process_event(event)

        assert order[0] == ("event_hook", {"secret": "raw-value"})
        assert order[1] == ("string_hook", "raw-value")

    @patch("agentcat.modules.event_queue.generate_prefixed_ksuid")
    def test_process_event_without_id(self, mock_ksuid):
        """Test processing event without ID generates one."""
        eq = EventQueue()
        generated_id = "evt_generated_id"
        mock_ksuid.return_value = generated_id

        event = UnredactedEvent(
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        with patch.object(eq, "_send_event") as mock_send:
            eq._process_event(event)

            mock_ksuid.assert_called_once()
            # sanitize_event creates a deep copy, so check the sent event
            sent_event = mock_send.call_args[0][0]
            assert sent_event.id == generated_id
            mock_send.assert_called_once()

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_send_event_success(self, mock_log):
        """Test sending event successfully."""
        eq = EventQueue()
        mock_api_client = MagicMock()
        eq.api_client = mock_api_client

        event = Event(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            duration=100,
            identify_actor_given_id="user-123",
        )

        eq._send_event(event)

        mock_api_client.publish_event.assert_called_once_with(
            publish_event_request=event,
            _request_timeout=10,
        )
        assert mock_log.call_count >= 1  # At least one success log

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_send_event_success_logs_session_not_payload(self, mock_log):
        """Success log carries session metadata, never a serialized payload."""
        eq = EventQueue()
        eq.api_client = MagicMock()

        event = Event(
            id="evt-1",
            event_type="mcp:tools/call",
            project_id="proj-1",
            session_id="ses-secret-123",
            timestamp=datetime.now(timezone.utc),
            duration=42,
            identify_actor_given_id="actor-1",
        )

        eq._send_event(event)

        logged = "\n".join(str(c.args[0]) for c in mock_log.call_args_list)
        assert "session ses-secret-123" in logged
        # The full-payload dump was removed for privacy.
        assert "Event details" not in logged
        assert "model_dump_json" not in logged

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_send_event_with_retries(self, mock_log):
        """Test sending event with retries on failure."""
        eq = EventQueue()
        mock_api_client = MagicMock()
        mock_api_client.publish_event.side_effect = [
            Exception("Network error"),
            Exception("Network error"),
            None,  # Success on third try
        ]
        eq.api_client = mock_api_client

        # Make shutdown_event.wait return immediately (simulates no shutdown)
        eq._shutdown_event = MagicMock()
        eq._shutdown_event.is_set.return_value = False
        eq._shutdown_event.wait.return_value = False  # Not shutting down

        event = Event(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        eq._send_event(event)

        assert mock_api_client.publish_event.call_count == 3
        assert eq._shutdown_event.wait.call_count == 2
        # Verify exponential backoff timeouts
        wait_calls = [call[1]["timeout"] for call in eq._shutdown_event.wait.call_args_list]
        assert wait_calls[0] < wait_calls[1]  # Exponential backoff

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_send_event_max_retries_exceeded(self, mock_log):
        """Test sending event when max retries exceeded."""
        eq = EventQueue()
        mock_api_client = MagicMock()
        mock_api_client.publish_event.side_effect = Exception("Persistent error")
        eq.api_client = mock_api_client

        # Make shutdown_event.wait return immediately (simulates no shutdown)
        eq._shutdown_event = MagicMock()
        eq._shutdown_event.is_set.return_value = False
        eq._shutdown_event.wait.return_value = False  # Not shutting down

        event = Event(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        eq._send_event(event)

        # Initial attempt + retries
        assert mock_api_client.publish_event.call_count == eq.max_retries + 1
        assert eq._shutdown_event.wait.call_count == eq.max_retries
        # Check that failure was logged
        assert any("retries" in str(call).lower() for call in mock_log.call_args_list)

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_send_event_aborts_retry_on_shutdown(self, mock_log):
        """Test that retry is aborted when shutdown is signaled during backoff wait."""
        eq = EventQueue()
        mock_api_client = MagicMock()
        mock_api_client.publish_event.side_effect = Exception("Network error")
        eq.api_client = mock_api_client

        # Mock _shutdown_event: not set at exception entry, but wait returns True (shutdown during backoff)
        eq._shutdown_event = MagicMock()
        eq._shutdown_event.is_set.return_value = False
        eq._shutdown_event.wait.return_value = True  # Shutdown signaled during wait

        event = Event(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        eq._send_event(event)

        # Only the initial attempt, no retry after wait returned True
        assert mock_api_client.publish_event.call_count == 1
        # wait was called once (for the first retry backoff) then aborted
        assert eq._shutdown_event.wait.call_count == 1
        # Log should mention shutdown
        assert any("shutdown" in str(call).lower() for call in mock_log.call_args_list)

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_send_event_early_return_on_shutdown_detected(self, mock_log):
        """Test that no retry is attempted when shutdown is already set at exception handler entry."""
        eq = EventQueue()
        mock_api_client = MagicMock()
        mock_api_client.publish_event.side_effect = Exception("Network error")
        eq.api_client = mock_api_client

        # Mock _shutdown_event: is_set returns True immediately at exception handler entry
        eq._shutdown_event = MagicMock()
        eq._shutdown_event.is_set.return_value = True

        event = Event(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        eq._send_event(event)

        # Only the initial attempt, early return before any retry logic
        assert mock_api_client.publish_event.call_count == 1
        # wait should never be called since is_set() returned True first
        eq._shutdown_event.wait.assert_not_called()
        # Log should mention shutdown
        assert any("shutdown" in str(call).lower() for call in mock_log.call_args_list)

    def test_get_stats(self):
        """Test getting queue statistics."""
        eq = EventQueue()

        # Add some events
        for i in range(3):
            event = UnredactedEvent(
                id=f"test-{i}",
                event_type="mcp:tools/call",
                project_id="project-123",
                session_id="session-123",
                timestamp=datetime.now(timezone.utc),
            )
            eq.queue.put_nowait(event)

        stats = eq.get_stats()

        assert "queueLength" in stats
        assert stats["queueLength"] == 3
        assert "activeRequests" in stats
        assert isinstance(stats["activeRequests"], int)
        assert "isProcessing" in stats
        assert isinstance(stats["isProcessing"], bool)

    def test_destroy(self):
        """destroy() sets flags, stops workers, and returns fast — no sleeps,
        no unbounded joins."""
        eq = EventQueue()
        eq._process_event = lambda event: None
        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )
        eq.add(event)

        start = time.monotonic()
        eq.destroy()
        elapsed = time.monotonic() - start

        assert eq._shutdown is True
        assert eq._shutdown_event.is_set()
        assert elapsed < 1.5  # bounded by the shared 1s join budget

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_destroy_logs_unprocessed_events(self, mock_log):
        """Events still queued when destroy() runs are counted in the log."""
        eq = EventQueue()

        num_events = 5
        for i in range(num_events):
            event = UnredactedEvent(
                id=f"test-{i}",
                event_type="mcp:tools/call",
                project_id="project-123",
                session_id="session-123",
                timestamp=datetime.now(timezone.utc),
            )
            eq.queue.put_nowait(event)  # no workers started: events sit there

        eq.destroy()

        assert mock_log.called
        assert any(str(num_events) in str(call) for call in mock_log.call_args_list)

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_destroy_wakeup_markers_are_not_counted_as_events(self, mock_log):
        """The _Stop markers destroy() enqueues to wake idle parked workers
        must never show up in the unprocessed-events count."""
        eq = EventQueue()
        eq._process_event = lambda event: None
        eq.add(
            UnredactedEvent(
                id="drains",
                event_type="mcp:tools/call",
                project_id="project-123",
                session_id="session-123",
                timestamp=datetime.now(timezone.utc),
            )
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if eq.queue.qsize() == 0 and eq.get_stats()["activeRequests"] == 0:
                break
            time.sleep(0.01)

        eq.destroy()

        assert not any(
            "unprocessed" in str(logged) for logged in mock_log.call_args_list
        )

    def test_worker_thread_processes_events(self):
        """Test that worker thread processes events from queue."""
        eq = EventQueue()

        # Mock the process_event method to track calls
        process_event_calls = []
        original_process = eq._process_event

        def mock_process(event):
            process_event_calls.append(event)
            # Don't actually process to avoid external calls

        eq._process_event = mock_process

        # Add an event
        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )
        eq.add(event)

        # Give worker thread time to process
        time.sleep(0.2)

        # Verify event was picked up by worker
        assert eq.queue.qsize() == 0

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_worker_thread_exception_handling(self, mock_log):
        """A raising _process_event is logged and the workers stay alive."""
        eq = EventQueue()
        eq._process_event = MagicMock(side_effect=Exception("Test exception"))

        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )
        eq.add(event)

        # Give a worker time to pick it up and handle the exception
        time.sleep(0.3)

        assert mock_log.called
        assert any(
            "Worker thread error (continuing)" in str(call)
            for call in mock_log.call_args_list
        )
        assert all(t.is_alive() for t in eq._workers)

        eq.destroy()


def _tracking_data(**overrides) -> AgentCatData:
    """Track-time data in its v2 shape: project, options, server identity."""
    fields = {
        "project_id": "project-123",
        "options": AgentCatOptions(),
        "server_name": "test-server",
        "server_version": "1.0.0",
    }
    fields.update(overrides)
    return AgentCatData(**fields)


class TestPublishEvent:
    """Test publish_event function."""

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_success(self, mock_eq, mock_tracking):
        """Test publishing event successfully."""
        mock_server = MagicMock()
        mock_data = _tracking_data(
            options=AgentCatOptions(redact_sensitive_information=None)
        )
        mock_tracking.return_value = mock_data

        # Create event
        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="ses_task_handle",
            timestamp=datetime.now(timezone.utc),
        )

        publish_event(mock_server, event)

        mock_tracking.assert_called_once_with(mock_server)

        mock_eq.add.assert_called_once()
        added_event = mock_eq.add.call_args[0][0]
        assert added_event.project_id == mock_data.project_id
        assert isinstance(added_event, UnredactedEvent)
        assert added_event.event_type == "mcp:tools/call"
        assert added_event.session_id == "ses_task_handle"

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_stamps_server_and_sdk_metadata(self, mock_eq, mock_tracking):
        """Server identity comes from the track-time capture on AgentCatData and
        the SDK identity from package metadata — there is no session cache to
        read them from anymore, but every event still carries all four."""
        import importlib.metadata

        mock_tracking.return_value = _tracking_data(
            server_name="todo-server", server_version="4.2.0"
        )

        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="ses_task_handle",
            timestamp=datetime.now(timezone.utc),
        )
        publish_event(MagicMock(), event)

        added_event = mock_eq.add.call_args[0][0]
        assert added_event.server_name == "todo-server"
        assert added_event.server_version == "4.2.0"
        assert added_event.sdk_language.startswith("Python ")
        # Compared against the distribution directly, not against the helper the
        # pipeline calls, so this cannot pass by agreeing with itself.
        assert added_event.agentcat_version == importlib.metadata.version("agentcat")

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_keeps_the_events_own_client_identity(
        self, mock_eq, mock_tracking
    ):
        """The per-request client identity ladder (design §7) already stamped
        this event. Publishing must not overwrite it — the v1 pipeline merged a
        server-wide session cache OVER the event and defeated the ladder on
        every non-stateless server."""
        mock_tracking.return_value = _tracking_data()

        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="ses_task_handle",
            timestamp=datetime.now(timezone.utc),
            client_name="Cursor",
            client_version="2.6.22",
        )
        publish_event(MagicMock(), event)

        added_event = mock_eq.add.call_args[0][0]
        assert added_event.client_name == "Cursor"
        assert added_event.client_version == "2.6.22"

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_keeps_the_events_own_actor_and_tags(
        self, mock_eq, mock_tracking
    ):
        """Same rule for everything else the call path resolved per request:
        the actor `identify` returned and the tags the handle layer merged."""
        mock_tracking.return_value = _tracking_data()

        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="ses_task_handle",
            timestamp=datetime.now(timezone.utc),
            identify_actor_given_id="user-123",
            identify_actor_name="Ada",
            identify_data={"plan": "pro"},
            tags={"agentcat_session_id_source": "supplied"},
        )
        publish_event(MagicMock(), event)

        added_event = mock_eq.add.call_args[0][0]
        assert added_event.identify_actor_given_id == "user-123"
        assert added_event.identify_actor_name == "Ada"
        assert added_event.identify_data == {"plan": "pro"}
        assert added_event.tags == {"agentcat_session_id_source": "supplied"}

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.write_to_log")
    def test_publish_event_no_tracking_data(self, mock_log, mock_tracking):
        """Test publishing event when no tracking data available."""
        mock_server = MagicMock()
        mock_tracking.return_value = None

        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="ses_task_handle",
            timestamp=datetime.now(timezone.utc),
        )

        publish_event(mock_server, event)

        assert mock_log.called
        assert any(
            "tracking data" in str(call).lower() for call in mock_log.call_args_list
        )

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_calculates_duration(self, mock_eq, mock_tracking):
        """Test publishing event calculates duration if not provided."""
        mock_server = MagicMock()
        mock_tracking.return_value = _tracking_data()

        # Create event without duration
        event_timestamp = datetime.now(timezone.utc)
        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="ses_task_handle",
            timestamp=event_timestamp,
        )

        # Mock current time to be 1 second later
        with patch("agentcat.modules.event_queue.datetime") as mock_datetime:
            mock_datetime.now.return_value.timestamp.return_value = (
                event_timestamp.timestamp() + 1
            )

            publish_event(mock_server, event)

            # Check duration was calculated
            assert event.duration is not None
            assert event.duration > 0

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_no_duration_no_timestamp(self, mock_eq, mock_tracking):
        """Test publishing event with no duration and no timestamp sets duration to None."""
        mock_server = MagicMock()
        mock_tracking.return_value = _tracking_data()

        # Create event without duration or timestamp
        event = UnredactedEvent(
            event_type="mcp:tools/call", session_id="ses_task_handle"
        )

        publish_event(mock_server, event)

        # Check duration is None
        assert event.duration is None

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_with_redaction_function(self, mock_eq, mock_tracking):
        """Test publishing event includes redaction function from options."""
        mock_server = MagicMock()
        mock_redaction_fn = MagicMock()
        mock_tracking.return_value = _tracking_data(
            options=AgentCatOptions(redact_sensitive_information=mock_redaction_fn)
        )

        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="ses_task_handle",
            timestamp=datetime.now(timezone.utc),
        )

        publish_event(mock_server, event)

        # Check event was added with redaction function
        added_event = mock_eq.add.call_args[0][0]
        assert added_event.redaction_fn == mock_redaction_fn

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_with_event_redaction_function(self, mock_eq, mock_tracking):
        """Test publishing event includes the event-level redaction hook."""
        mock_server = MagicMock()
        mock_event_redaction_fn = MagicMock()
        mock_tracking.return_value = _tracking_data(
            options=AgentCatOptions(redact_event=mock_event_redaction_fn)
        )

        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="ses_task_handle",
            timestamp=datetime.now(timezone.utc),
        )

        publish_event(mock_server, event)

        added_event = mock_eq.add.call_args[0][0]
        assert added_event.event_redaction_fn == mock_event_redaction_fn


def test_module_import_installs_no_process_hooks():
    """The SDK must never register signal handlers — the customer's process
    owns its own shutdown. The one exit hook this module owns registers on
    first publish, never at import. (Full subprocess-based checks live in
    test_process_safety.py; this pins the module surface.)"""
    import agentcat.modules.event_queue as eq_module

    assert not hasattr(eq_module, "_shutdown_handler")
    source = open(eq_module.__file__).read()
    assert "import signal" not in source
    assert "signal.signal" not in source
    assert "os._exit" not in source


def test_bounded_queue_is_the_only_buffer():
    """Audit finding 9: with every worker wedged, memory is capped by the
    queue's maxsize — there is no second, unbounded buffer behind it (the old
    dispatcher drained the bounded queue into ThreadPoolExecutor's unbounded
    SimpleQueue, so the advertised cap protected nothing)."""
    eq = EventQueue()
    eq.queue = queue.Queue(maxsize=5)
    wedge = threading.Event()
    eq._process_event = lambda event: wedge.wait(10)

    def make(i: int) -> UnredactedEvent:
        return UnredactedEvent(
            id=f"e{i}",
            event_type="mcp:tools/call",
            project_id="p",
            session_id="s",
            timestamp=datetime.now(timezone.utc),
        )

    try:
        with patch("agentcat.modules.event_queue.write_to_log") as mock_log:
            for i in range(30):
                eq.add(make(i))
            deadline = time.monotonic() + 2.0
            while (
                eq.get_stats()["activeRequests"] < eq.concurrency
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)

        # concurrency in flight + maxsize buffered is the whole footprint;
        # every other event was dropped at add() with a log line.
        assert eq.queue.qsize() <= 5
        assert len(eq._workers) == eq.concurrency
        assert all(t.daemon for t in eq._workers)

        stats = eq.get_stats()
        assert stats["activeRequests"] == eq.concurrency
        assert stats["isProcessing"] is True
        assert any(
            "full" in str(c).lower() for c in mock_log.call_args_list
        ), "no drop was logged"
    finally:
        wedge.set()
        eq.destroy()
