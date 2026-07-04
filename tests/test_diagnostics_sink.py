"""Tests for the diagnostics sink hook in logging.write_to_log."""

from agentcat.modules.logging import (
    set_debug_mode,
    set_diagnostics_sink,
    write_to_log,
)


def teardown_function():
    # Always clear the sink so one test never leaks into another.
    set_diagnostics_sink(None)


def test_sink_receives_every_entry():
    seen: list[str] = []
    set_diagnostics_sink(seen.append)

    write_to_log("hello world")

    assert len(seen) == 1
    assert "hello world" in seen[0]
    # Sink gets the timestamped, newline-free entry.
    assert seen[0].startswith("[")
    assert "\n" not in seen[0]


def test_sink_raising_never_breaks_write_to_log():
    def boom(_entry: str) -> None:
        raise RuntimeError("sink failure")

    set_diagnostics_sink(boom)

    # Must not raise.
    write_to_log("still fine")


def test_cleared_sink_stops_forwarding():
    seen: list[str] = []
    set_diagnostics_sink(seen.append)
    set_diagnostics_sink(None)

    write_to_log("after clear")

    assert seen == []


def test_sink_fires_when_debug_mode_disabled():
    """The key Python invariant: the tee is independent of debug_mode."""
    seen: list[str] = []
    set_debug_mode(False)
    set_diagnostics_sink(seen.append)

    write_to_log("debug off but tee on")

    assert len(seen) == 1
    assert "debug off but tee on" in seen[0]
