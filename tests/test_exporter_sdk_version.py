"""Exporters must derive the SDK version from the installed distribution.

Pins that OTLP scope version and the Sentry auth header do not carry stale
hardcoded version literals.
"""

import importlib.metadata
import platform
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from agentcat.modules.exporters.otlp import OTLPExporter
from agentcat.modules.exporters.sentry import SentryExporter
from agentcat.types import Event
from agentcat.utils import get_agentcat_version

INSTALLED_VERSION = importlib.metadata.version("agentcat")

try:
    FASTMCP_VERSION: str | None = importlib.metadata.version("fastmcp")
except importlib.metadata.PackageNotFoundError:  # the test-without-fastmcp legs
    FASTMCP_VERSION = None


def make_event(**kwargs) -> Event:
    defaults = dict(
        id="evt-test-id",
        event_type="mcp:tools/call",
        project_id="project-123",
        session_id="session-123",
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return Event(**defaults)


class TestOTLPScopeVersion:
    def _export_and_get_scope(self, event: Event) -> dict:
        exporter = OTLPExporter({"endpoint": "http://localhost:4318/v1/traces"})
        exporter.session = MagicMock()
        exporter.export(event)
        assert exporter.session.post.called
        payload = exporter.session.post.call_args.kwargs["json"]
        return payload["resourceSpans"][0]["scopeSpans"][0]["scope"]

    def test_scope_version_uses_event_version_when_present(self):
        scope = self._export_and_get_scope(make_event(agentcat_version="9.9.9"))
        assert scope["version"] == "9.9.9"

    def test_scope_version_falls_back_to_installed_distribution(self):
        scope = self._export_and_get_scope(make_event(agentcat_version=None))
        assert scope["version"] == INSTALLED_VERSION

    def test_scope_version_falls_back_to_unknown_when_unresolvable(self):
        with patch(
            "agentcat.modules.exporters.otlp.get_agentcat_version", return_value=None
        ):
            scope = self._export_and_get_scope(make_event(agentcat_version=None))
        assert scope["version"] == "unknown"


class TestOTLPResourceVersionAttributes:
    """Every exported event's OTLP resource carries runtime + MCP SDK versions."""

    def _export_and_get_resource_attrs(self, event: Event) -> dict:
        exporter = OTLPExporter({"endpoint": "http://localhost:4318/v1/traces"})
        exporter.session = MagicMock()
        exporter.export(event)
        assert exporter.session.post.called
        payload = exporter.session.post.call_args.kwargs["json"]
        attrs = payload["resourceSpans"][0]["resource"]["attributes"]
        return {a["key"]: a["value"]["stringValue"] for a in attrs}

    def test_python_runtime_attributes_present(self):
        attrs = self._export_and_get_resource_attrs(make_event())
        assert attrs["process.runtime.name"] == platform.python_implementation().lower()
        assert attrs["process.runtime.version"] == platform.python_version()

    def test_mcp_sdk_version_attribute_matches_installed(self):
        attrs = self._export_and_get_resource_attrs(make_event())
        assert attrs["agentcat.mcp_sdk.version"] == importlib.metadata.version("mcp")

    def test_fastmcp_attribute_present_iff_installed(self):
        attrs = self._export_and_get_resource_attrs(make_event())
        if FASTMCP_VERSION is None:
            assert "agentcat.fastmcp_sdk.version" not in attrs
        else:
            assert attrs["agentcat.fastmcp_sdk.version"] == FASTMCP_VERSION

    def test_absent_distribution_emits_no_attribute(self):
        with patch(
            "agentcat.modules.exporters.otlp.get_dist_version", return_value=None
        ):
            attrs = self._export_and_get_resource_attrs(make_event())
        assert "agentcat.mcp_sdk.version" not in attrs
        assert "agentcat.fastmcp_sdk.version" not in attrs
        # Runtime attributes do not depend on distribution lookups.
        assert attrs["process.runtime.version"] == platform.python_version()


class TestGetDistVersion:
    """Best-effort installed-distribution reader shared by the log-line version
    suffix and the exporters."""

    def test_returns_installed_version(self):
        from agentcat.utils import get_dist_version

        get_dist_version.cache_clear()
        assert get_dist_version("agentcat") == INSTALLED_VERSION

    def test_returns_none_for_missing_distribution(self):
        from agentcat.utils import get_dist_version

        get_dist_version.cache_clear()
        assert get_dist_version("definitely-not-a-real-distribution") is None


class TestSentryAuthHeaderVersion:
    DSN = "https://abcdef1234567890@o123.ingest.sentry.io/456"

    def test_auth_header_uses_installed_distribution_version(self):
        exporter = SentryExporter({"dsn": self.DSN})
        assert f"sentry_client=agentcat/{INSTALLED_VERSION}" in exporter.auth_header

    def test_auth_header_falls_back_to_unknown_when_unresolvable(self):
        with patch(
            "agentcat.modules.exporters.sentry.get_agentcat_version",
            return_value=None,
        ):
            exporter = SentryExporter({"dsn": self.DSN})
        assert "sentry_client=agentcat/unknown" in exporter.auth_header


class TestGetAgentcatVersion:
    """The single reader of the installed distribution version.

    Every event is stamped with it and both exporters fall back to it, so its
    two behaviors — the right lookup key, and never raising — are worth pinning
    directly. (Restores the coverage that lived in tests/test_session.py before
    the function moved to agentcat.utils.)
    """

    def test_returns_the_installed_distribution_version(self):
        assert get_agentcat_version() == INSTALLED_VERSION

    @patch("importlib.metadata.version")
    def test_looks_the_version_up_under_the_distribution_name(self, mock_version):
        mock_version.return_value = "1.2.3"
        assert get_agentcat_version() == "1.2.3"
        mock_version.assert_called_once_with("agentcat")

    @patch("importlib.metadata.version")
    def test_returns_none_when_the_distribution_cannot_be_read(self, mock_version):
        """An un-readable version must never break event publishing: the field
        is simply omitted."""
        mock_version.side_effect = importlib.metadata.PackageNotFoundError("agentcat")
        assert get_agentcat_version() is None
