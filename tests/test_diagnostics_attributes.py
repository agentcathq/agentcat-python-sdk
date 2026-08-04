"""Tests for static OTLP resource attributes (identity + environment)."""

import importlib.metadata

import pytest

from agentcat.modules import diagnostics
from agentcat.modules.constants import DIAGNOSTICS_SCOPE_NAME


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    diagnostics._reset_diagnostics_for_test()
    # Force-enable past the test-environment auto-disable; HTTP is mocked.
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "false")
    yield
    diagnostics._reset_diagnostics_for_test()


def _attrs() -> dict[str, str]:
    return {
        a["key"]: a["value"]["stringValue"]
        for a in diagnostics._get_static_attributes_for_test()
    }


def test_project_id_present_no_install_id():
    diagnostics.init_diagnostics("proj_ABC")
    attrs = _attrs()
    assert attrs.get("agentcat.project_id") == "proj_ABC"
    assert "agentcat.install_id" not in attrs


def test_anonymous_install_id_when_no_project():
    diagnostics.init_diagnostics(None)
    attrs = _attrs()
    assert "agentcat.project_id" not in attrs
    assert attrs.get("agentcat.install_id")


def test_install_id_stable_across_reinit():
    diagnostics.init_diagnostics(None)
    first = _attrs().get("agentcat.install_id")

    diagnostics._reset_diagnostics_for_test()
    diagnostics.init_diagnostics(None)
    second = _attrs().get("agentcat.install_id")

    assert first and first == second


def test_sdk_and_environment_metadata_present():
    diagnostics.init_diagnostics("proj_1")
    attrs = _attrs()
    assert DIAGNOSTICS_SCOPE_NAME == "agentcat-diagnostics"
    assert attrs.get("agentcat.sdk.language") == "python"
    assert attrs.get("agentcat.sdk.version")
    assert attrs.get("os.type")
    assert attrs.get("process.runtime.name")
    assert attrs.get("process.runtime.version")


def test_mcp_sdk_version_attributes():
    """Both MCP SDK distributions are reported when installed, omitted when not."""
    diagnostics.init_diagnostics("proj_1")
    attrs = _attrs()
    assert attrs.get("agentcat.mcp_sdk.version") == importlib.metadata.version("mcp")

    try:
        fastmcp_version = importlib.metadata.version("fastmcp")
    except importlib.metadata.PackageNotFoundError:
        assert "agentcat.fastmcp_sdk.version" not in attrs
    else:
        assert attrs.get("agentcat.fastmcp_sdk.version") == fastmcp_version
