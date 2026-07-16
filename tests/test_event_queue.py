"""Test event queue functionality."""

import queue
import signal
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

from agentcat.modules.event_queue import EventQueue, publish_event
from agentcat.modules.logging import write_to_log
from agentcat.types import Event, AgentCatData, AgentCatOptions, SessionInfo, UnredactedEvent


class TestEventQueue:
    """Test EventQueue class."""

    def test_init(self):
        """Test EventQueue initialization."""
        eq = EventQueue()

        assert isinstance(eq.queue, queue.Queue)
        assert eq.queue.maxsize > 0
        assert eq.max_retries > 0
        assert eq.max_queue_size > 0
        assert eq.concurrency > 0
        assert eq._shutdown is False
        assert isinstance(eq._shutdown_event, threading.Event)
        assert eq.worker_thread.is_alive()
        assert eq.worker_thread.daemon is True

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
            publish_event_request=event
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

    @patch("time.sleep")
    def test_destroy(self, mock_sleep):
        """Test graceful shutdown."""
        eq = EventQueue()

        # Add an event
        event = UnredactedEvent(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )
        eq.queue.put_nowait(event)

        # Mock executor
        mock_executor = MagicMock()
        eq.executor = mock_executor

        eq.destroy()

        assert eq._shutdown is True
        assert eq._shutdown_event.is_set()
        mock_executor.shutdown.assert_called_once()

    @patch("time.time")
    @patch("time.sleep")
    @patch("agentcat.modules.event_queue.write_to_log")
    def test_destroy_with_timeout(self, mock_log, mock_sleep, mock_time):
        """Test destroy with events still in queue after timeout."""
        eq = EventQueue()

        # Add events that won't be processed
        num_events = 5
        for i in range(num_events):
            event = UnredactedEvent(
                id=f"test-{i}",
                event_type="mcp:tools/call",
                project_id="project-123",
                session_id="session-123",
                timestamp=datetime.now(timezone.utc),
            )
            eq.queue.put_nowait(event)

        # Mock time to simulate timeout
        mock_time.side_effect = [0, 0.1, 0.2, 10.0]  # Exceeds timeout

        # Mock executor
        mock_executor = MagicMock()
        eq.executor = mock_executor

        eq.destroy()

        assert mock_log.called
        assert any(str(num_events) in str(call) for call in mock_log.call_args_list)

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
        """Test worker thread handles exceptions gracefully."""
        eq = EventQueue()

        # Mock executor.submit to raise an exception
        with patch.object(
            eq.executor, "submit", side_effect=Exception("Test exception")
        ):
            # Add an event
            event = UnredactedEvent(
                id="test-id",
                event_type="mcp:tools/call",
                project_id="project-123",
                session_id="session-123",
                timestamp=datetime.now(timezone.utc),
            )
            eq.add(event)

            # Give worker thread time to process and handle exception
            time.sleep(0.2)

            # Check that error was logged
            assert mock_log.called
            assert any(
                "Failed to submit event for processing" in str(call)
                for call in mock_log.call_args_list
            )

            # Worker thread should still be alive
            assert eq.worker_thread.is_alive()


