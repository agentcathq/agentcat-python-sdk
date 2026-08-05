"""25 simultaneous tool calls, each carrying its own handle, on every flavor.

Handles are per REQUEST in v2 — resolved from that call's own arguments, and
never held anywhere between the resolve and the publish that reads it. This
module is the cross-flavor proof, and it is written so that it cannot pass
against an implementation that keeps the resolution anywhere shared.

**Why the barrier is the whole test.** A concurrency test whose calls do not
actually overlap inside the window under test proves nothing: each call
resolves, publishes and decorates before the next one starts, so a
last-write-wins store looks exactly like per-request state. So every tool body
blocks until all 25 have arrived, which puts all 25 calls provably between
their own `resolve_call` and their own publish at the same moment — and
`peak_concurrency` is asserted, so a transport that could not interleave fails
the test instead of quietly weakening it.

Each call supplies a DISTINCT `session_id`, so what every event and every response
must name is knowable exactly rather than merely "different from the others".
"""

import asyncio
import json

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import (
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_INSTRUCTIONS_KEY,
    MINT_BACK_HEADER_SESSION,
    SESSION_ID_PARAM,
)

from .test_utils import NEEDS_CONCURRENT_DISPATCH, sid
from .test_utils.flavors import flavors

# Every test here asserts `barrier.peak == TOTAL`, which is unsatisfiable on an
# SDK that handles messages serially. See `NEEDS_CONCURRENT_DISPATCH`.
pytestmark = NEEDS_CONCURRENT_DISPATCH

TOTAL = 25


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


class Barrier:
    """Holds every concurrent tool body until all of them have arrived.

    `peak` is the largest number of bodies that were inside at once. The tests
    assert it reaches `total`, which is what makes them real: it is the
    difference between "25 calls happened" and "25 calls were simultaneously
    between their own resolve and their own publish".
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.in_flight = 0
        self.peak = 0
        self.open = asyncio.Event()

    async def wait(self) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        if self.in_flight >= self.total:
            self.open.set()
        try:
            await self.open.wait()
        finally:
            self.in_flight -= 1


def _session_id(index: int) -> str:
    # Fixed width, so no handle is a prefix of another and "names only its own"
    # can be asserted by substring. Must be a SHAPE-VALID id — anything else is
    # now rejected as `invalid` and the test would prove nothing about
    # cross-attribution.
    return sid(f"conc{index:02d}")


def _call_events(capture) -> list:
    return [e for e in capture if e.event_type == "mcp:tools/call"]


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_simultaneous_calls_never_cross_attribute_a_handle(flavor, capture):
    """25 calls, all inside their own window at once, each with its own handle.

    Two independent things must hold, and a shared resolution breaks both: the
    event AgentCat publishes for a call names that call's handle, and the
    result the agent is handed back mirrors that same one and no other.
    """
    barrier = Barrier(TOTAL)
    built = flavor.build("concurrency", hook=barrier.wait)
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        # Listed first, so the output-injection registry is armed and the
        # structured mirror — the handle the AGENT is told — is in play.
        listed = await flavor.list_tools(client)
        echo = next(tool for tool in listed if tool.name == "echo")
        assert SESSION_ID_PARAM in echo.input_schema["properties"]
        assert MCP_INSTRUCTIONS_KEY in (echo.output_schema or {})["properties"]

        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    flavor.call(
                        client,
                        "echo",
                        {"text": f"t{index:02d}", SESSION_ID_PARAM: _session_id(index)},
                    )
                    for index in range(TOTAL)
                )
            ),
            timeout=60,
        )

    assert barrier.peak == TOTAL, (
        f"only {barrier.peak} of {TOTAL} calls were ever inside the window at "
        "once; this transport cannot prove anything about concurrency"
    )

    # ── what the agent was handed ────────────────────────────────────────────
    for index, result in enumerate(results):
        mine = _session_id(index)
        assert result.text.startswith(f"echo:t{index:02d}")
        assert result.structured[MCP_INSTRUCTIONS_KEY][SESSION_ID_PARAM] == mine
        # Nobody else's handle is anywhere in this response.
        dumped = json.dumps(result.structured) + result.text
        others = [_session_id(other) for other in range(TOTAL) if other != index]
        assert not [handle for handle in others if handle in dumped]
        # Every handle was supplied, so nothing was minted and no mint-back
        # text block exists to name one.
        assert MINT_BACK_HEADER_SESSION not in result.text

    # ── what AgentCat published ──────────────────────────────────────────────
    events = _call_events(capture)
    assert len(events) == TOTAL
    for event in events:
        index = int(event.parameters["arguments"]["text"][1:])
        assert event.session_id == _session_id(index)
        assert event.parameters["arguments"][SESSION_ID_PARAM] == _session_id(index)
        assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "supplied"
    assert len({event.session_id for event in events}) == TOTAL


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_simultaneous_calls_each_mint_their_own_handle(flavor, capture):
    """The same window with nothing supplied: 25 distinct minted handles.

    The supplied case above can only catch a resolution that leaked between
    calls. This one also catches a MINT that did: every call mints, and the
    handle each agent is told has to be the one its own event carries.
    """
    barrier = Barrier(TOTAL)
    built = flavor.build("concurrency-mint", hook=barrier.wait)
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    flavor.call(client, "echo", {"text": f"t{index:02d}"})
                    for index in range(TOTAL)
                )
            ),
            timeout=60,
        )

    assert barrier.peak == TOTAL
    events = _call_events(capture)
    assert len(events) == TOTAL

    published = {
        event.parameters["arguments"]["text"]: event.session_id for event in events
    }
    assert len(set(published.values())) == TOTAL, "two calls minted one handle"
    for index, result in enumerate(results):
        minted = result.structured[MCP_INSTRUCTIONS_KEY][SESSION_ID_PARAM]
        assert minted.startswith("ses_")
        # The handle in the mint-back text and the one in the mirror are the
        # same object of trust the agent echoes back, and the event has to be
        # keyed on it.
        assert f"session_id={minted} " in result.text
        assert published[f"t{index:02d}"] == minted
