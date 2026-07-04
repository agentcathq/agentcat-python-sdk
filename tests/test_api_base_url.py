"""Tests for api_base_url configuration option."""

import os
from unittest.mock import MagicMock, patch

from agentcat.types import AgentCatOptions


class TestAgentCatOptionsApiBaseUrl:
    """Test api_base_url field on AgentCatOptions."""

    def test_default_is_none(self):
        """AgentCatOptions should have api_base_url default to None."""
        opts = AgentCatOptions()
        assert opts.api_base_url is None

    def test_can_set_api_base_url(self):
        """AgentCatOptions should accept an api_base_url parameter."""
        opts = AgentCatOptions(api_base_url="https://custom.example.com")
        assert opts.api_base_url == "https://custom.example.com"


class TestEventQueueConfigure:
    """Test EventQueue.configure() method."""

    @patch("agentcat.modules.event_queue.EventsApi")
    @patch("agentcat.modules.event_queue.ApiClient")
    @patch("agentcat.modules.event_queue.Configuration")
    def test_configure_changes_api_base_url(
        self, mock_configuration, mock_api_client, mock_events_api
    ):
        """configure() should recreate the API client with the new base URL."""
        from agentcat.modules.event_queue import EventQueue

        eq = EventQueue(api_client=MagicMock())
        eq.configure("https://custom.example.com")

        mock_configuration.assert_called_with(host="https://custom.example.com")
        mock_api_client.assert_called_once_with(
            configuration=mock_configuration.return_value
        )
        mock_events_api.assert_called_once_with(
            api_client=mock_api_client.return_value
        )
        assert eq.api_client == mock_events_api.return_value

    @patch("agentcat.modules.event_queue.EventsApi")
    @patch("agentcat.modules.event_queue.ApiClient")
    @patch("agentcat.modules.event_queue.Configuration")
    def test_default_url_used_when_not_configured(
        self, mock_configuration, mock_api_client, mock_events_api
    ):
        """EventQueue() should use AGENTCAT_API_URL by default."""
        from agentcat.modules.constants import AGENTCAT_API_URL
        from agentcat.modules.event_queue import EventQueue

        eq = EventQueue()

        # Check that Configuration was called with the default URL
        mock_configuration.assert_called_with(host=AGENTCAT_API_URL)


class TestTrackApiBaseUrl:
    """Test that track() wires api_base_url resolution correctly."""

    # Common patches needed to isolate track() from real MCP server logic
    TRACK_PATCHES = [
        "agentcat.is_community_fastmcp_v3",
        "agentcat.is_community_fastmcp_v2",
        "agentcat.is_official_fastmcp_server",
        "agentcat.is_compatible_server",
        "agentcat._apply_server_tracking",
        "agentcat.get_session_info",
        "agentcat.set_server_tracking_data",
    ]

    def _call_track_with_patches(self, options, env_vars=None):
        """Helper to call track() with all internals mocked, returning a mock event_queue."""
        from agentcat import track

        mock_eq = MagicMock()
        patches = {}
        for p in self.TRACK_PATCHES:
            patches[p] = patch(p)

        eq_patch = patch("agentcat.modules.event_queue.event_queue", mock_eq)

        started = []
        try:
            for name, p in patches.items():
                m = p.start()
                started.append(p)
                if name == "agentcat.is_compatible_server":
                    m.return_value = True
                elif name in (
                    "agentcat.is_community_fastmcp_v3",
                    "agentcat.is_community_fastmcp_v2",
                    "agentcat.is_official_fastmcp_server",
                ):
                    m.return_value = False
                elif name == "agentcat.get_session_info":
                    from agentcat.types import SessionInfo
                    m.return_value = SessionInfo()

            eq_patch.start()
            started.append(eq_patch)

            server = MagicMock()
            if env_vars is not None:
                with patch.dict(os.environ, env_vars, clear=True):
                    track(server, project_id="proj-123", options=options)
            else:
                # Clear API URL env vars to avoid interference
                env = os.environ.copy()
                env.pop("AGENTCAT_API_URL", None)
                env.pop("MCPCAT_API_URL", None)
                with patch.dict(os.environ, env, clear=True):
                    track(server, project_id="proj-123", options=options)

            return mock_eq
        finally:
            for p in started:
                p.stop()

    def test_option_overrides_default(self):
        """api_base_url option should trigger configure() on event_queue."""
        opts = AgentCatOptions(api_base_url="https://custom.example.com")
        mock_eq = self._call_track_with_patches(opts)
        mock_eq.configure.assert_called_once_with("https://custom.example.com")

    def test_env_var_overrides_default(self):
        """MCPCAT_API_URL env var should trigger configure() when no option set."""
        opts = AgentCatOptions()
        mock_eq = self._call_track_with_patches(
            opts, env_vars={"MCPCAT_API_URL": "https://env.example.com"}
        )
        mock_eq.configure.assert_called_once_with("https://env.example.com")

    def test_agentcat_env_var_overrides_default(self):
        """AGENTCAT_API_URL env var should trigger configure() when no option set."""
        opts = AgentCatOptions()
        mock_eq = self._call_track_with_patches(
            opts, env_vars={"AGENTCAT_API_URL": "https://new.example.com"}
        )
        mock_eq.configure.assert_called_once_with("https://new.example.com")

    def test_agentcat_env_var_takes_precedence_over_mcpcat(self):
        """AGENTCAT_API_URL wins over the legacy MCPCAT_API_URL fallback."""
        opts = AgentCatOptions()
        mock_eq = self._call_track_with_patches(
            opts,
            env_vars={
                "AGENTCAT_API_URL": "https://new.example.com",
                "MCPCAT_API_URL": "https://legacy.example.com",
            },
        )
        mock_eq.configure.assert_called_once_with("https://new.example.com")

    def test_option_takes_precedence_over_env_var(self):
        """api_base_url option should take precedence over MCPCAT_API_URL env var."""
        opts = AgentCatOptions(api_base_url="https://option.example.com")
        mock_eq = self._call_track_with_patches(
            opts, env_vars={"MCPCAT_API_URL": "https://env.example.com"}
        )
        mock_eq.configure.assert_called_once_with("https://option.example.com")

    def test_no_configure_when_using_default(self):
        """configure() should NOT be called when neither option nor env var is set."""
        opts = AgentCatOptions()
        mock_eq = self._call_track_with_patches(opts)
        mock_eq.configure.assert_not_called()
