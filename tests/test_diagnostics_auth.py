"""Tests for the diagnostics bearer-token Authorization header."""

from unittest.mock import patch

import pytest

from agentcat.modules import diagnostics
from agentcat.modules.logging import write_to_log


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    diagnostics._reset_diagnostics_for_test()
    # Force-enable past the test-environment auto-disable; HTTP is mocked.
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "false")
    monkeypatch.delenv("DIAGNOSTICS_TOKEN", raising=False)
    yield
    diagnostics._reset_diagnostics_for_test()


def _flush_and_get_headers() -> dict:
    write_to_log("a log line")
    with patch("agentcat.modules.diagnostics.requests.post") as mock_post:
        diagnostics.flush_diagnostics()
        mock_post.assert_called_once()
        return mock_post.call_args.kwargs["headers"]


def test_default_bearer_token():
    diagnostics.init_diagnostics("proj_1")
    headers = _flush_and_get_headers()
    assert headers["Authorization"].startswith("Bearer dgk_sdk_diag_")


def test_token_override(monkeypatch):
    monkeypatch.setenv("DIAGNOSTICS_TOKEN", "custom-token-123")
    diagnostics.init_diagnostics("proj_1")
    headers = _flush_and_get_headers()
    assert headers["Authorization"] == "Bearer custom-token-123"


def test_empty_token_falls_back_to_default(monkeypatch):
    """Empty string is falsy → fall back to the default (header still present)."""
    monkeypatch.setenv("DIAGNOSTICS_TOKEN", "")
    diagnostics.init_diagnostics("proj_1")
    headers = _flush_and_get_headers()
    assert headers["Authorization"].startswith("Bearer dgk_sdk_diag_")
