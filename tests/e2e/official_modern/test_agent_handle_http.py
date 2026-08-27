"""The agent handle over real Streamable HTTP (modern official SDK).

`enable_agent_tracking` is off by default, so every OTHER e2e module in this
tree runs with `agent_id` never injected at all. This module turns it on for
its own server — the fixture reads `AGENTCAT_OPTIONS_FACTORY` per module — and
it is deliberately a separate file rather than an option flipped on a shared
one: `agent_id` is injected as REQUIRED, which changes the listed schema every
sibling test calls against.

What only a socket proves here is the listing. The SDK validates every outbound
spec result against the negotiated protocol surface, so an injected schema that
is malformed once `agent_id` joins `session_id` and `context` fails server-side
rather than reaching the agent. In-process clients on this shape skip that pass.

The strip needs no recorder on this shape: `create_lowlevel_todo_server`'s
handler calls `_reject_unexpected(arguments, {"text"})`, so a handle that
survived to the tool body fails the call outright.
"""

from __future__ import annotations

import time

import pytest
from mcp.client import Client

from agentcat import AgentCatOptions
from agentcat.modules.constants import (
    AGENT_ID_PARAM,
    AGENTCAT_TAG_AGENT_ID,
    AGENTCAT_TAG_AGENT_SOURCE,
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_SESSION_KEY,
    SESSION_ID_PARAM,
)

pytestmark = pytest.mark.e2e

MINT_BACK_HEADER = (
    "[session_id issued — see this tool's session_id parameter description]"  # noqa: E501
)
AGENT = "opus-4.80-1m|claude-code|k3n9x"


def _agent_tracking_options() -> AgentCatOptions:
    return AgentCatOptions(enable_tracing=True, enable_agent_tracking=True)


AGENTCAT_OPTIONS_FACTORY = _agent_tracking_options


def _call_events(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"]


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


async def test_the_agent_handle_survives_the_wire(modern_http_server, capture_queue):
    """Listing with agent tracking on: the schema the agent is handed.

    Property order is the contract (`modules/injection.py` §"Resulting property
    order"), and both handles are required — `session_id` names `start` as its
    explicit first-call value, and a call that omits either is still served.
    """
    url, _ = modern_http_server
    async with Client(url) as client:
        listed = await client.list_tools()

    add = next(t for t in listed.tools if t.name == "add_todo")
    assert list(add.input_schema["properties"]) == [
        "text",
        SESSION_ID_PARAM,
        AGENT_ID_PARAM,
        "context",
    ]
    assert AGENT_ID_PARAM in add.input_schema["required"]
    assert SESSION_ID_PARAM in add.input_schema["required"]
    assert "pattern" in add.input_schema["properties"][SESSION_ID_PARAM]
    assert MCP_SESSION_KEY in add.output_schema["properties"]


async def test_a_supplied_agent_handle_tags_the_event(
    modern_http_server, capture_queue
):
    """The handle rides the event as a tag and never reaches the tool."""
    url, _ = modern_http_server
    async with Client(url) as client:
        result = await client.call_tool(
            "add_todo",
            {"text": "with agent", AGENT_ID_PARAM: AGENT, "context": "why"},
        )
        # The handler rejects any argument but `text`, so a surviving handle
        # fails here rather than passing silently.
        assert result.is_error is False, _text(result)
        text = _text(result)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id: ")[1].split("\n")[0]
        # Both handles are mirrored, so an agent can re-read either mid-session.
        mirror = result.structured_content[MCP_SESSION_KEY]
        assert mirror[SESSION_ID_PARAM] == minted
        assert mirror[AGENT_ID_PARAM] == AGENT

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert event.tags[AGENTCAT_TAG_AGENT_ID] == AGENT
    assert event.tags[AGENTCAT_TAG_AGENT_SOURCE] == "supplied"
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"
    # The event records the call as the agent made it: handles included.
    assert event.parameters["arguments"][AGENT_ID_PARAM] == AGENT


async def test_both_handles_echo_across_calls(modern_http_server, capture_queue):
    """The agent echoes session and agent handle together on the next call.

    The two are independent: the session is confirmed rather than re-minted,
    while `agent_id` is `supplied` on both calls — it is never issued by the
    server, so it has no `minted` state to pass through.
    """
    url, _ = modern_http_server
    async with Client(url) as client:
        first = await client.call_tool(
            "add_todo",
            {
                "text": "one",
                SESSION_ID_PARAM: "start",
                AGENT_ID_PARAM: AGENT,
                "context": "start",
            },
        )
        minted = _text(first).split("session_id: ")[1].split("\n")[0]

        second = await client.call_tool(
            "add_todo",
            {"text": "two", SESSION_ID_PARAM: minted, AGENT_ID_PARAM: AGENT},
        )
        assert second.is_error is False, _text(second)
        assert MINT_BACK_HEADER not in _text(second)
        assert second.structured_content[MCP_SESSION_KEY][AGENT_ID_PARAM] == AGENT

    time.sleep(0.5)
    events = _call_events(capture_queue)[-2:]
    assert [e.session_id for e in events] == [minted, minted]
    assert [e.tags[AGENTCAT_TAG_SESSION_SOURCE] for e in events] == [
        "minted",
        "supplied",
    ]
    assert [e.tags[AGENTCAT_TAG_AGENT_ID] for e in events] == [AGENT, AGENT]
    assert [e.tags[AGENTCAT_TAG_AGENT_SOURCE] for e in events] == [
        "supplied",
        "supplied",
    ]


async def test_omitting_the_required_agent_handle_degrades_to_absence(
    modern_http_server, capture_queue
):
    """`required` is advisory: nothing enforces it, and nothing may break.

    Measured on mcp 2.0 — the injected schema is what `tools/list` advertises,
    but AgentCat strips the handles at the request-handler seam BEFORE any
    argument validation the tool layer would do, so an agent that ignores the
    `required` marker is served normally. The event is then simply
    agent-less: an absent handle must never become an empty or invented tag,
    because a customer filtering on `agentcat_agent_id` has to be able to tell
    "no agent told us" from "this agent".

    The session handle is untouched by any of it and still mints.
    """
    url, _ = modern_http_server
    async with Client(url) as client:
        result = await client.call_tool("add_todo", {"text": "no agent"})
        assert result.is_error is False, _text(result)
        assert MINT_BACK_HEADER in _text(result)
        # Nothing to confirm, so the mirror names only the session.
        assert AGENT_ID_PARAM not in result.structured_content[MCP_SESSION_KEY]

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert AGENTCAT_TAG_AGENT_ID not in event.tags
    assert AGENTCAT_TAG_AGENT_SOURCE not in event.tags
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"


async def test_a_blank_agent_handle_is_a_miss_not_an_empty_tag(
    modern_http_server, capture_queue
):
    """`extract_handle` trims and rejects, over the wire.

    A whitespace-only value must leave the event with NO agent tags at all —
    an `agentcat_agent_id: ""` would key every such call to one phantom agent
    downstream, which is worse than the absence it stands in for.
    """
    url, _ = modern_http_server
    async with Client(url) as client:
        result = await client.call_tool(
            "add_todo", {"text": "blank", AGENT_ID_PARAM: "   ", "context": "x"}
        )
        assert result.is_error is False, _text(result)

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert AGENTCAT_TAG_AGENT_ID not in event.tags
    assert AGENTCAT_TAG_AGENT_SOURCE not in event.tags
    # The session handle is unaffected: suppression is per handle.
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"
