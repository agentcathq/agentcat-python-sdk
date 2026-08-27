"""`MCPServer` over real Streamable HTTP.

`MCPServer` is tracked through its `_lowlevel_server`, so this is the same
adapter the bare-`Server` file exercises — what is new here is the whole
higher-level stack on top of it: the tool manager's generated schemas, its
argument delivery, and its structured-output conversion.

That manager does NOT reject a parameter AgentCat failed to strip — measured on
mcp 2.0, it drops an undeclared argument silently — so the strip is asserted
against what the manager was handed (`tests.test_utils.delivery`), not against
the call merely succeeding.
"""

from __future__ import annotations

import time

import pytest
from mcp.client import Client

from agentcat.modules.constants import (
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_SESSION_KEY,
)
from tests.test_utils.delivery import delivered_arguments_for
from tests.test_utils.modern_server import create_mcpserver_todo_server

from ...test_utils import sid

pytestmark = pytest.mark.e2e

SERVER_FACTORY = create_mcpserver_todo_server

MINT_BACK_HEADER = (
    "[session_id issued — see this tool's session_id parameter description]"  # noqa: E501
)


def _call_events(capture_queue):
    return [e for e in capture_queue if e.event_type == "mcp:tools/call"]


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


@pytest.mark.asyncio
async def test_mcpserver_injects_strips_and_publishes(
    modern_http_server, capture_queue
):
    url, server = modern_http_server
    async with Client(url) as client:
        listed = await client.list_tools()
        add = next(t for t in listed.tools if t.name == "add_todo")
        assert list(add.input_schema["properties"])[-2:] == ["session_id", "context"]
        assert MCP_SESSION_KEY in add.output_schema["properties"]

        result = await client.call_tool(
            "add_todo",
            {"text": "over http", "session_id": "start", "context": "why"},
        )
        assert result.is_error is False, _text(result)
        text = _text(result)
        assert MINT_BACK_HEADER in text
        minted = text.split("session_id: ")[1].split("\n")[0]
        assert result.structured_content[MCP_SESSION_KEY]["session_id"] == minted
        assert result.structured_content["result"].startswith("Added todo")

    # The tool layer never saw `context` — the only observation on this shape
    # that a broken strip would fail. The server runs in-process (uvicorn in a
    # thread), so its recorder is readable straight off the fixture's object.
    assert delivered_arguments_for(server, "add_todo") == [{"text": "over http"}]

    time.sleep(0.5)
    events = _call_events(capture_queue)
    assert [e.resource_name for e in events] == ["add_todo"]
    assert events[0].session_id == minted
    assert events[0].tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"
    # The event carries the RAW arguments — the sentinel as the agent sent
    # it — and the UNDECORATED response.
    assert events[0].parameters["arguments"] == {
        "text": "over http",
        "session_id": "start",
        "context": "why",
    }
    assert MCP_SESSION_KEY not in (events[0].response or {}).get(
        "structuredContent", {}
    )
    assert MCP_SESSION_KEY not in (events[0].response or {}).get(
        "structured_content", {}
    )


@pytest.mark.asyncio
async def test_mcpserver_echoed_handle_is_not_minted_again(
    modern_http_server, capture_queue
):
    url, _ = modern_http_server
    async with Client(url) as client:
        result = await client.call_tool(
            "list_todos", {"session_id": sid("supplied_over_http")}
        )
        assert result.is_error is False, _text(result)
        assert MINT_BACK_HEADER not in _text(result)

    time.sleep(0.5)
    event = _call_events(capture_queue)[-1]
    assert event.session_id == sid("supplied_over_http")
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "supplied"
