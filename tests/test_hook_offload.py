"""run_hook: customer hooks may block, raise anything, or hang — never the loop.

Regression suite for audit findings 10 (sync hooks ran inline on the event
loop, so one blocking hook stalled every concurrent request) and 12 (hook
isolation caught only Exception, so SystemExit / a spontaneous CancelledError
rode the request path).

The containment contract under test:
- a blocking SYNC hook suspends only its own request — the loop keeps serving;
- a hook slower than the timeout degrades that call and nothing else;
- SystemExit and a hook's own CancelledError become HookExecutionError, a
  plain Exception every call site's degradation rule already catches;
- genuine cancellation of the enclosing task still propagates.
"""

import asyncio
import sys
import time
from typing import Any

import pytest

from agentcat.modules.handles import resolve_handles
from agentcat.modules.hooks import HookExecutionError, run_hook
from agentcat.modules.identify import resolve_identity
from agentcat.types import AgentCatData, AgentCatOptions


@pytest.fixture
def log_sink():
    """Everything `write_to_log` tees to diagnostics, as the collector sees it."""
    from agentcat.modules import logging as agentcat_logging

    previous = agentcat_logging._diagnostics_sink
    lines: list[str] = []
    agentcat_logging.set_diagnostics_sink(lines.append)
    yield lines
    agentcat_logging.set_diagnostics_sink(previous)


def _data(**option_overrides: Any) -> AgentCatData:
    return AgentCatData(
        project_id="proj_test",
        options=AgentCatOptions(**option_overrides),
        server_name="test-server",
        server_version="1.0.0",
    )


# ── the loop stays responsive ────────────────────────────────────────────────


async def test_blocking_sync_hook_does_not_stall_the_loop():
    """Finding 10's repro: while a sync hook sleeps on its worker thread, a
    sibling coroutine must keep running. Inline execution scores ~0 ticks."""
    ticks = 0
    running = True

    async def ticker() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.01)

    def slow_hook(_request: Any, _extra: Any) -> str:
        time.sleep(0.5)
        return "done"

    task = asyncio.create_task(ticker())
    try:
        result = await run_hook(slow_hook, "identify", None, None)
    finally:
        running = False
        await task

    assert result == "done"
    assert ticks >= 10, f"loop was stalled: only {ticks} ticks during a 0.5s hook"


# ── timeout ──────────────────────────────────────────────────────────────────


async def test_sync_hook_timeout_degrades_and_logs(log_sink):
    def wedged(_request: Any, _extra: Any) -> str:
        time.sleep(3)
        return "too late"

    start = time.monotonic()
    with pytest.raises(HookExecutionError):
        await run_hook(wedged, "identify", None, None, timeout=0.2)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"timeout did not fire promptly: {elapsed:.1f}s"
    assert any("timed out" in line for line in log_sink)


async def test_async_hook_timeout_is_enforced_too():
    async def wedged(_request: Any, _extra: Any) -> str:
        await asyncio.sleep(3)
        return "too late"

    start = time.monotonic()
    with pytest.raises(HookExecutionError):
        await run_hook(wedged, "event_tags", None, None, timeout=0.2)
    assert time.monotonic() - start < 1.0


# ── containment of BaseException escapees ────────────────────────────────────


async def test_sync_hook_system_exit_is_contained():
    def exits(_request: Any, _extra: Any) -> None:
        sys.exit(3)

    with pytest.raises(HookExecutionError, match="SystemExit"):
        await run_hook(exits, "identify", None, None)


async def test_sync_hook_spontaneous_cancelled_error_is_contained():
    """A CancelledError raised BY the hook (e.g. .result() on a cancelled
    future from its cache layer) is the hook failing, not us being cancelled."""

    def cancels(_request: Any, _extra: Any) -> None:
        raise asyncio.CancelledError()

    with pytest.raises(HookExecutionError, match="CancelledError"):
        await run_hook(cancels, "resolve_session_id", None, None)


async def test_async_hook_system_exit_is_contained():
    async def exits(_request: Any, _extra: Any) -> None:
        sys.exit(3)

    with pytest.raises(HookExecutionError, match="SystemExit"):
        await run_hook(exits, "event_properties", None, None)


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="Task.cancelling() probe needs 3.11; 3.10 conservatively re-raises",
)
async def test_async_hook_spontaneous_cancelled_error_is_contained():
    async def cancels(_request: Any, _extra: Any) -> None:
        raise asyncio.CancelledError()

    with pytest.raises(HookExecutionError, match="CancelledError"):
        await run_hook(cancels, "identify", None, None)


async def test_genuine_task_cancellation_still_propagates():
    """Client disconnects mid-call: the enclosing task's cancellation must not
    be swallowed by hook containment."""

    def wedged(_request: Any, _extra: Any) -> str:
        time.sleep(3)
        return "unreachable"

    task = asyncio.create_task(run_hook(wedged, "identify", None, None))
    await asyncio.sleep(0.05)  # let it enter the hook
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── ordinary failures keep their shape ───────────────────────────────────────


async def test_sync_hook_exception_becomes_hook_execution_error():
    def broken(_request: Any, _extra: Any) -> None:
        raise ValueError("boom")

    with pytest.raises(HookExecutionError, match="ValueError"):
        await run_hook(broken, "identify", None, None)


async def test_well_behaved_hooks_are_unchanged():
    def sync_hook(_request: Any, _extra: Any) -> str:
        return "sync"

    async def async_hook(_request: Any, _extra: Any) -> str:
        return "async"

    assert await run_hook(sync_hook, "identify", None, None) == "sync"
    assert await run_hook(async_hook, "identify", None, None) == "async"


# ── the call sites degrade by their own rules ────────────────────────────────


async def test_identify_degrades_to_anonymous_on_system_exit():
    def exits(_request: Any, _extra: Any) -> Any:
        sys.exit(3)

    data = _data(identify=exits)
    assert await resolve_identity(data, None, None) is None


async def test_resolve_session_id_mints_on_system_exit():
    def exits(_request: Any, _extra: Any) -> str:
        sys.exit(3)

    resolution = await resolve_handles(
        {}, AgentCatOptions(resolve_session_id=exits), "proj_test", None, None
    )
    assert resolution.session_source == "minted"
    assert resolution.session_id.startswith("ses_")
    assert resolution.hook_mode is True
