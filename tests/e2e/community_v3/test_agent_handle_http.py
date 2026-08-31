"""The agent handle over real Streamable HTTP (community FastMCP 3).

`enable_agent_tracking` is off by default, so every OTHER e2e module in this
tree runs with `agent_id` never injected. This module turns it on for its own
server — the fixture reads `AGENTCAT_OPTIONS_FACTORY` per module — and it is a
separate file rather than a flag flipped on a shared one because `agent_id` is
injected as REQUIRED, which changes the schema every sibling test calls against.

The strip needs no recorder here: the conftest's `add_todo(text: str)` is a
typed FastMCP tool, and community FastMCP — unlike both official tool managers
— raises on an argument the signature never declared. A handle that survived to
the tool body fails the call.

Every `fastmcp` import is inside a test body, as in the rest of this tree: a
module-scope import fails at COLLECTION on the no-fastmcp matrix legs, which no
conftest gate downstream of it can rescue.
"""

from __future__ import annotations

import time

import pytest

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


@pytest.mark.asyncio
async def test_the_agent_handle_survives_the_wire(v3_http_server, capture_queue):
    """Listing with agent tracking on: the schema the agent is handed.

    Property order is the contract (`modules/injection.py` §"Resulting property
    order"), and both handles are required — `session_id` names `start` as its
    explicit first-call value, and a call that omits either is still served.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        listed = await client.list_tools()

    add = next(t for t in listed if t.name == "add_todo")
    assert list(add.inputSchema["properties"])[-3:] == [
        SESSION_ID_PARAM,
        AGENT_ID_PARAM,
        "context",
    ]
    assert AGENT_ID_PARAM in add.inputSchema["required"]
    assert SESSION_ID_PARAM in add.inputSchema["required"]
    assert "pattern" in add.inputSchema["properties"][SESSION_ID_PARAM]
    assert MCP_SESSION_KEY in add.outputSchema["properties"]


@pytest.mark.asyncio
async def test_a_supplied_agent_handle_tags_the_event(v3_http_server, capture_queue):
    """The handle rides the event as a tag and never reaches the tool."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        result = await client.call_tool(
            "add_todo",
            {"text": "with agent", AGENT_ID_PARAM: AGENT, "context": "why"},
        )
        # The typed tool raises on any argument but `text`, so a surviving
        # handle fails here rather than passing silently.
        text = _text(result)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id: ")[1].split("\n")[0]
        mirror = result.structured_content[MCP_SESSION_KEY]
        assert mirror[SESSION_ID_PARAM] == minted
        assert mirror[AGENT_ID_PARAM] == AGENT

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert event.tags[AGENTCAT_TAG_AGENT_ID] == AGENT
    assert event.tags[AGENTCAT_TAG_AGENT_SOURCE] == "supplied"
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"
    assert event.parameters["arguments"][AGENT_ID_PARAM] == AGENT


@pytest.mark.asyncio
async def test_both_handles_echo_across_calls(v3_http_server, capture_queue):
    """The agent echoes session and agent handle together on the next call.

    The two are independent: the session is confirmed rather than re-minted,
    while `agent_id` is `supplied` on both calls — the server never issues one,
    so it has no `minted` state to pass through.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
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


@pytest.mark.asyncio
async def test_omitting_the_required_agent_handle_degrades_to_absence(
    v3_http_server, capture_queue
):
    """`required` is advisory: nothing enforces it, and nothing may break.

    AgentCat strips the handles in middleware, before the tool's own argument
    validation, so an agent that ignores the `required` marker is served
    normally. The event is then simply agent-less: an absent handle must never
    become an empty or invented tag, because a customer filtering on
    `agentcat_agent_id` has to be able to tell "no agent told us" from "this
    agent".
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        result = await client.call_tool("add_todo", {"text": "no agent"})
        assert MINT_BACK_HEADER in _text(result)
        assert AGENT_ID_PARAM not in result.structured_content[MCP_SESSION_KEY]

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert AGENTCAT_TAG_AGENT_ID not in event.tags
    assert AGENTCAT_TAG_AGENT_SOURCE not in event.tags
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"


@pytest.mark.asyncio
async def test_a_blank_agent_handle_is_a_miss_not_an_empty_tag(
    v3_http_server, capture_queue
):
    """`extract_handle` trims and rejects, over the wire.

    A whitespace-only value must leave the event with NO agent tags — an
    `agentcat_agent_id: ""` would key every such call to one phantom agent
    downstream, which is worse than the absence it stands in for.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url, _ = v3_http_server
    async with Client(StreamableHttpTransport(url)) as client:
        await client.call_tool(
            "add_todo", {"text": "blank", AGENT_ID_PARAM: "   ", "context": "x"}
        )

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert AGENTCAT_TAG_AGENT_ID not in event.tags
    assert AGENTCAT_TAG_AGENT_SOURCE not in event.tags
    # The session handle is unaffected: suppression is per handle.
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"
