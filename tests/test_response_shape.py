"""The `response` field's spelling, pinned per flavor rather than normalized.

An event's `response` is the era-native dump of whatever result object that
generation's tool call produced: `_common.response_payload` calls
`model_dump(mode="json")` with no `by_alias`, so official mcp 1.x publishes
`isError` / `structuredContent` and mcp 2.x and both community eras publish
`is_error` / `structured_content`.

**That divergence is deliberate — see the ruling recorded at
`_common.response_payload`.** It is not a regression: v1 community already
published FastMCP's snake_case dump, so "normalizing" would change data the
backend has been receiving rather than fix it. What was missing is that
nothing pinned it, which is how a divergence drifts silently or gets "fixed"
by someone who reads it as a bug. This module is that pin. If you are here
because you changed the spelling on purpose, change the ruling first.

The wire result the agent receives is unaffected either way: this is the
analytics payload, not the tool's answer.
"""

import json

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import MCP_INSTRUCTIONS_KEY, SESSION_ID_PARAM

from .test_utils import sid
from .test_utils.flavors import flavors

# (error key, structured key) as each flavor's own result model spells them.
EXPECTED_SPELLING = {
    "official-fastmcp-v1": ("isError", "structuredContent"),
    "lowlevel-v1": ("isError", "structuredContent"),
    "mcpserver-v2": ("is_error", "structured_content"),
    "lowlevel-v2": ("is_error", "structured_content"),
    "community-v3": ("is_error", "structured_content"),
    "community-v4": ("is_error", "structured_content"),
}

BOTH_SPELLINGS = {"isError", "is_error", "structuredContent", "structured_content"}


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_the_event_response_keeps_its_eras_own_field_names(flavor, capture):
    error_key, structured_key = EXPECTED_SPELLING[flavor.id]

    built = flavor.build("response-shape")
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        await flavor.call(
            client, "echo", {"text": "hi", SESSION_ID_PARAM: sid("supplied")}
        )

    response = capture[0].response
    assert isinstance(response, dict)
    # This era's spelling is present, and the other era's is nowhere in it.
    assert error_key in response
    assert structured_key in response
    assert not (BOTH_SPELLINGS - {error_key, structured_key}) & set(response)
    # ...and it is the customer's own result, undecorated: the mint-back is
    # wire-only and must never reach the analytics payload.
    assert response[structured_key] == {"result": "echo:hi"}
    assert MCP_INSTRUCTIONS_KEY not in json.dumps(response)
    assert "[MCP INSTRUCTIONS]" not in json.dumps(response)
