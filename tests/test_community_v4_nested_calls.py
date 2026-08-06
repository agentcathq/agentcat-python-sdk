"""Server-internal (nested) traffic on FastMCP 4.

The v4 sibling of `tests/community/test_community_v3_nested_calls.py` — it
does not re-prove the v3 contract, only that the re-entrancy frame holds on
the 4.x line, whose `Context.__aenter__` shares `_request_state` the same way
but whose dispatch differs (second pass, default dereferencing middleware).
Covers the breakage repro, the nested-call session join, and the outer
decoration surviving a nested catalog fetch.
"""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import (
    AGENTCAT_TAG_NESTED,
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_INSTRUCTIONS_KEY,
)

from .test_utils.community_catalog_server import (
    HAS_CATALOG_TRANSFORM,
    create_catalog_meta_server,
)
from .test_utils.community_client import (
    HAS_COMMUNITY_CLIENT,
    create_community_test_client,
)
from .test_utils.community_todo_server import (
    HAS_COMMUNITY_FASTMCP,
)

pytestmark = pytest.mark.skipif(
    not (HAS_COMMUNITY_FASTMCP and HAS_COMMUNITY_CLIENT and HAS_CATALOG_TRANSFORM),
    reason="Community FastMCP with CatalogTransform not available",
)

MINT_BACK_HEADER = "[MCP INSTRUCTIONS]: session_id issued."
CONTEXT = "Driving the hidden catalog through the meta tool to exercise nesting"


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


def _text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


def _call_events(capture) -> list:
    return [e for e in capture if e.event_type == "mcp:tools/call"]


async def test_the_echoed_session_id_still_strips_after_a_nested_call(capture):
    observed: dict = {}
    server = create_catalog_meta_server(observed)
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        (run_tool,) = [t for t in listed if t.name == "run"]
        assert {"session_id", "context"} <= set(run_tool.input_schema["properties"])

        r1 = await client.call_tool("run", {"program": "first", "context": CONTEXT})
        minted = _text(r1).split("session_id=")[1].split(" ")[0]
        assert minted.startswith("ses_")

        # A raise here is the regression: the nested catalog fetch inside call
        # one must not have clobbered the registry that strips these.
        await client.call_tool(
            "run", {"program": "second", "session_id": minted, "context": CONTEXT}
        )

    outer = [e for e in _call_events(capture) if e.resource_name == "run"]
    assert [e.session_id for e in outer] == [minted, minted]
    assert outer[1].tags[AGENTCAT_TAG_SESSION_SOURCE] == "supplied"


async def test_a_nested_call_joins_the_session_and_is_never_decorated(capture):
    observed: dict = {}
    server = create_catalog_meta_server(observed)
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        await client.list_tools()
        result = await client.call_tool("run", {"program": "hello", "context": CONTEXT})

    events = _call_events(capture)
    assert [e.resource_name for e in events] == ["echo", "run"]
    inner, outer = events
    assert inner.session_id == outer.session_id
    assert inner.tags[AGENTCAT_TAG_NESTED] == "true"
    assert AGENTCAT_TAG_NESTED not in outer.tags

    # The sandbox-side view is the customer's data and nothing else...
    assert observed["inner_text"] == "echo:hello"
    assert observed["inner_structured"] == {"result": "echo:hello"}
    assert not any(
        "session_id" in props for props in observed["catalog"].values()
    )
    # ...while the outer wire result keeps both mint-back forms.
    assert MINT_BACK_HEADER in _text(result)
    mint = result.structured_content[MCP_INSTRUCTIONS_KEY]
    assert mint["session_id"] == outer.session_id
