"""A customer parameter that happens to share one of AgentCat's names.

`session_id`, `agent_id` and `context` are ordinary words. A task tracker, a job
runner or a ticketing tool can already have a `session_id` of its own, and the
injection pass is careful about it: on a name collision it logs and skips, and
the strip spares the parameter so the customer's handler still receives it.

What was missing is the third consumer. The call path read
`arguments["session_id"]` unconditionally, so a parameter AgentCat never injected
was consumed as the analytics handle anyway. Two things went wrong at once:

- **Analytics.** Every call to that tool collapsed onto one AgentCat session
  keyed by a customer-domain value, severed from the agent's real conversation.
- **Privacy.** `session_id` is in `redaction.PROTECTED_FIELDS`, so that value
  reached the wire EXEMPT from the customer's own redaction hook — and a
  `session_id` in a customer's domain is plausibly an email, an order number or an
  account ID.

The rule this module pins: a `session_id` the customer's own schema declared is
never read, and such calls publish **sessionless** rather than minting. Minting
one per call on a tool that can never carry AgentCat's handle would manufacture
a phantom session per call — noise shaped like data. Sessionless is the honest
signal, and it resolves the moment the customer adopts `resolve_session_id`.

Ownership is read off `AgentCatData.declared_session_params`, a positive record
of what the customer declared, populated by the injection pass during listing.

Runs on every server shape the installed dependency set can build, in both
dependency sets — this is a per-flavor property, because the registry is
populated by each adapter's own listing path.
"""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import (
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_SESSION_KEY,
    SESSION_ID_PARAM,
    SESSION_ID_PARAM_DESCRIPTION,
)

from .test_utils import sid
from .test_utils.flavors import CUSTOMER_SESSION_ID_DESCRIPTION, flavors


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_a_customers_own_session_id_is_never_the_handle(flavor, capture):
    built = flavor.build("customer-owned", customer_session_id=True)
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        listed = await flavor.list_tools(client)
        complete = next(t for t in listed if t.name == "complete_task")
        # The schema is the customer's. A description they wrote survives (the
        # lowlevel flavors declare one; the facades generate the schema from a
        # typed signature and have none), and on no flavor is it replaced by
        # AgentCat's copy telling the agent what session_id means to us.
        described = complete.input_schema["properties"][SESSION_ID_PARAM].get(
            "description"
        )
        assert described in (None, CUSTOMER_SESSION_ID_DESCRIPTION)
        assert described != SESSION_ID_PARAM_DESCRIPTION

        first = await flavor.call(
            client, "complete_task", {"session_id": "TASK-1234", "note": "done"}
        )
        second = await flavor.call(
            client, "complete_task", {"session_id": "TASK-1234", "note": "again"}
        )

    # The handler ran on the customer's value, untouched.
    assert "completed TASK-1234: done" in first.text
    assert first.is_error is False

    events = capture
    assert len(events) == 2
    for event in events:
        # Sessionless, not minted: their value is never adopted, and no
        # phantom session is manufactured in its place.
        assert event.session_id is None
        assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "foreign"

    # And AgentCat never tells the agent to send `session_id=ses_…` on a tool
    # whose `session_id` means something else — that would change what the
    # customer's tool does — nor confirms their value back to them.
    # (Their own tool body still echoes TASK-1234 in its result — that is their
    # data. What must not appear is an AgentCat-authored block naming it, which
    # is what the absence of MCP_SESSION_KEY asserts.)
    for result in (first, second):
        assert "[session_id" not in result.text
        assert MCP_SESSION_KEY not in (result.structured or {})


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_a_tool_agentcat_did_inject_still_supplies_normally(flavor, capture):
    """The gate is per tool, not per server: the colliding tool above and this
    one live on the same instance."""
    built = flavor.build("customer-owned", customer_session_id=True)
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        listed = await flavor.list_tools(client)
        echo = next(t for t in listed if t.name == "echo")
        assert SESSION_ID_PARAM in echo.input_schema["properties"]

        await flavor.call(client, "echo", {"text": "hi", SESSION_ID_PARAM: sid("mine")})
        await flavor.call(
            client, "complete_task", {"session_id": "TASK-1234", "note": "n"}
        )

    echoed, completed = capture
    assert echoed.session_id == sid("mine")
    assert echoed.tags[AGENTCAT_TAG_SESSION_SOURCE] == "supplied"
    assert completed.session_id is None
    assert completed.tags[AGENTCAT_TAG_SESSION_SOURCE] == "foreign"


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_agent_id_is_still_confirmed_on_a_colliding_tool(flavor, capture):
    """Suppression is per handle, not per response.

    A `session_id` collision skips only `session_id` injection. `agent_id` is
    a separate branch and still lands in that tool's schema, so the agent
    still sends one and it is still ours to confirm. Dropping the whole mirror
    would withhold a handle AgentCat issued purely because a neighbouring one
    belongs to the customer, and would leave agents seeing `agent_id`
    confirmed on some tools and not others on the same server.
    """
    built = flavor.build("customer-owned", customer_session_id=True)
    track(built.server, "proj_test", AgentCatOptions(enable_agent_tracking=True))

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        result = await flavor.call(
            client,
            "complete_task",
            {"session_id": "TASK-1234", "note": "n", "agent_id": "opus|cc|k3n9x"},
        )

    mint = (result.structured or {}).get(MCP_SESSION_KEY)
    assert mint is not None, "agent_id was withheld because session_id collided"
    assert mint["agent_id"] == "opus|cc|k3n9x"
    assert SESSION_ID_PARAM not in mint
    assert "TASK-1234" not in str(mint)

    (event,) = capture
    assert event.session_id is None
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "foreign"
    assert event.tags["agentcat_agent_id"] == "opus|cc|k3n9x"