class TestPublishEvent:
    """Test publish_event function."""

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.get_session_info")
    @patch("agentcat.modules.event_queue.set_last_activity")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_success(
        self, mock_eq, mock_set_activity, mock_session, mock_tracking
    ):
        """Test publishing event successfully."""
        # Mock server and data
        mock_server = MagicMock()
        mock_data = AgentCatData(
            project_id="project-123",
            session_id="session-123",
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=AgentCatOptions(redact_sensitive_information=None),
        )
        mock_tracking.return_value = mock_data

        mock_session_info = SessionInfo(
            server_name="test-server", server_version="1.0.0"
        )
        mock_session.return_value = mock_session_info

        # Create event
        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        publish_event(mock_server, event)

        mock_tracking.assert_called_once_with(mock_server)
        mock_session.assert_called_once_with(mock_server, mock_data)
        mock_set_activity.assert_called_once_with(mock_server)

        # Check event was added with merged data
        mock_eq.add.assert_called_once()
        added_event = mock_eq.add.call_args[0][0]
        assert added_event.project_id == mock_data.project_id
        # Just verify the event has the expected type and required fields
        assert isinstance(added_event, UnredactedEvent)
        assert added_event.event_type == "mcp:tools/call"
        assert added_event.session_id is not None

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.write_to_log")
    def test_publish_event_no_tracking_data(self, mock_log, mock_tracking):
        """Test publishing event when no tracking data available."""
        mock_server = MagicMock()
        mock_tracking.return_value = None

        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        publish_event(mock_server, event)

        assert mock_log.called
        assert any(
            "tracking data" in str(call).lower() for call in mock_log.call_args_list
        )

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.get_session_info")
    @patch("agentcat.modules.event_queue.set_last_activity")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_calculates_duration(
        self, mock_eq, mock_set_activity, mock_session, mock_tracking
    ):
        """Test publishing event calculates duration if not provided."""
        mock_server = MagicMock()
        mock_data = AgentCatData(
            project_id="project-123",
            session_id="session-123",
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=AgentCatOptions(),
        )
        mock_tracking.return_value = mock_data
        mock_session.return_value = SessionInfo()

        # Create event without duration
        event_timestamp = datetime.now(timezone.utc)
        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="session-123",
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
    @patch("agentcat.modules.event_queue.get_session_info")
    @patch("agentcat.modules.event_queue.set_last_activity")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_no_duration_no_timestamp(
        self, mock_eq, mock_set_activity, mock_session, mock_tracking
    ):
        """Test publishing event with no duration and no timestamp sets duration to None."""
        mock_server = MagicMock()
        mock_data = AgentCatData(
            project_id="project-123",
            session_id="session-123",
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=AgentCatOptions(),
        )
        mock_tracking.return_value = mock_data
        mock_session.return_value = SessionInfo()

        # Create event without duration or timestamp
        event = UnredactedEvent(event_type="mcp:tools/call", session_id="session-123")

        publish_event(mock_server, event)

        # Check duration is None
        assert event.duration is None

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.get_session_info")
    @patch("agentcat.modules.event_queue.set_last_activity")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_with_redaction_function(
        self, mock_eq, mock_set_activity, mock_session, mock_tracking
    ):
        """Test publishing event includes redaction function from options."""
        mock_server = MagicMock()
        mock_redaction_fn = MagicMock()
        mock_data = AgentCatData(
            project_id="project-123",
            session_id="session-123",
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=AgentCatOptions(redact_sensitive_information=mock_redaction_fn),
        )
        mock_tracking.return_value = mock_data
        mock_session.return_value = SessionInfo()

        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        publish_event(mock_server, event)

        # Check event was added with redaction function
        added_event = mock_eq.add.call_args[0][0]
        assert added_event.redaction_fn == mock_redaction_fn

    @patch("agentcat.modules.event_queue.get_server_tracking_data")
    @patch("agentcat.modules.event_queue.get_session_info")
    @patch("agentcat.modules.event_queue.set_last_activity")
    @patch("agentcat.modules.event_queue.event_queue")
    def test_publish_event_attaches_redact_event_hook_from_options(
        self, mock_eq, mock_set_activity, mock_session, mock_tracking
    ):
        """Test publishing event includes the event-level hook from options."""
        mock_server = MagicMock()
        mock_hook = MagicMock()
        mock_data = AgentCatData(
            project_id="project-123",
            session_id="session-123",
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=AgentCatOptions(redact_event=mock_hook),
        )
        mock_tracking.return_value = mock_data
        mock_session.return_value = SessionInfo()

        event = UnredactedEvent(
            event_type="mcp:tools/call",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
        )

        publish_event(mock_server, event)

        added_event = mock_eq.add.call_args[0][0]
        assert added_event.redact_event_fn == mock_hook


