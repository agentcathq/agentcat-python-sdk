"""Comprehensive unit tests for session.py module."""

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time

from mcpcat.modules.constants import INACTIVITY_TIMEOUT_IN_MINUTES, SESSION_ID_PREFIX
from mcpcat.modules.internal import get_server_tracking_data, set_server_tracking_data
from mcpcat.modules.session import (
    get_agentcat_version,
    get_server_session_id,
    get_session_info,
    new_session_id,
    set_last_activity,
)
from mcpcat.types import MCPCatData, MCPCatOptions, SessionInfo

from .test_utils.todo_server import create_todo_server


class TestNewSessionId:
    """Test the new_session_id function."""

    def test_generates_unique_ids(self):
        """Test that new_session_id generates unique IDs."""
        ids = [new_session_id() for _ in range(100)]
        assert len(set(ids)) == 100  # All IDs should be unique

    def test_session_id_has_correct_prefix(self):
        """Test that session ID has the correct prefix."""
        session_id = new_session_id()
        assert session_id.startswith(SESSION_ID_PREFIX)

    def test_session_id_format(self):
        """Test that session ID follows expected format."""
        session_id = new_session_id()
        # Format should be: prefix_ksuid (e.g., ses_2aYXpLJGvKU1234567890abcdef)
        parts = session_id.split("_")
        assert len(parts) == 2
        assert parts[0] == SESSION_ID_PREFIX
        assert len(parts[1]) > 0  # KSUID part should not be empty


class TestGetMcpcatVersion:
    """Test the get_agentcat_version function."""

    @patch("importlib.metadata.version")
    def test_returns_correct_version(self, mock_version):
        """Test that get_agentcat_version returns the correct version."""
        mock_version.return_value = "1.2.3"
        assert get_agentcat_version() == "1.2.3"
        mock_version.assert_called_once_with("mcpcat")

    @patch("importlib.metadata.version")
    def test_returns_none_on_exception(self, mock_version):
        """Test that get_agentcat_version returns None when an exception occurs."""
        mock_version.side_effect = Exception("Package not found")
        assert get_agentcat_version() is None


class TestGetSessionInfo:
    """Test the get_session_info function."""

    def setup_method(self):
        """Set up test fixtures."""
        self.server = create_todo_server()

    def test_without_mcpcat_data(self):
        """Test get_session_info without MCPCat data."""
        session_info = get_session_info(self.server, None)

        assert session_info.ip_address is None
        assert (
            session_info.sdk_language
            == f"Python {sys.version_info.major}.{sys.version_info.minor}"
        )
        assert session_info.agentcat_version == get_agentcat_version()
        assert session_info.server_name == "todo-server"
        assert (
            session_info.server_version is None
        )  # FastMCP doesn't have version attribute
        assert session_info.client_name is None
        assert session_info.client_version is None
        assert session_info.identify_actor_given_id is None
        assert session_info.identify_actor_name is None
        assert session_info.identify_data is None

    def test_with_mcpcat_data_no_actor(self):
        """Test get_session_info with MCPCat data but no identified actor."""
        data = MCPCatData(
            project_id="test_project",
            session_id="test_session",
            session_info=SessionInfo(client_name="TestClient", client_version="2.0.0"),
            last_activity=datetime.now(timezone.utc),
            options=MCPCatOptions(),
        )

        session_info = get_session_info(self.server, data)

        assert session_info.client_name == "TestClient"
        assert session_info.client_version == "2.0.0"
        assert session_info.identify_actor_given_id is None
        assert session_info.identify_actor_name is None
        assert session_info.identify_data is None

        # Verify that the session_info was updated in the data object
        assert data.session_info == session_info

    def test_session_info_never_carries_identity(self):
        """get_session_info returns None for identity fields."""
        data = MCPCatData(
            project_id="test_project",
            session_id="test_session",
            session_info=SessionInfo(client_name="TestClient", client_version="2.0.0"),
            last_activity=datetime.now(timezone.utc),
            options=MCPCatOptions(),
        )

        session_info = get_session_info(self.server, data)

        assert session_info.identify_actor_given_id is None
        assert session_info.identify_actor_name is None
        assert session_info.identify_data is None

    def test_server_without_name_or_version(self):
        """Test get_session_info with a server that doesn't have name or version attributes."""
        mock_server = MagicMock()
        # Remove name and version attributes
        del mock_server.name
        del mock_server.version

        session_info = get_session_info(mock_server, None)

        assert session_info.server_name is None
        assert session_info.server_version is None

    def test_get_session_info_with_tracked_server(self):
        """Test get_session_info when server has tracked data."""
        # Set up initial data
        data = MCPCatData(
            project_id="test_project",
            session_id="test_session",
            session_info=SessionInfo(
                client_name="TrackedClient", client_version="3.0.0"
            ),
            last_activity=datetime.now(timezone.utc),
            options=MCPCatOptions(),
        )

        # Store data using set_server_tracking_data
        set_server_tracking_data(self.server, data)

        # When called without data, get_session_info returns basic info only
        session_info_no_data = get_session_info(self.server, None)
        assert session_info_no_data.client_name is None
        assert session_info_no_data.client_version is None
        assert session_info_no_data.server_name == "todo-server"

        # When called with data, get_session_info uses and updates that data
        session_info_with_data = get_session_info(self.server, data)
        assert session_info_with_data.client_name == "TrackedClient"
        assert session_info_with_data.client_version == "3.0.0"
        assert session_info_with_data.server_name == "todo-server"


