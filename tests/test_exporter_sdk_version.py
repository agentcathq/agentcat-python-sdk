"""Exporters must derive the SDK version from the installed distribution.

Pins that OTLP scope version and the Sentry auth header do not carry stale
hardcoded version literals.
"""

import importlib.metadata
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from agentcat.modules.exporters.otlp import OTLPExporter
from agentcat.modules.exporters.sentry import SentryExporter
from agentcat.types import Event

INSTALLED_VERSION = importlib.metadata.version("agentcat")


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
