"""Tests for the batched OTLP export (fire-and-forget POST)."""

from unittest.mock import patch

import pytest

from mcpcat.modules import diagnostics
from mcpcat.modules.logging import write_to_log


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    diagnostics._reset_diagnostics_for_test()
    # Force-enable past the test-environment auto-disable; HTTP is mocked.
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "false")
    monkeypatch.delenv("DIAGNOSTICS_ENDPOINT", raising=False)
    yield
    diagnostics._reset_diagnostics_for_test()


def test_flush_posts_otlp_shaped_json():
    diagnostics.init_diagnostics("proj_1")
    write_to_log("Warning: something happened")

    with patch("mcpcat.modules.diagnostics.requests.post") as mock_post:
        diagnostics.flush_diagnostics()

        mock_post.assert_called_once()
        url = mock_post.call_args.args[0]
        assert url.endswith("/v1/logs")

        payload = mock_post.call_args.kwargs["json"]
        scope_logs = payload["resourceLogs"][0]["scopeLogs"][0]
        records = scope_logs["logRecords"]
        assert any(
            "something happened" in r["body"]["stringValue"] for r in records
        )

        resource_attrs = {
            a["key"]: a["value"]["stringValue"]
            for a in payload["resourceLogs"][0]["resource"]["attributes"]
        }
        assert resource_attrs.get("agentcat.project_id") == "proj_1"


def test_flush_swallows_post_errors():
    diagnostics.init_diagnostics("proj_1")
    write_to_log("some log line")

    with patch(
        "mcpcat.modules.diagnostics.requests.post",
        side_effect=RuntimeError("network down"),
    ):
        # Must not raise.
        diagnostics.flush_diagnostics()


def test_no_post_when_disabled():
    diagnostics.init_diagnostics("proj_1", disabled=True)
    write_to_log("ignored")

    with patch("mcpcat.modules.diagnostics.requests.post") as mock_post:
        diagnostics.flush_diagnostics()
        mock_post.assert_not_called()


def test_no_post_when_buffer_empty():
    diagnostics.init_diagnostics("proj_1")

    with patch("mcpcat.modules.diagnostics.requests.post") as mock_post:
        diagnostics.flush_diagnostics()
        mock_post.assert_not_called()
