"""Every customer hook takes a sync OR an async callable, on every server shape.

`AgentCatOptions` exposes five customer-supplied callables and all five are
documented to accept either. Before `modules/hooks.py` each site decided that
for itself: `identify` never awaited at all, and `event_tags` /
`event_properties` narrowed on `inspect.iscoroutine`, which matches ONLY native
coroutines. A hook returning an `asyncio.Task` — a cached in-flight lookup — or
any object implementing `__await__` was assigned into the event verbatim, which
is the `<coroutine object ...>`-on-the-wire failure that looks like it worked.

So the parametrization is the test. Four ways of expressing "a value, maybe
later" run through each hook: a plain return, a native coroutine, a Task, and a
bare `__await__` implementer. The last two are the ones the old predicate
dropped.

`flavors()` builds every server shape the installed dependency set supports, and
CI runs this file on every mcp and fastmcp leg of the compatibility matrix — so
"works on all supported SDK versions" is asserted rather than reasoned about.

`redact_sensitive_information` is not parametrized over flavors here: it runs on
the publish queue's worker THREAD, not the request path, so it is adapter- and
version-independent by construction and gets its own section against
`redact_event` directly. That thread has no event loop, which is why it needs
`drive_hook_result` rather than an await.
"""

import asyncio
from typing import Any

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import AGENTCAT_TAG_SESSION_SOURCE
from agentcat.modules.handles import derive_session_id
from agentcat.modules.hooks import await_hook_result, drive_hook_result
from agentcat.modules.redaction import redact_event
from agentcat.types import UserIdentity

from .test_utils.flavors import flavors

PROJECT = "proj_test"


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


@pytest.fixture
def log_sink():
    """Everything `write_to_log` tees to diagnostics, as the collector sees it."""
    from agentcat.modules import logging as agentcat_logging

    previous = agentcat_logging._diagnostics_sink
    lines: list[str] = []
    agentcat_logging.set_diagnostics_sink(lines.append)
    yield lines
    agentcat_logging.set_diagnostics_sink(previous)


# ── the four ways a hook can hand back a value ───────────────────────────────


class Thenable:
    """Awaitable, but neither a coroutine nor a Future.

    The shape `inspect.iscoroutine` misses that has no asyncio machinery behind
    it at all — what a library wrapping its own scheduler hands back.
    """

    def __init__(self, value: Any) -> None:
        self._value = value

    def __await__(self):
        async def _inner() -> Any:
            return self._value

        return _inner().__await__()


def _plain(value: Any):
    def hook(_request: Any, _extra: Any) -> Any:
        return value

    return hook


def _coroutine(value: Any):
    async def hook(_request: Any, _extra: Any) -> Any:
        return value

    return hook


def _task(value: Any):
    """A Task-returning hook, async because Tasks only exist on a loop.

    Since run_hook, the hook CALL runs on a worker thread with no running
    loop, so a SYNC hook can no longer call asyncio APIs — that is a
    documented v2 behavior change (MIGRATION.md). An async hook's body runs
    on the loop as always, and its returned Task pins the awaitable-unwrap
    regression: run_hook must await the coroutine AND then the Task it
    returned, not assign either into the event verbatim.
    """

    async def hook(_request: Any, _extra: Any) -> Any:
        return asyncio.ensure_future(_coroutine(value)(None, None))

    return hook


def _thenable(value: Any):
    def hook(_request: Any, _extra: Any) -> Any:
        return Thenable(value)

    return hook


WRAPPERS = [
    pytest.param(_plain, id="sync"),
    pytest.param(_coroutine, id="coroutine"),
    pytest.param(_task, id="task"),
    pytest.param(_thenable, id="thenable"),
]

FLAVORS = pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
WRAPPED = pytest.mark.parametrize("wrap", WRAPPERS)


async def _call_once(flavor, options: AgentCatOptions):
    built = flavor.build("async-hooks")
    track(built.server, PROJECT, options)
    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        return await flavor.call(client, "echo", {"text": "hi"})


# ── A. the request-path hooks, on every shape and every wrapper ──────────────


@FLAVORS
@WRAPPED
async def test_identify(flavor, wrap, capture):
    identity = UserIdentity(user_id="alice", user_name="Alice", user_data=None)
    result = await _call_once(flavor, AgentCatOptions(identify=wrap(identity)))

    assert result.is_error is False
    (event,) = capture
    assert event.identify_actor_given_id == "alice"
    assert event.identify_actor_name == "Alice"


@FLAVORS
@WRAPPED
async def test_event_tags(flavor, wrap, capture):
    result = await _call_once(flavor, AgentCatOptions(event_tags=wrap({"lane": "a"})))

    assert result.is_error is False
    (event,) = capture
    assert event.tags["lane"] == "a"
    # The SDK's own tags still merge over the customer's, as ever.
    assert AGENTCAT_TAG_SESSION_SOURCE in event.tags


