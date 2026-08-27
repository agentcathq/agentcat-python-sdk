"""The agent handle over real Streamable HTTP (official MCP SDK 1.x).

`enable_agent_tracking` is off by default, so every OTHER e2e module in this
tree runs with `agent_id` never injected. This module turns it on for its own
server — the fixture reads `AGENTCAT_OPTIONS_FACTORY` per module — and it is a
separate file rather than a flag flipped on a shared one because `agent_id` is
injected as REQUIRED, which changes the schema every sibling test calls against.

The strip is read at the TOOL MANAGER (`tests.test_utils.delivery`), never from
the tool's own result: this SDK's manager DROPS an argument the signature does
not name without complaint, so a test that sends `agent_id` and asserts "no
error" passes identically with the strip disabled.

Structured output is gated: `Tool.outputSchema` and `structuredContent` arrive
in mcp 1.10, and this tree runs as far back as 1.9.2. Below it AgentCat mirrors
nothing, because there is no field to mirror into.
"""

from __future__ import annotations

import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agentcat import AgentCatOptions
from agentcat.modules.constants import (
    AGENT_ID_PARAM,
    AGENTCAT_TAG_AGENT_ID,
    AGENTCAT_TAG_AGENT_SOURCE,
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_SESSION_KEY,
    SESSION_ID_PARAM,
)
from tests.test_utils import NEEDS_STRUCTURED_OUTPUT
from tests.test_utils.delivery import delivered_arguments_for

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
async def test_the_agent_handle_survives_the_wire(official_http_server, capture_queue):
    """Listing with agent tracking on: the schema the agent is handed.

    Property order is the contract (`modules/injection.py` §"Resulting property
    order"), and both handles are required — `session_id` names `start` as its
    explicit first-call value, and a call that omits either is still served.
    """
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            listed = await client.list_tools()

    add = next(t for t in listed.tools if t.name == "add_todo")
    assert list(add.inputSchema["properties"])[-3:] == [
        SESSION_ID_PARAM,
        AGENT_ID_PARAM,
        "context",
    ]
    assert AGENT_ID_PARAM in add.inputSchema["required"]
    assert SESSION_ID_PARAM in add.inputSchema["required"]
    assert "pattern" in add.inputSchema["properties"][SESSION_ID_PARAM]


@pytest.mark.asyncio
async def test_a_supplied_agent_handle_tags_the_event(
    official_http_server, capture_queue
):
    """The handle rides the event as a tag and never reaches the tool."""
    url, server = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool(
                "add_todo",
                {"text": "with agent", AGENT_ID_PARAM: AGENT, "context": "why"},
            )
            assert result.isError is False, _text(result)
            assert MINT_BACK_HEADER in _text(result)

    # The tool layer never saw the handles — the only observation on this shape
    # that a broken strip would fail. The server runs in-process (uvicorn in a
    # thread), so its recorder is readable straight off the fixture's object.
    assert delivered_arguments_for(server, "add_todo")[-1] == {"text": "with agent"}

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert event.tags[AGENTCAT_TAG_AGENT_ID] == AGENT
    assert event.tags[AGENTCAT_TAG_AGENT_SOURCE] == "supplied"
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"
    # The event records the call as the agent made it: handles included.
    assert event.parameters["arguments"][AGENT_ID_PARAM] == AGENT


@pytest.mark.asyncio
async def test_both_handles_echo_across_calls(official_http_server, capture_queue):
    """The agent echoes session and agent handle together on the next call.

    The two are independent: the session is confirmed rather than re-minted,
    while `agent_id` is `supplied` on both calls — the server never issues one,
    so it has no `minted` state to pass through.
    """
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
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
            assert second.isError is False, _text(second)
            assert MINT_BACK_HEADER not in _text(second)

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
    official_http_server, capture_queue
):
    """`required` is advisory: nothing enforces it, and nothing may break.

    The injected schema is what `tools/list` advertises, but AgentCat strips the
    handles at the request-handler seam BEFORE any argument validation the tool
    layer would do, so an agent that ignores the `required` marker is served
    normally. The event is then simply agent-less: an absent handle must never
    become an empty or invented tag, because a customer filtering on
    `agentcat_agent_id` has to be able to tell "no agent told us" from "this
    agent".
    """
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool("add_todo", {"text": "no agent"})
            assert result.isError is False, _text(result)
            assert MINT_BACK_HEADER in _text(result)

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert AGENTCAT_TAG_AGENT_ID not in event.tags
    assert AGENTCAT_TAG_AGENT_SOURCE not in event.tags
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"


@pytest.mark.asyncio
async def test_a_blank_agent_handle_is_a_miss_not_an_empty_tag(
    official_http_server, capture_queue
):
    """`extract_handle` trims and rejects, over the wire.

    A whitespace-only value must leave the event with NO agent tags — an
    `agentcat_agent_id: ""` would key every such call to one phantom agent
    downstream, which is worse than the absence it stands in for.
    """
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool(
                "add_todo", {"text": "blank", AGENT_ID_PARAM: "   ", "context": "x"}
            )
            assert result.isError is False, _text(result)

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert AGENTCAT_TAG_AGENT_ID not in event.tags
    assert AGENTCAT_TAG_AGENT_SOURCE not in event.tags
    # The session handle is unaffected: suppression is per handle.
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"


@NEEDS_STRUCTURED_OUTPUT
@pytest.mark.asyncio
async def test_both_handles_are_mirrored_into_structured_content(
    official_http_server, capture_queue
):
    """The agent can re-read either handle mid-session, over the wire.

    Unlike the mint-back text (announcements only), the structured mirror is
    present on every response — and it names BOTH handles, because suppression
    is per handle rather than per response.
    """
    url, _ = official_http_server
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            listed = await client.list_tools()
            add = next(t for t in listed.tools if t.name == "add_todo")
            assert MCP_SESSION_KEY in add.outputSchema["properties"]

            result = await client.call_tool(
                "add_todo", {"text": "mirrored", AGENT_ID_PARAM: AGENT}
            )
            mirror = result.structuredContent[MCP_SESSION_KEY]
            assert mirror[SESSION_ID_PARAM].startswith("ses_")
            assert mirror[AGENT_ID_PARAM] == AGENT
            # The customer's own structured payload survives untouched.
            assert result.structuredContent["result"].startswith("Added todo")

    time.sleep(0.5)
    assert _call_events(capture_queue)[-1].session_id == mirror[SESSION_ID_PARAM]
