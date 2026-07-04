"""Tests for diagnostics opt-out (option + DISABLE_DIAGNOSTICS env var).

Diagnostics auto-disable in test environments (PYTEST_CURRENT_TEST /
PYTEST_VERSION set). To exercise the "enabled by default" behavior we simulate a
non-test environment by deleting those markers; to exercise the auto-disable we
set just the relevant marker.
"""

import pytest

from agentcat.modules import diagnostics


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    diagnostics._reset_diagnostics_for_test()
    # Force-enable past the test-environment auto-disable; HTTP stays unused here.
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "false")
    yield
    diagnostics._reset_diagnostics_for_test()


def _simulate_non_test_env(monkeypatch):
    """Make _is_test_environment() report False for default-enabled behavior."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("PYTEST_VERSION", raising=False)
    monkeypatch.delenv("DISABLE_DIAGNOSTICS", raising=False)


def test_enabled_by_default(monkeypatch):
    _simulate_non_test_env(monkeypatch)
    diagnostics.init_diagnostics("proj_1")
    assert diagnostics.is_diagnostics_enabled() is True


def test_disabled_via_option():
    diagnostics.init_diagnostics("proj_1", disabled=True)
    assert diagnostics.is_diagnostics_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_disabled_via_env(monkeypatch, value):
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", value)
    diagnostics.init_diagnostics("proj_1")
    assert diagnostics.is_diagnostics_enabled() is False


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_falsy_env_force_enables(monkeypatch, value):
    """Explicit falsy values are a deliberate opt-in, even under pytest."""
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", value)
    diagnostics.init_diagnostics("proj_1")
    assert diagnostics.is_diagnostics_enabled() is True


def test_whitespace_is_treated_as_unset(monkeypatch):
    """Whitespace-only is unset, so default behavior applies (enabled outside tests)."""
    _simulate_non_test_env(monkeypatch)
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "  ")
    diagnostics.init_diagnostics("proj_1")
    assert diagnostics.is_diagnostics_enabled() is True


def test_disabled_by_default_when_pytest_current_test_set(monkeypatch):
    _simulate_non_test_env(monkeypatch)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "some_test (call)")
    diagnostics.init_diagnostics("proj_1")
    assert diagnostics.is_diagnostics_enabled() is False


def test_disabled_by_default_when_pytest_version_set(monkeypatch):
    _simulate_non_test_env(monkeypatch)
    monkeypatch.setenv("PYTEST_VERSION", "9.0.2")
    diagnostics.init_diagnostics("proj_1")
    assert diagnostics.is_diagnostics_enabled() is False


def test_disable_false_force_enables_in_test_environment(monkeypatch):
    """DISABLE_DIAGNOSTICS=false overrides the test-environment auto-disable."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "some_test (call)")
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "false")
    diagnostics.init_diagnostics("proj_1")
    assert diagnostics.is_diagnostics_enabled() is True
