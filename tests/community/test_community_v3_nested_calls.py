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
    MCP_SESSION_KEY,
)

from ..test_utils.community_catalog_server import (
    BOOM_TEXT,
    HAS_CATALOG_TRANSFORM,
    HAS_COMMUNITY_NESTING,
    create_catalog_meta_server,
    create_composing_server,
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
    not (HAS_COMMUNITY_FASTMCP and HAS_COMMUNITY_CLIENT and HAS_COMMUNITY_NESTING),
    reason="Community FastMCP not available",
)

# Only the hidden-catalog shape needs `CatalogTransform` (mid-3.x); the
# composing-server tests below run on every release the community extra
# allows, so the daily version sweep keeps its nested-call coverage there.
catalog = pytest.mark.skipif(
    not HAS_CATALOG_TRANSFORM,
    reason="fastmcp CatalogTransform not available",
)

MINT_BACK_HEADER = "[session_id issued — see this tool's session_id parameter description]"  # noqa: E501
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
    minted = _text(result).split("session_id: ")[1].split("\n")[0]
    assert minted.startswith("ses_")
    return minted


@catalog
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


@catalog
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


@catalog
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
    assert "[session_id" not in observed["inner_text"]
    assert observed["inner_structured"] == {"result": "echo:hello"}


@catalog
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
    mint = result.structured_content[MCP_SESSION_KEY]
    assert mint["session_id"] == _minted_from(result)
    assert result.structured_content["result"] == "ran:hello"


@catalog
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


@catalog
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


@catalog
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


@catalog
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


@catalog
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


@catalog
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


# ── plain composing servers: nesting without any transform ──────────────────
# These run on EVERY community fastmcp release (no CatalogTransform guard):
# `ctx.fastmcp.call_tool` from a tool body is the ordinary nesting shape, and
# the daily version sweep must keep covering it on pre-transform releases.


async def test_a_composing_tool_is_nested_without_any_transform(capture):
    """The frame does not depend on transforms: a listed tool calling a
    sibling gets the same treatment, and per-call resolution (the actor) is
    still resolved on the nested event rather than copied from the outer."""
    from agentcat.types import UserIdentity

    observed: dict = {}
    server = create_composing_server(observed)
    track(
        server,
        "proj_test",
        AgentCatOptions(
            identify=lambda request, extra: UserIdentity(
                user_id="actor-1", user_name="Composer", user_data=None
            )
        ),
    )

    async with create_community_test_client(server) as client:
        await client.list_tools()
        await client.call_tool(
            "compose",
            {"text": "hi", "context": "Composing one tool from another to test nesting"},
        )

    events = _call_events(capture)
    assert [e.resource_name for e in events] == ["echo", "compose"]
    inner, outer = events
    assert inner.session_id == outer.session_id
    assert inner.tags[AGENTCAT_TAG_NESTED] == "true"
    assert AGENTCAT_TAG_NESTED not in outer.tags
    # Undecorated inner result, exactly as the composing body consumed it.
    assert observed["inner_structured"] == {"result": "echo:hi"}
    # Actor resolution stayed per-call: the nested event carries its own.
    assert inner.identify_actor_given_id == "actor-1"
    assert outer.identify_actor_given_id == "actor-1"


async def test_concurrent_inner_calls_all_join_the_outer_session(capture):
    """Two inner calls running CONCURRENTLY (asyncio.gather in the tool body)
    install and restore their frames in the shared request state in whatever
    order they complete. Every one of them must still inherit the outer
    session, and a later sequential call on the same server must come up
    clean — no stale frame from the concurrent interleaving."""
    observed: dict = {}
    server = create_composing_server(observed)
    track(server, "proj_test", AgentCatOptions())

    async with create_community_test_client(server) as client:
        await client.list_tools()
        await client.call_tool(
            "fanout",
            {"a": "left", "b": "right", "context": "Fanning out two inner calls"},
        )
        await client.call_tool(
            "compose",
            {"text": "after", "context": "A sequential call after the fan-out"},
        )

    events = _call_events(capture)
    fan_outer = next(e for e in events if e.resource_name == "fanout")
    fan_inner = [
        e
        for e in events
        if e.resource_name == "echo" and e.session_id == fan_outer.session_id
    ]
    assert len(fan_inner) == 2
    assert all(e.tags[AGENTCAT_TAG_NESTED] == "true" for e in fan_inner)
    assert AGENTCAT_TAG_NESTED not in fan_outer.tags
    # The sandbox-side results came back clean from both concurrent calls.
    assert observed["fanned"] == [{"result": "echo:left"}, {"result": "echo:right"}]

    # The follow-up call is its own top-level call on a fresh session.
    compose_outer = next(e for e in events if e.resource_name == "compose")
    compose_inner = [
        e
        for e in events
        if e.resource_name == "echo" and e.session_id == compose_outer.session_id
    ]
    assert compose_outer.session_id != fan_outer.session_id
    assert AGENTCAT_TAG_NESTED not in compose_outer.tags
    assert len(compose_inner) == 1