@FLAVORS
@WRAPPED
async def test_event_properties(flavor, wrap, capture):
    payload = {"flag": True, "nested": {"n": 1}}
    result = await _call_once(flavor, AgentCatOptions(event_properties=wrap(payload)))

    assert result.is_error is False
    (event,) = capture
    assert event.properties == payload


@FLAVORS
@WRAPPED
async def test_resolve_session_id(flavor, wrap, capture):
    """Hook mode, which also proves the awaited value is used rather than merely
    consumed: the handle is DERIVED from what the hook returned, so a dropped
    result would mint a random one instead and the equality would fail."""
    result = await _call_once(
        flavor, AgentCatOptions(resolve_session_id=wrap("corr-7"))
    )

    assert result.is_error is False
    (event,) = capture
    assert event.session_id == derive_session_id("corr-7", PROJECT)
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "hook"


# ── B. an awaitable that fails degrades quietly, and says why ────────────────


def _raising_awaitable(_request: Any, _extra: Any) -> Any:
    async def _boom() -> Any:
        raise RuntimeError("hook exploded mid-await")

    return _boom()


@FLAVORS
@pytest.mark.parametrize(
    ("option", "hook_name"),
    [
        ("identify", "identify"),
        ("event_tags", "event_tags"),
        ("event_properties", "event_properties"),
        ("resolve_session_id", "resolve_session_id"),
    ],
)
async def test_a_failing_awaitable_degrades_and_names_the_await(
    flavor, option, hook_name, capture, log_sink
):
    """Silent for the customer's agent, loud in the log.

    The degradation contract is unchanged — analytics never fails a tool call —
    but the log now distinguishes "your hook raised" from "your hook's awaitable
    could not be driven". Without that second line a customer reading
    "identify callback error" goes looking for a bug in code that ran fine.
    """
    result = await _call_once(flavor, AgentCatOptions(**{option: _raising_awaitable}))

    assert result.is_error is False, "a failing hook must never fail the call"
    (event,) = capture
    assert event.identify_actor_given_id is None
    assert event.properties is None
    assert "lane" not in (event.tags or {})
    # resolve_session_id degrades by minting, never by publishing sessionless.
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"

    awaited = [ln for ln in log_sink if "could not be awaited" in ln]
    assert len(awaited) == 1, log_sink
    assert hook_name in awaited[0], awaited[0]


# ── C. redaction, which drives its awaitable from a thread ───────────────────


@pytest.mark.parametrize("wrap", WRAPPERS[:1] + WRAPPERS[3:])
def test_redaction_accepts_sync_and_non_coroutine_awaitables(wrap):
    """`redact_event` runs on the publish worker thread, so it cannot await.

    Parametrized without the coroutine and Task cases: both are already covered
    by `test_redaction.py`, and a Task cannot even be constructed here — there
    is no running loop on this path, which is the whole reason this hook needs
    `drive_hook_result` rather than an await.
    """
    from agentcat.types import UnredactedEvent

    def redact(value: str) -> Any:
        return wrap(value.replace("secret", "[REDACTED]"))(None, None)

    event = UnredactedEvent(
        event_type="mcp:tools/call",
        resource_name="echo",
        parameters={"arguments": {"text": "a secret value"}},
    )
    redacted = redact_event(event, redact)
    assert redacted.parameters["arguments"]["text"] == "a [REDACTED] value"


def test_redaction_reports_an_undrivable_awaitable(log_sink):
    """A hook that cannot be driven raises, so the queue drops the event rather
    than publishing it unredacted — and the log says which half broke."""
    from agentcat.types import UnredactedEvent

    def redact(_value: str) -> Any:
        async def _boom() -> str:
            raise RuntimeError("redactor exploded mid-await")

        return _boom()

    event = UnredactedEvent(
        event_type="mcp:tools/call",
        resource_name="echo",
        parameters={"arguments": {"text": "a secret value"}},
    )
    with pytest.raises(RuntimeError):
        redact_event(event, redact)

    assert [
        ln
        for ln in log_sink
        if "could not be awaited" in ln and "redact_sensitive_information" in ln
    ], log_sink


# ── D. the helpers themselves ────────────────────────────────────────────────


async def test_await_hook_result_passes_plain_values_straight_through():
    assert await await_hook_result(7, "h") == 7
    assert await await_hook_result(None, "h") is None
    # A callable is a value, not something to invoke: detection is on the
    # RESULT, which is what makes partials and decorated hooks work unchanged.
    marker = lambda: None  # noqa: E731
    assert await await_hook_result(marker, "h") is marker


def test_drive_hook_result_passes_plain_values_straight_through():
    assert drive_hook_result(7, "h") == 7
    assert drive_hook_result(None, "h") is None


async def test_await_hook_result_reraises_so_call_sites_keep_their_own_rule(
    log_sink,
):
    """The helper logs and re-raises rather than swallowing.

    Each site degrades differently — anonymous actor, dropped tags, a freshly
    minted handle, a dropped event — and collapsing those into one decision
    here would flatten four deliberate behaviors.
    """

    async def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await await_hook_result(boom(), "some_hook")

    assert [ln for ln in log_sink if "some_hook" in ln and "could not be awaited" in ln]
