"""Regression: diagnostics must auto-disable inside test environments.

Ports AgentCat/agentcat-typescript-sdk#44 to Python. A pytest run must never ship
OTLP diagnostics to the live collector, even when a test calls ``track()`` with
default options and no ``DISABLE_DIAGNOSTICS`` override. Consumers of the SDK get
this protection for free; explicit ``DISABLE_DIAGNOSTICS=false`` opts back in.
"""

from unittest.mock import patch

import pytest

import agentcat
from agentcat.modules import diagnostics


@pytest.fixture(autouse=True)
def reset():
    diagnostics._reset_diagnostics_for_test()
    yield
    diagnostics._reset_diagnostics_for_test()


def test_track_does_not_enable_diagnostics_in_pytest(monkeypatch):
    # Simulate a consumer's test suite: no explicit opt-out in the environment.
    monkeypatch.delenv("DISABLE_DIAGNOSTICS", raising=False)

    with patch("agentcat.modules.diagnostics.requests.post") as mock_post:
        # track() runs init_diagnostics before validating the server, so even
        # the error path would latch diagnostics on without the guard.
        with pytest.raises(TypeError):
            agentcat.track(object(), "proj_test_env")

        assert diagnostics.is_diagnostics_enabled() is False
        diagnostics.flush_diagnostics()
        mock_post.assert_not_called()


def test_disable_diagnostics_false_force_enables_under_pytest(monkeypatch):
    # Explicit opt-in overrides the test-environment auto-disable.
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "false")
    diagnostics.init_diagnostics("proj_test_env")
    assert diagnostics.is_diagnostics_enabled() is True
