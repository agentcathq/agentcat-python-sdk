"""Registry rebuild-on-demand, and what happens when it cannot happen.

A 2026 factory builds a fresh server per request, so a `tools/call` routinely
lands on an instance that has never served a `tools/list` — and the strip
registries only exist because a listing built them. The engine answers by
rebuilding them from the adapter's own list source (changelog §6.3); the
injection pipeline is deterministic, so a rebuilt registry matches what any
listing instance advertised.

`tests/test_callpath.py` pins that contract at the unit level. This module
pins it per flavor, end to end, on the two things only a real server can show:
that the CUSTOMER's tool body receives the stripped arguments, and that the
structured mirror is gated on the rebuilt output registry.

The failure path is the other half. When the list source is down there is
nothing to rebuild from, and the engine falls back to the heuristic strip —
which must still keep `get_more_tools`' own `context`, because on that one
tool `context` is a real parameter rather than something AgentCat injected.
"""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import (
    AGENTCAT_TAG_SESSION_SOURCE,
    CONTEXT_PARAM,
    GET_MORE_TOOLS_NAME,
    MCP_INSTRUCTIONS_KEY,
    SESSION_ID_PARAM,
)

from .test_utils import sid
from .test_utils.flavors import flavors, tracking_data

SUPPLIED = sid("supplied")


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


def _logged(log_sink, fragment: str) -> bool:
    return any(fragment in line for line in log_sink)


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_a_call_before_any_listing_rebuilds_the_registries(
    flavor, capture, log_sink
):
    """No listing has ever run, and the call still behaves as if one had.

    Three things follow from the rebuild, and all three are what a customer
    would notice if it stopped happening: their tool body sees only its own
    arguments, the event records the call as the agent made it, and the mirror
    lands because the rebuilt output registry says the schema declares it.
    """
    built = flavor.build("rebuild")
    track(built.server, "proj_test", AgentCatOptions())
    data = tracking_data(built.server)
    assert data.injected_params_registry is None, "something listed before the call"

    result = await flavor.call_unlisted(
        built.server,
        "echo",
        {"text": "hi", SESSION_ID_PARAM: SUPPLIED, CONTEXT_PARAM: "why I called"},
    )

    assert _logged(log_sink, "Rebuilt injection registries on demand")
    # What the customer's tool layer was actually HANDED — read at the tool
    # manager on the facade flavors, whose typed bodies cannot report an
    # argument the SDK silently dropped on the way in, and from the raw
    # argument dict on the lowlevel ones. Either way an injected parameter that
    # survived the strip shows up here.
    assert built.seen == [("echo", {"text": "hi"})]
    # Rebuilt from the adapter's list source, and stored for the next call.
    assert data.injected_params_registry is not None
    assert data.injected_params_registry["echo"] == {SESSION_ID_PARAM, CONTEXT_PARAM}
    # get_more_tools is in the rebuilt view too — that view is also what
    # settles whether AgentCat may advertise it at all.
    assert data.injected_params_registry[GET_MORE_TOOLS_NAME] == {SESSION_ID_PARAM}
    # The mirror is gated on the rebuilt OUTPUT registry, and `echo` is in it.
    assert data.output_injection_registry is not None
    assert "echo" in data.output_injection_registry
    assert result.structured[MCP_INSTRUCTIONS_KEY][SESSION_ID_PARAM] == SUPPLIED

    event = capture[0]
    assert event.session_id == SUPPLIED
    # Raw, unstripped: the event records the call as the agent made it.
    assert event.parameters["arguments"][CONTEXT_PARAM] == "why I called"
    assert event.user_intent == "why I called"


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_the_rebuild_recovers_a_customers_own_session_id_ownership(
    flavor, capture
):
    """Ownership is rebuilt too, not just the strip registry.

    This is the stateless-HTTP shape: on mcp 2.x every request can reach a
    fresh server instance, so the listing that recorded the collision is
    routinely NOT the instance serving the call. Without the rebuild
    populating `declared_session_params`, that instance would read the
    customer's value, find it malformed and tag the call `invalid` — the same
    sessionless outcome, but attributed to the agent rather than to the
    collision, which is the difference between a dashboard that explains the
    gap and one that does not.
    """
    built = flavor.build("rebuild-owned", customer_session_id=True)
    track(built.server, "proj_test", AgentCatOptions())
    data = tracking_data(built.server)
    assert data.declared_session_params == set(), "something listed before the call"

    await flavor.call_unlisted(
        built.server, "complete_task", {"session_id": "TICKET-9", "note": "done"}
    )

    assert data.declared_session_params == {"complete_task"}
    event = capture[0]
    assert event.session_id is None
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "foreign"


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_a_failed_rebuild_degrades_to_the_heuristic_strip(
    flavor, capture, log_sink
):
    """The list source is down, so the strip has no registry to consult.

    What every era owes on this path, whatever the call then does: fall back
    rather than raise, leave the (already absent) parameter registry alone so a
    concurrent call that just stored a good one is not robbed of it, and clear
    the output registry so the mirror stops gating on knowledge we no longer
    have. And still publish the one event, with the call as the agent made it.
    """
    built = flavor.build("rebuild-down-any", customer_get_more_tools=True)
    track(built.server, "proj_test", AgentCatOptions())
    flavor.break_list_source(built.server)
    data = tracking_data(built.server)

    await flavor.call_unlisted(
        built.server,
        GET_MORE_TOOLS_NAME,
        {CONTEXT_PARAM: "I need a tool to send email", SESSION_ID_PARAM: SUPPLIED},
    )

    assert _logged(log_sink, "rebuild-on-demand failed")
    assert data.injected_params_registry is None
    assert data.output_injection_registry is None
    assert len(capture) == 1
    event = capture[0]
    assert event.session_id == SUPPLIED
    # Raw arguments: both injected names are still on the event.
    assert event.parameters["arguments"][SESSION_ID_PARAM] == SUPPLIED
    assert event.parameters["arguments"][CONTEXT_PARAM] == "I need a tool to send email"


