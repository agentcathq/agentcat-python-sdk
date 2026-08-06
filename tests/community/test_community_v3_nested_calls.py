"""Server-internal (nested) traffic on the community adapter.

A customer tool can drive its own server mid-call — fastmcp's code mode does
it on every `execute`: `CatalogTransform.get_tool_catalog` performs a nested
`tools/list` with `run_middleware=True`, and the sandbox's `call_tool` a
nested `tools/call`. This file pins the re-entrancy contract those paths get:
the nested listing is served verbatim (no injection, no registry rebuild — the
rebuild is what clobbered the agent-facing registries and made the agent's
echoed `session_id` fail FastMCP validation), and the nested call joins the
enclosing session, is tagged `agentcat_nested`, and is never decorated.

The server shape comes from `tests.test_utils.community_catalog_server`: the
code-mode mechanism reproduced without pydantic-monty, with an `observed` dict
recording what the "sandbox" side actually saw.
"""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import (
    AGENTCAT_TAG_NESTED,
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_INSTRUCTIONS_KEY,
)

from ..test_utils.community_catalog_server import (
    BOOM_TEXT,
    HAS_CATALOG_TRANSFORM,
    create_catalog_meta_server,
)
from ..test_utils.community_client import (
    HAS_COMMUNITY_CLIENT,
    create_community_test_client,
)
from ..test_utils.community_todo_server import (
    HAS_COMMUNITY_FASTMCP,
    create_community_todo_server,
)
from ..test_utils.flavors import tracking_data

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


def _minted_from(result) -> str:
    minted = _text(result).split("session_id=")[1].split(" ")[0]
    assert minted.startswith("ses_")
    return minted


async def test_nested_listing_leaves_the_registries_alone():
    """The clobber itself: a mid-call catalog fetch must not replace the
    registries the agent-facing listing built."""
    observed: dict = {}
    server = create_catalog_meta_server(observed)
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        await client.list_tools()
        data = tracking_data(server)
        assert set(data.injected_params_registry) == {"run"}

        await client.call_tool("run", {"program": "first", "context": CONTEXT})

    # The nested get_tool_catalog listing DID happen...
    assert set(observed["catalog"]) == {"echo", "compose", "get_more_tools"}
    # ...and the agent-facing registries survived it untouched.
    assert set(data.injected_params_registry) == {"run"}
    assert set(data.output_injection_registry) == {"run"}
    assert data.declared_session_params == set()


async def test_the_echoed_session_id_still_strips_after_a_nested_call(capture):
    """THE repro: list → call → echo the minted handle on the next call.

    Before the fix, the nested catalog fetch replaced the registry with the
    raw backend catalog, nothing stripped `session_id`/`context` off the
    second call, and FastMCP failed it with "Unexpected keyword argument".
    """
    observed: dict = {}
    server = create_catalog_meta_server(observed)
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        (run_tool,) = [t for t in listed if t.name == "run"]
        assert {"session_id", "context"} <= set(run_tool.inputSchema["properties"])

        r1 = await client.call_tool("run", {"program": "first", "context": CONTEXT})
        minted = _minted_from(r1)

        # A raise here is the regression: the strip must consume both params.
        await client.call_tool(
            "run", {"program": "second", "session_id": minted, "context": CONTEXT}
        )

    # The bodies received exactly their own arguments (community FastMCP would
    # have raised on an unstripped extra, but pin the delivered shape anyway).
    assert ("run", {"program": "second"}) in observed["delivered"]
    outer = [e for e in _call_events(capture) if e.resource_name == "run"]
    assert [e.session_id for e in outer] == [minted, minted]
    assert outer[1].tags[AGENTCAT_TAG_SESSION_SOURCE] == "supplied"


async def test_a_nested_call_joins_the_session_and_is_never_decorated(capture):
    observed: dict = {}
    server = create_catalog_meta_server(observed)
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        await client.list_tools()
        await client.call_tool("run", {"program": "hello", "context": CONTEXT})

    events = _call_events(capture)
    assert [e.resource_name for e in events] == ["echo", "run"]
    inner, outer = events

    # One logical call: the inner event rides the outer's session, keeps the
    # outer's provenance, and carries the nested marker; the outer does not.
    assert inner.session_id == outer.session_id
    assert inner.tags[AGENTCAT_TAG_NESTED] == "true"
    assert (
        inner.tags[AGENTCAT_TAG_SESSION_SOURCE]
        == outer.tags[AGENTCAT_TAG_SESSION_SOURCE]
        == "minted"
    )
    assert AGENTCAT_TAG_NESTED not in outer.tags

    # The inner result, as agent-authored code would consume it, is the
    # customer's data and nothing else: no mint-back block, no mirror key.
    assert observed["inner_text"] == "echo:hello"
    assert "[MCP INSTRUCTIONS]" not in observed["inner_text"]
    assert observed["inner_structured"] == {"result": "echo:hello"}