class TestSetLastActivity:
    """Test the set_last_activity function."""

    def setup_method(self):
        """Set up test fixtures."""
        self.server = create_todo_server()

    def test_updates_last_activity(self):
        """Test that set_last_activity updates the last activity timestamp."""
        initial_time = datetime.now(timezone.utc)

        data = MCPCatData(
            project_id="test_project",
            session_id="test_session",
            session_info=SessionInfo(),
            last_activity=initial_time,
            options=MCPCatOptions(),
        )

        # Set up the server with tracking data
        set_server_tracking_data(self.server, data)

        # Move time forward
        with freeze_time(initial_time + timedelta(minutes=5)):
            set_last_activity(self.server)

        # Verify the timestamp was updated
        assert data.last_activity > initial_time

    def test_raises_exception_when_no_data(self):
        """Test that set_last_activity raises exception when no data is found."""
        with pytest.raises(Exception) as exc_info:
            set_last_activity(self.server)

        assert str(exc_info.value) == "MCPCat data not initialized for this server"


class TestGetServerSessionId:
    """Test the get_server_session_id function."""

    def setup_method(self):
        """Set up test fixtures."""
        self.server = create_todo_server()
        self.initial_time = datetime.now(timezone.utc)
        self.initial_session_id = "ses_initial123"

        self.data = MCPCatData(
            project_id="test_project",
            session_id=self.initial_session_id,
            session_info=SessionInfo(),
            last_activity=self.initial_time,
            options=MCPCatOptions(),
        )

    def test_returns_existing_session_when_not_timed_out(self):
        """Test that existing session ID is returned when not timed out."""
        set_server_tracking_data(self.server, self.data)

        # Test within timeout period (e.g., 10 minutes later)
        with freeze_time(self.initial_time + timedelta(minutes=10)):
            session_id = get_server_session_id(self.server)

        assert session_id == self.initial_session_id
        # Verify last activity was updated
        assert self.data.last_activity > self.initial_time

    def test_creates_new_session_when_timed_out(self):
        """Test that new session ID is created when session has timed out."""
        set_server_tracking_data(self.server, self.data)

        # Test after timeout period (e.g., 31 minutes later)
        timeout_time = self.initial_time + timedelta(
            minutes=INACTIVITY_TIMEOUT_IN_MINUTES + 1
        )
        with freeze_time(timeout_time):
            session_id = get_server_session_id(self.server)

        # Should have a new session ID
        assert session_id != self.initial_session_id
        assert session_id.startswith(SESSION_ID_PREFIX)
        assert self.data.session_id == session_id
        # Verify last activity was updated to current time
        assert self.data.last_activity == timeout_time

    def test_exactly_at_timeout_boundary(self):
        """Test behavior exactly at the timeout boundary."""
        set_server_tracking_data(self.server, self.data)

        # Test exactly at timeout boundary
        boundary_time = self.initial_time + timedelta(
            minutes=INACTIVITY_TIMEOUT_IN_MINUTES
        )
        with freeze_time(boundary_time):
            session_id = get_server_session_id(self.server)

        # Should not timeout at exact boundary (> not >=)
        assert session_id == self.initial_session_id

    def test_raises_exception_when_no_data(self):
        """Test that get_server_session_id raises exception when no data is found."""
        with pytest.raises(Exception) as exc_info:
            get_server_session_id(self.server)

        assert str(exc_info.value) == "MCPCat data not initialized for this server"

    def test_multiple_calls_with_activity(self):
        """Test multiple calls to get_server_session_id with activity between them."""
        set_server_tracking_data(self.server, self.data)

        # First call at initial time
        with freeze_time(self.initial_time):
            session_id1 = get_server_session_id(self.server)

        # Activity at 20 minutes - should reset timeout
        activity_time = self.initial_time + timedelta(minutes=20)
        with freeze_time(activity_time):
            get_server_session_id(self.server)
            # This updates last_activity to activity_time

        # Call 40 minutes after initial time (but only 20 minutes after last activity)
        with freeze_time(self.initial_time + timedelta(minutes=40)):
            session_id2 = get_server_session_id(self.server)

        # Should still be the same session since last activity was only 20 minutes ago
        assert session_id1 == session_id2 == self.initial_session_id

    def test_timeout_calculation_edge_cases(self):
        """Test edge cases in timeout calculation."""
        set_server_tracking_data(self.server, self.data)

        # Test just before timeout
        with freeze_time(
            self.initial_time + timedelta(minutes=INACTIVITY_TIMEOUT_IN_MINUTES - 1)
        ):
            session_id = get_server_session_id(self.server)
            assert session_id == self.initial_session_id

        # Re-initialize data to reset the session for the next test
        self.data.session_id = self.initial_session_id
        self.data.last_activity = self.initial_time

        # Test just after timeout
        with freeze_time(
            self.initial_time + timedelta(minutes=INACTIVITY_TIMEOUT_IN_MINUTES + 1)
        ):
            session_id = get_server_session_id(self.server)
            assert session_id != self.initial_session_id


