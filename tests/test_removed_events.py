"""The three retired event types, proven gone on every flavor.

v1 published `mcp:initialize`, `mcp:tools/list` and `agentcat:identify` beside
the tool call. v2 publishes ONE event type automatically — `mcp:tools/call` —
and `tools/list` is still intercepted, for schema injection only
(changelog §3.1). That is a wire-visible promise to every consumer of the
event stream, and it is made by four different adapters, so it is asserted
against a full lifecycle on each of them rather than inferred from the shared
engine.

The lifecycle is deliberately the noisy one: connect (which handshakes, and on
the 2026 wire discovers), list, call twice, call the tool AgentCat itself
answers, and fail a call. Every one of those was an event in v1.
"""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import GET_MORE_TOOLS_NAME, SESSION_ID_PARAM

from .test_utils.flavors import BOOM_TEXT, flavors

# What v1 published beside the tool call. Named rather than implied, so a
# regression reads as "initialize is back" instead of "a set changed".
RETIRED = {"mcp:initialize", "mcp:tools/list", "agentcat:identify"}


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_a_full_lifecycle_publishes_only_tool_calls(flavor, capture):
    """Handshake, listing and four calls; four events, all of one type."""
    built = flavor.build("removed-events")
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        listed = await flavor.list_tools(client)
        assert {tool.name for tool in listed} == {"echo", GET_MORE_TOOLS_NAME}

        first = await flavor.call(client, "echo", {"text": "one"})
        # The mint line is "session_id: <id>", on its own line at the START
        # of the result text (the block is the first content element).
        minted = first.text.split("session_id: ")[1].split("\n")[0]
        await flavor.call(client, "echo", {"text": "two", SESSION_ID_PARAM: minted})
        # AgentCat answers this one itself, and it is still just a tool call.
        await flavor.call(
            client, GET_MORE_TOOLS_NAME, {"context": "I need a tool to send email"}
        )

    assert {event.event_type for event in capture} == {"mcp:tools/call"}
    assert len(capture) == 3
    assert not RETIRED & {event.event_type for event in capture}
    assert [event.resource_name for event in capture] == [
        "echo",
        "echo",
        GET_MORE_TOOLS_NAME,
    ]


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_a_failed_call_publishes_only_its_own_tool_call(flavor, capture):
    """The error path is where an extra event would be easiest to add back.

    The tool body refuses the sentinel text, so the failure is a real Python
    exception on every era — surfaced as a raise on some and as an `is_error`
    result on others — and every era still owes exactly one event for it.
    """
    built = flavor.build("removed-events-error")
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        try:
            await flavor.call(client, "echo", {"text": BOOM_TEXT})
        except Exception:
            # Community FastMCP surfaces a failing call as a raised error at
            # the client; the official SDKs answer with an `is_error` result.
            # Which one it is belongs to the SDK, not to this assertion.
            pass

    assert {event.event_type for event in capture} == {"mcp:tools/call"}
    assert len(capture) == 1
    assert capture[0].is_error is True
    assert capture[0].error is not None
