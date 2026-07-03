"""Tests for stateless mode behavior."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import agentcat
from agentcat.modules.internal import (
    get_server_tracking_data,
    set_server_tracking_data,
    reset_all_tracking_data,
)
from agentcat.modules.session import get_server_session_id, get_client_info_from_request_context
from agentcat.modules.identify import identify_session
from agentcat.types import AgentCatData, AgentCatOptions, SessionInfo, UserIdentity

from .test_utils.todo_server import create_todo_server


def _make_identify_fn(user_id="user_123", user_name="Test User"):
    """Return an identify function that always returns a UserIdentity."""
    def identify(request, context):
        return UserIdentity(user_id=user_id, user_name=user_name, user_data=None)
    return identify


class TestStatelessMode:
    """Tests for SDK stateless mode behavior."""

    def setup_method(self):
        reset_all_tracking_data()
        self.server = create_todo_server()

    def teardown_method(self):
        reset_all_tracking_data()

    def _setup_data(self, stateless=False, identify=None):
        """Create and store AgentCatData on the server."""
        options = AgentCatOptions()
        if identify:
            options.identify = identify
        data = AgentCatData(
            project_id="test_project",
            session_id="ses_existing123",
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=options,
            is_stateless=stateless,
        )
        set_server_tracking_data(self.server, data)
        return data

    def test_stateless_option_sets_flag(self):
        """AgentCatOptions(stateless=True) should set is_stateless on data."""
        data = self._setup_data(stateless=True)
        assert data.is_stateless is True

    def test_stateless_session_id_is_none(self):
        """In stateless mode, get_server_session_id() should return None."""
        self._setup_data(stateless=True)
        session_id = get_server_session_id(self.server)
        assert session_id is None

    @patch("agentcat.modules.identify.event_queue")
    def test_stateless_identify_runs_every_time(self, mock_event_queue):
        """In stateless mode, identify should run on every call (no early-return guard)."""
        mock_fn = MagicMock(return_value=UserIdentity(
            user_id="alice", user_name="Alice", user_data=None
        ))
        self._setup_data(stateless=True, identify=mock_fn)

        identify_session(self.server, MagicMock(), MagicMock())
        identify_session(self.server, MagicMock(), MagicMock())

        assert mock_fn.call_count == 2

    @patch("agentcat.modules.identify.event_queue")
    def test_stateless_identify_returns_identity(self, mock_event_queue):
        """In stateless mode, identify_session() should return the UserIdentity."""
        self._setup_data(stateless=True, identify=_make_identify_fn())

        result = identify_session(self.server, MagicMock(), MagicMock())

        assert isinstance(result, UserIdentity)
        assert result.user_id == "user_123"
        assert result.user_name == "Test User"

    @patch("agentcat.modules.identify.event_queue")
    def test_stateful_identify_runs_every_time(self, mock_event_queue):
        """Stateful mode runs identify on every request."""
        mock_fn = MagicMock(return_value=UserIdentity(
            user_id="alice", user_name="Alice", user_data=None
        ))
        self._setup_data(stateless=False, identify=mock_fn)

        # Session ID should be a string
        session_id = get_server_session_id(self.server)
        assert isinstance(session_id, str)
        assert session_id.startswith("ses_")

        identify_session(self.server, MagicMock(), MagicMock())
        identify_session(self.server, MagicMock(), MagicMock())

        assert mock_fn.call_count == 2

    def test_track_stateless_true_sets_flag(self):
        """track() with stateless=True should set is_stateless on data."""
        server = create_todo_server()
        options = AgentCatOptions(stateless=True)
        agentcat.track(server, "test_project", options)
        data = get_server_tracking_data(server)
        assert data.is_stateless is True

    def test_track_stateless_false_overrides_detection(self):
        """track() with stateless=False should force stateful even if server looks stateless."""
        server = create_todo_server()
        # Mock the server to look stateless
        server.settings = MagicMock()
        server.settings.stateless_http = True
        options = AgentCatOptions(stateless=False)
        agentcat.track(server, "test_project", options)
        data = get_server_tracking_data(server)
        assert data.is_stateless is False

    def test_track_stateless_none_auto_detects(self):
        """track() with stateless=None (default) should auto-detect from server."""
        server = create_todo_server()
        options = AgentCatOptions()  # stateless=None by default
        agentcat.track(server, "test_project", options)
        data = get_server_tracking_data(server)
        # create_todo_server() is not stateless, so should be False
        assert data.is_stateless is False

    @patch("agentcat.modules.identify.event_queue")
    def test_stateless_identify_bad_return(self, mock_event_queue):
        """In stateless mode, identify returning non-UserIdentity should return None."""
        bad_fn = MagicMock(return_value="not a UserIdentity")
        self._setup_data(stateless=True, identify=bad_fn)

        result = identify_session(self.server, MagicMock(), MagicMock())

        assert result is None
        assert bad_fn.call_count == 1

    @patch("agentcat.modules.identify.event_queue")
    def test_stateless_identify_exception(self, mock_event_queue):
        """In stateless mode, identify raising should return None, not propagate."""
        raising_fn = MagicMock(side_effect=RuntimeError("identify exploded"))
        self._setup_data(stateless=True, identify=raising_fn)

        result = identify_session(self.server, MagicMock(), MagicMock())

        assert result is None
        assert raising_fn.call_count == 1

    def _make_request_context(self, user_agent):
        """Create a mock request context with a User-Agent header."""
        ctx = MagicMock()
        ctx.request.headers = {"user-agent": user_agent}
        # No session attribute (stateless HTTP)
        ctx.session = None
        return ctx

    def test_stateless_client_info_per_request(self):
        """In stateless mode, consecutive requests with different clients return different info."""
        self._setup_data(stateless=True)

        ctx1 = self._make_request_context("Cursor/2.6.22")
        ctx2 = self._make_request_context("Claude Desktop/1.0")

        result1 = get_client_info_from_request_context(self.server, ctx1)
        result2 = get_client_info_from_request_context(self.server, ctx2)

        assert result1 == ("Cursor", "2.6.22")
        assert result2 == ("Claude Desktop", "1.0")

    def test_stateless_client_info_returns_values(self):
        """In stateless mode, get_client_info_from_request_context returns a tuple."""
        self._setup_data(stateless=True)

        ctx = self._make_request_context("Cursor/2.6.22")
        result = get_client_info_from_request_context(self.server, ctx)

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert result[0] == "Cursor"
        assert result[1] == "2.6.22"

    def test_stateful_client_info_cached_across_requests(self):
        """In stateful mode, client info is determined by the first request."""
        self._setup_data(stateless=False)

        ctx1 = self._make_request_context("Cursor/2.6.22")
        ctx2 = self._make_request_context("Claude Desktop/1.0")

        get_client_info_from_request_context(self.server, ctx1)
        get_client_info_from_request_context(self.server, ctx2)

        data = get_server_tracking_data(self.server)
        assert data.session_info.client_name == "Cursor"
        assert data.session_info.client_version == "2.6.22"