# The mcp 1.x official flavors cannot answer a call at all while their list
# source is down — see `Flavor.survives_a_down_list_source` for why, and note
# that it is the SDK's behavior rather than AgentCat's. Everything AgentCat
# does on that path is asserted above; what the CUSTOMER's tool sees can only
# be asked of an era where the call completes.
CAN_ANSWER_WITH_A_DOWN_LIST_SOURCE = [
    flavor for flavor in flavors() if flavor.survives_a_down_list_source
]


@pytest.mark.parametrize(
    "flavor", CAN_ANSWER_WITH_A_DOWN_LIST_SOURCE, ids=lambda f: f.id
)
async def test_a_failed_rebuild_still_protects_get_more_tools_own_context(
    flavor, capture, log_sink
):
    """The heuristic strips all three injected names — except one.

    `get_more_tools`' `context` is a real parameter the tool needs, not
    something AgentCat injected, and a customer tool by that name is what makes
    the distinction observable: strip its `context` and the call fails or
    answers about nothing.

    And the mirror applies anyway. A cleared output registry means "no schema
    we know about can be in play", not "no schema declares the field", so
    gating on it would silently drop the mint-back on exactly the instances
    that never listed.
    """
    built = flavor.build("rebuild-down", customer_get_more_tools=True)
    track(built.server, "proj_test", AgentCatOptions())
    flavor.break_list_source(built.server)
    data = tracking_data(built.server)

    result = await flavor.call_unlisted(
        built.server,
        GET_MORE_TOOLS_NAME,
        {CONTEXT_PARAM: "I need a tool to send email", SESSION_ID_PARAM: SUPPLIED},
    )

    # The rebuild really did fail — otherwise the registry strip would keep
    # `context` for its own reasons and this would prove nothing.
    assert _logged(log_sink, "rebuild-on-demand failed")
    assert data.injected_params_registry is None
    assert data.output_injection_registry is None

    # `context` reached the customer's tool; `session_id` did not.
    assert built.seen == [
        (GET_MORE_TOOLS_NAME, {CONTEXT_PARAM: "I need a tool to send email"})
    ]
    assert "customer answered: I need a tool to send email" in result.text
    # ...and the handle still rides back with no registry to gate it.
    assert result.structured[MCP_INSTRUCTIONS_KEY][SESSION_ID_PARAM] == SUPPLIED
    assert capture[0].session_id == SUPPLIED


@pytest.mark.parametrize(
    "flavor", CAN_ANSWER_WITH_A_DOWN_LIST_SOURCE, ids=lambda f: f.id
)
async def test_a_failed_rebuild_still_strips_context_from_every_other_tool(
    flavor, capture, log_sink
):
    """The other half of the heuristic: `context` is ours on every tool but one.

    Same broken instance, an ordinary tool — and `echo` never declared a
    `context` parameter, so handing it one is the injection leaking through.
    """
    built = flavor.build("rebuild-down-echo")
    track(built.server, "proj_test", AgentCatOptions())
    flavor.break_list_source(built.server)

    await flavor.call_unlisted(
        built.server,
        "echo",
        {"text": "hi", SESSION_ID_PARAM: SUPPLIED, CONTEXT_PARAM: "why I called"},
    )

    assert _logged(log_sink, "rebuild-on-demand failed")
    assert built.seen == [("echo", {"text": "hi"})]
    assert capture[0].parameters["arguments"][CONTEXT_PARAM] == "why I called"