class TestEventLevelRedactionHook:
    """Test the redact_event hook in the queue pipeline."""

    @staticmethod
    def _event(**overrides) -> UnredactedEvent:
        defaults = dict(
            id="test-id",
            event_type="mcp:tools/call",
            project_id="project-123",
            session_id="session-123",
            timestamp=datetime.now(timezone.utc),
            resource_name="add_todo",
            parameters={"text": "raw sensitive value"},
            user_intent="raw secret",
        )
        defaults.update(overrides)
        return UnredactedEvent(**defaults)

    def test_hook_result_is_published(self):
        """The hook sees raw metadata and its rewrite is what gets sent."""
        seen = {}

        def hook(event):
            seen["resource_name"] = event.resource_name
            seen["parameters"] = event.parameters
            modified = event.model_copy()
            modified.parameters = {"text": "[EVENT-REDACTED]"}
            return modified

        eq = EventQueue()
        event = self._event(redact_event_fn=hook)

        with patch.object(eq, "_send_event") as mock_send:
            eq._process_event(event)

            assert seen["resource_name"] == "add_todo"
            assert seen["parameters"] == {"text": "raw sensitive value"}
            sent_event = mock_send.call_args[0][0]
            assert sent_event.parameters == {"text": "[EVENT-REDACTED]"}
            assert sent_event.redact_event_fn is None

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_hook_returning_none_drops_the_event(self, mock_log):
        eq = EventQueue()
        event = self._event(redact_event_fn=lambda event: None)

        with patch.object(eq, "_send_event") as mock_send:
            eq._process_event(event)

            mock_send.assert_not_called()
            log_messages = [str(c) for c in mock_log.call_args_list]
            assert any("dropped by redact_event hook" in m for m in log_messages)

    @patch("agentcat.modules.event_queue.write_to_log")
    def test_hook_raising_drops_the_event(self, mock_log):
        def hook(event):
            raise ValueError("hook exploded")

        eq = EventQueue()
        event = self._event(redact_event_fn=hook)

        with patch.object(eq, "_send_event") as mock_send:
            eq._process_event(event)

            mock_send.assert_not_called()
            log_messages = [str(c) for c in mock_log.call_args_list]
            assert any(
                "Failed to redact event (event-level hook)" in m
                for m in log_messages
            )

    @patch("agentcat.modules.event_queue.redact_event")
    def test_hook_runs_before_string_redaction(self, mock_redact):
        """The event hook sees raw values; string redaction runs on its output."""
        raw_seen = {}

        def hook(event):
            raw_seen["user_intent"] = event.user_intent
            modified = event.model_copy()
            modified.user_intent = "hook output"
            return modified

        string_fn = MagicMock()
        mock_redact.side_effect = lambda event, fn: event

        eq = EventQueue()
        event = self._event(redact_event_fn=hook, redaction_fn=string_fn)

        with patch.object(eq, "_send_event"):
            eq._process_event(event)

            assert raw_seen["user_intent"] == "raw secret"
            mock_redact.assert_called_once()
            redacted_arg = mock_redact.call_args[0][0]
            assert redacted_arg.user_intent == "hook output"
            assert mock_redact.call_args[0][1] is string_fn


@patch("agentcat.modules.event_queue.signal.signal")
@patch("agentcat.modules.event_queue.atexit.register")
def test_shutdown_handlers_registered(mock_atexit, mock_signal):
    """Test that shutdown handlers are registered on module import."""
    # Import the module to trigger registration
    import importlib
    import agentcat.modules.event_queue

    importlib.reload(agentcat.modules.event_queue)

    # Check signal handlers registered
    assert mock_signal.call_count >= 2
    signal_calls = mock_signal.call_args_list
    registered_signals = [call[0][0] for call in signal_calls]
    assert signal.SIGINT in registered_signals
    assert signal.SIGTERM in registered_signals

    # Check atexit handler registered
    assert mock_atexit.called


@patch("os._exit")
@patch("agentcat.modules.event_queue.signal.signal")
@patch("agentcat.modules.event_queue.event_queue")
def test_shutdown_handler_function(mock_event_queue, mock_signal, mock_exit):
    """Test the _shutdown_handler function."""
    from agentcat.modules.event_queue import _shutdown_handler

    # Call the shutdown handler with proper signal handler arguments
    _shutdown_handler(signal.SIGINT, None)

    # Verify signal handlers are reset to default
    mock_signal.assert_any_call(signal.SIGINT, signal.SIG_DFL)
    mock_signal.assert_any_call(signal.SIGTERM, signal.SIG_DFL)

    # Verify it calls destroy on the event queue
    mock_event_queue.destroy.assert_called_once()

    # Verify it exits with code 0
    mock_exit.assert_called_once_with(0)


@patch("time.sleep")
def test_destroy_cancels_pending_futures(mock_sleep):
    """Test destroy method cancels pending futures."""
    eq = EventQueue()

    # Add an event
    event = UnredactedEvent(
        id="test-id",
        event_type="mcp:tools/call",
        project_id="project-123",
        session_id="session-123",
        timestamp=datetime.now(timezone.utc),
    )
    eq.queue.put_nowait(event)

    # Mock executor
    mock_executor = MagicMock()
    eq.executor = mock_executor

    eq.destroy()

    assert eq._shutdown is True
    assert eq._shutdown_event.is_set()
    mock_executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
