"""Tests for api_base_url configuration option."""

import os
from unittest.mock import MagicMock, patch

import pytest

from agentcat.types import AgentCatOptions

from .conftest import MCP_MAJOR


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


@pytest.mark.skipif(
    MCP_MAJOR >= 2,
    reason="needs a server flavor track() can adapt; mcp 2.x lands in Task 12",
)
class TestTrackApiBaseUrl:
    """Test that track() wires api_base_url resolution correctly."""

    def _call_track_with_patches(self, options, env_vars=None):
        """Run the real track() against a real server, with only the queue mocked.

        A bare lowlevel `Server` has no tools/list or tools/call handler yet, so
        the adapter installs nothing — but detection, data storage and the
        api-base-url resolution all run exactly as they do in production.
        """
        from mcp.server.lowlevel import Server

        from agentcat import track

        mock_eq = MagicMock()
        with patch("agentcat.modules.event_queue.event_queue", mock_eq):
            server = Server("api-base-url-test")
            if env_vars is None:
                # Clear API URL env vars to avoid interference
                env_vars = os.environ.copy()
                env_vars.pop("AGENTCAT_API_URL", None)
                env_vars.pop("MCPCAT_API_URL", None)
            with patch.dict(os.environ, env_vars, clear=True):
                track(server, project_id="proj-123", options=options)
        return mock_eq

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
