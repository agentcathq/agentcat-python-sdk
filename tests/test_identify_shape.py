"""The object AgentCat hands the customer's hooks, pinned on every flavor.

`identify`, `event_tags`, `event_properties` and `resolve_session_id` all receive
the same `(request, extra)` pair, and `request` is the tool call's **params** —
the model carrying `.name` and `.arguments` — never the enclosing JSON-RPC
request. That is the only shape all four adapters can produce: mcp 2.x hands
its handler `(ctx, params)` with no request object in scope, and community
FastMCP's `context.message` is params as well, so the official 1.x adapter
unwraps `request.params` to match.

Why this module exists at all: `identify.py` swallows every exception a hook
raises, so a hook written against the WRONG shape does not fail — it silently
yields an anonymous event. The whole v2 branch shipped with `identify`
integration coverage on the legacy e2e suites only, and neither of those reads
the argument dict, so an adapter handing over a different object was invisible.
Here the hook indexes `request.arguments` on every shape a customer can hand
`track()`, and the assertion is on the published event, so a regression is a
failure rather than a `None`.
"""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.types import UserIdentity

from .test_utils.flavors import flavors


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_identify_reads_the_arguments_off_the_request_it_is_given(
    flavor, capture
):
    """The README's own example, run against every server shape.

    A hook that reaches for `.name` / `.arguments` — one hop, not two — works
    everywhere, and the actor it returns reaches the event.
    """
    shapes: list = []

    def identify(request, extra):
        shapes.append(request)
        return UserIdentity(
            user_id=f"user-{request.arguments['text']}",
            user_name=request.name,
            user_data={"tool": request.name},
        )

    built = flavor.build("identify-shape")
    track(built.server, "proj_test", AgentCatOptions(identify=identify))

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        result = await flavor.call(client, "echo", {"text": "hi"})

    assert result.is_error is False
    assert len(shapes) == 1
    # Params, not the enclosing request: a `.params` attribute here would mean
    # this flavor hands over one more layer than the contract promises.
    assert not hasattr(shapes[0], "params")

    assert len(capture) == 1
    event = capture[0]
    assert event.identify_actor_given_id == "user-hi"
    assert event.identify_actor_name == "echo"
    assert event.identify_data == {"tool": "echo"}


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_every_customer_hook_receives_the_same_pair(flavor, capture):
    """`identify`, `event_tags`, `event_properties` and `resolve_session_id` are
    documented to share one signature — so they must share one object."""
    seen: dict[str, tuple] = {}

    def record(name):
        def hook(request, extra):
            seen[name] = (request, extra)
            return None

        return hook

    def tags(request, extra):
        seen["event_tags"] = (request, extra)
        return {"tool": request.name}

    def properties(request, extra):
        seen["event_properties"] = (request, extra)
        return {"args": dict(request.arguments or {})}

    def resolve_session_id(request, extra):
        seen["resolve_session_id"] = (request, extra)
        return f"task-for-{request.name}"

    built = flavor.build("identify-shape")
    track(
        built.server,
        "proj_test",
        AgentCatOptions(
            identify=record("identify"),
            event_tags=tags,
            event_properties=properties,
            resolve_session_id=resolve_session_id,
        ),
    )

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        await flavor.call(client, "echo", {"text": "hi"})

    assert set(seen) == {
        "identify",
        "event_tags",
        "event_properties",
        "resolve_session_id",
    }
    pairs = list(seen.values())
    assert all(pair[0] is pairs[0][0] for pair in pairs)
    assert all(pair[1] is pairs[0][1] for pair in pairs)

    assert len(capture) == 1
    assert capture[0].tags["tool"] == "echo"
    assert capture[0].properties == {"args": {"text": "hi"}}