async def test_the_outer_call_is_still_fully_decorated():
    """The registry survives the nested fetch, so the outer mint-back keeps
    both of its forms — the text block and the structured mirror (which the
    clobber used to silently drop)."""
    observed: dict = {}
    server = create_catalog_meta_server(observed)
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        await client.list_tools()
        result = await client.call_tool("run", {"program": "hello", "context": CONTEXT})

    assert MINT_BACK_HEADER in _text(result)
    mint = result.structured_content[MCP_INSTRUCTIONS_KEY]
    assert mint["session_id"] == _minted_from(result)
    assert result.structured_content["result"] == "ran:hello"


async def test_the_nested_listing_is_served_uninjected():
    """The sandbox-facing catalog is the customer's raw view: parameters a
    sandbox cannot echo must never appear in it."""
    observed: dict = {}
    server = create_catalog_meta_server(observed)
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        await client.list_tools()
        await client.call_tool("run", {"program": "hello", "context": CONTEXT})

    catalog = observed["catalog"]
    assert catalog["echo"] == ["text"]
    assert catalog["compose"] == ["text"]
    # get_more_tools' `context` is its own real parameter, not an injection.
    assert catalog["get_more_tools"] == ["context"]
    assert not any("session_id" in props for props in catalog.values())


async def test_another_server_is_untouched_by_this_servers_frame():
    """The frame is scoped by server identity: a listing on server B while a
    call on server A is in flight still injects and still writes B's
    registries."""
    observed: dict = {}
    other = create_community_todo_server()
    server = create_catalog_meta_server(observed, also_list=other.list_tools)
    track(server, "proj_test", AgentCatOptions())
    track(other, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        await client.list_tools()
        await client.call_tool("run", {"program": "hello", "context": CONTEXT})

    add_todo = next(t for t in observed["also_listed"] if t.name == "add_todo")
    assert {"session_id", "context"} <= set(add_todo.parameters["properties"])
    assert "add_todo" in tracking_data(other).injected_params_registry


async def test_nesting_of_nesting_shares_the_outermost_session(capture):
    observed: dict = {}
    server = create_catalog_meta_server(observed, target="compose")
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        await client.list_tools()
        await client.call_tool("run", {"program": "deep", "context": CONTEXT})

    events = _call_events(capture)
    assert [e.resource_name for e in events] == ["echo", "compose", "run"]
    assert len({e.session_id for e in events}) == 1
    assert [AGENTCAT_TAG_NESTED in e.tags for e in events] == [True, True, False]


async def test_a_failing_nested_call_publishes_and_surfaces(capture):
    """The inner failure is recorded as a nested error event, and the raise
    reaches the customer's tool body unchanged — analytics never rewrites the
    failure a composing tool sees."""
    observed: dict = {}
    server = create_catalog_meta_server(observed)
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        await client.list_tools()
        with pytest.raises(Exception, match="inner tool failed"):
            await client.call_tool("run", {"program": BOOM_TEXT, "context": CONTEXT})

    events = _call_events(capture)
    assert [e.resource_name for e in events] == ["echo", "run"]
    inner, outer = events
    assert inner.is_error and inner.tags[AGENTCAT_TAG_NESTED] == "true"
    assert inner.session_id == outer.session_id
    assert outer.is_error


async def test_tracing_off_still_shields_the_registry():
    """Context injection is independent of tracing, so the frame must cover
    the tracing-off path too: a nested fetch there would otherwise eat the
    registry entry that strips the next call's `context`."""
    observed: dict = {}
    server = create_catalog_meta_server(observed)
    track(server, "proj_test", AgentCatOptions(enable_tracing=False))

    async with create_community_test_client(server) as client:
        await client.list_tools()
        data = tracking_data(server)
        assert data.injected_params_registry["run"] == {"context"}

        await client.call_tool("run", {"program": "first", "context": CONTEXT})
        # The strip consumed `context` (a raise here would be the regression),
        # and the registry still carries the entry for the next call.
        await client.call_tool("run", {"program": "second", "context": CONTEXT})

    assert data.injected_params_registry["run"] == {"context"}
    assert ("run", {"program": "second"}) in observed["delivered"]


async def test_sibling_calls_under_one_context_are_not_nested(capture):
    """Restore-not-pop: a parent fastmcp Context can outlive one call, and the
    sibling that follows must come up top-level — fresh session, no marker."""
    import fastmcp.server.context as fastmcp_context

    observed: dict = {}
    server = create_catalog_meta_server(observed)
    track(server, "proj_test", AgentCatOptions())

    await server.list_tools()
    async with fastmcp_context.Context(fastmcp=server):
        await server.call_tool("run", {"program": "one", "context": CONTEXT})
        await server.call_tool("run", {"program": "two", "context": CONTEXT})

    outer = [e for e in _call_events(capture) if e.resource_name == "run"]
    assert len(outer) == 2
    assert outer[0].session_id != outer[1].session_id
    assert all(AGENTCAT_TAG_NESTED not in e.tags for e in outer)
    # Each run's inner echo still joined its OWN outer call's session.
    inner = [e for e in _call_events(capture) if e.resource_name == "echo"]
    assert [e.session_id for e in inner] == [e.session_id for e in outer]
