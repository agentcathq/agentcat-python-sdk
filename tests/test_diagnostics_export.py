"""Tests for the batched OTLP export (fire-and-forget POST)."""

from unittest.mock import patch

import pytest

from agentcat.modules import diagnostics
from agentcat.modules.logging import write_to_log


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

    with patch("agentcat.modules.diagnostics.requests.post") as mock_post:
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
        "agentcat.modules.diagnostics.requests.post",
        side_effect=RuntimeError("network down"),
    ) as mock_post:
        # Must not raise.
        diagnostics.flush_diagnostics()

    # ...for the right reason. Without this, a flush that never posted at all
    # also "swallows the error", and this test would be green on a diagnostics
    # path that had stopped exporting entirely.
    mock_post.assert_called()


def test_no_post_when_disabled():
    diagnostics.init_diagnostics("proj_1", disabled=True)
    write_to_log("ignored")

    with patch("agentcat.modules.diagnostics.requests.post") as mock_post:
        diagnostics.flush_diagnostics()
        mock_post.assert_not_called()


def test_no_post_when_buffer_empty():
    diagnostics.init_diagnostics("proj_1")

    with patch("agentcat.modules.diagnostics.requests.post") as mock_post:
        diagnostics.flush_diagnostics()
        mock_post.assert_not_called()


# ── the exit-time flush is hard-bounded (audit finding 13) ───────────────────


def test_flush_at_exit_with_empty_buffer_never_posts():
    """Nothing buffered: the exit hook returns instantly, no lock, no POST."""
    diagnostics.init_diagnostics("proj_1")

    with patch("agentcat.modules.diagnostics.requests.post") as mock_post:
        diagnostics._flush_at_exit()
        mock_post.assert_not_called()


def test_flush_at_exit_posts_with_a_2s_timeout():
    """Buffered records flush at exit with the tightened 2s cap, not the 5s
    in-process default — customer shutdown is never held longer than that."""
    diagnostics.init_diagnostics("proj_1")
    write_to_log("Warning: buffered at exit")

    with patch("agentcat.modules.diagnostics.requests.post") as mock_post:
        diagnostics._flush_at_exit()
        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["timeout"] == 2.0


def test_flush_default_timeout_is_still_5s():
    diagnostics.init_diagnostics("proj_1")
    write_to_log("Warning: buffered in process")

    with patch("agentcat.modules.diagnostics.requests.post") as mock_post:
        diagnostics.flush_diagnostics()
        assert mock_post.call_args.kwargs["timeout"] == 5.0


def test_flush_at_exit_swallows_post_failures():
    diagnostics.init_diagnostics("proj_1")
    write_to_log("Warning: buffered at exit")

    with patch(
        "agentcat.modules.diagnostics.requests.post",
        side_effect=RuntimeError("network gone"),
    ):
        diagnostics._flush_at_exit()  # must not raise


def test_the_sdk_has_exactly_two_bounded_atexit_hooks():
    """The no-drain-at-exit decision, pinned: two bounded atexit hooks total —
    the diagnostics beacon (~2s cap, skipped when empty) and the event-queue
    worker stop (~1s join budget, sends nothing). Neither drains events."""
    import pathlib

    import agentcat

    src_root = pathlib.Path(agentcat.__file__).parent
    registrations = []
    for path in src_root.rglob("*.py"):
        text = path.read_text()
        if "atexit.register" in text:
            registrations.append(path.name)
    assert sorted(registrations) == ["diagnostics.py", "event_queue.py"], registrations