class TestIntegration:
    """Integration tests for session management."""

    @freeze_time("2024-01-01 12:00:00")
    def test_full_session_lifecycle(self):
        """Test a complete session lifecycle with timeout and renewal."""
        server = create_todo_server()

        # Create initial session data
        initial_session_id = new_session_id()
        data = MCPCatData(
            project_id="integration_project",
            session_id=initial_session_id,
            session_info=SessionInfo(
                client_name="IntegrationClient", client_version="1.0.0"
            ),
            last_activity=datetime.now(timezone.utc),
            options=MCPCatOptions(),
        )

        set_server_tracking_data(server, data)

        # Get initial session
        session_id = get_server_session_id(server)
        assert session_id == initial_session_id

        # Get session info
        session_info = get_session_info(server, data)
        assert session_info.server_name == "todo-server"
        assert session_info.client_name == "IntegrationClient"

        assert session_info.identify_actor_given_id is None
        assert session_info.identify_actor_name is None

        # Test session timeout and renewal
        with freeze_time("2024-01-01 12:31:00"):  # 31 minutes later
            new_sid = get_server_session_id(server)
            assert new_sid != initial_session_id

            new_data = MCPCatData(
                project_id="integration_project",
                session_id=new_sid,
                session_info=SessionInfo(
                    client_name="IntegrationClient", client_version="1.0.0"
                ),
                last_activity=datetime(2024, 1, 1, 12, 31, 0),
                options=MCPCatOptions(),
            )
            session_info = get_session_info(server, new_data)
            assert session_info.identify_actor_given_id is None
            assert session_info.identify_actor_name is None

    def test_session_persistence_across_function_calls(self):
        """Test that session persists correctly across multiple function calls."""
        server = create_todo_server()

        # Initialize tracking data
        data = MCPCatData(
            project_id="persistence_test",
            session_id=new_session_id(),
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=MCPCatOptions(),
        )

        set_server_tracking_data(server, data)

        # Multiple calls should return same session
        session_ids = []
        for _ in range(5):
            session_ids.append(get_server_session_id(server))

        assert len(set(session_ids)) == 1  # All should be the same

        # Verify activity tracking works
        original_activity = data.last_activity
        with freeze_time(datetime.now(timezone.utc) + timedelta(seconds=5)):
            set_last_activity(server)

        assert data.last_activity > original_activity

    def test_session_info_updates_tracked_data(self):
        """get_session_info updates the tracked data's session_info."""
        server = create_todo_server()

        data = MCPCatData(
            project_id="update_test",
            session_id="update_session",
            session_info=SessionInfo(
                client_name="UpdateClient", client_version="1.0.0"
            ),
            last_activity=datetime.now(timezone.utc),
            options=MCPCatOptions(),
        )

        set_server_tracking_data(server, data)

        session_info = get_session_info(server, data)

        assert session_info.identify_actor_given_id is None
        assert session_info.identify_actor_name is None
        assert session_info.identify_data is None

        # Verify the data object was updated
        assert data.session_info == session_info

        # Verify that the server's tracked data was also updated via set_server_tracking_data
        stored_data = get_server_tracking_data(server)
        assert stored_data.session_info == session_info
