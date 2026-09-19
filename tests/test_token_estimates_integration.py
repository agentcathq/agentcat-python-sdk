"""Token estimates ride on every tools/call event, computed on the raw
payloads before the queue's redaction and truncation stages run.

Vectors: {"text":"hi there"} is 19 bytes -> 6 tokens; the flavors' `echo`
tool answers "echo:hi there", 13 bytes -> 4. {"text":"secret-value-123456789"}
is 33 bytes -> 10, and the echo of it, "echo:secret-value-123456789", is 27
bytes -> 8.
"""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules import event_queue

from .test_utils.flavors import flavors


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_tool_call_events_carry_token_estimates(flavor, capture):
    built = flavor.build("token-estimates")
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        await flavor.call(client, "echo", {"text": "hi there"})

    event = capture[0]
    assert event.input_tokens == 6
    # Only the content text counts: the structured mirror of the same answer
    # and the era-specific error/structured keys add nothing.
    assert event.output_tokens == 4


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_counts_survive_redaction_and_truncation(flavor, capture, monkeypatch):
    sent: list = []
    monkeypatch.setattr(event_queue.event_queue, "_send_event", sent.append)

    built = flavor.build("token-estimates-redacted")
    track(
        built.server,
        "proj_test",
        # Only the secret string is rewritten: a hook that replaced every
        # string would also clobber the content block's `type` discriminator.
        AgentCatOptions(
            redact_sensitive_information=lambda text: (
                "[REDACTED]" if "secret" in text else text
            )
        ),
    )

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        await flavor.call(client, "echo", {"text": "secret-value-123456789"})

    queued = capture[0]
    assert queued.redaction_fn is not None
    # Drive the real pipeline (redact -> sanitize -> truncate -> send) on the
    # captured event, exactly as the worker thread would.
    event_queue.event_queue._process_event(queued)

    assert len(sent) == 1
    published = sent[0]
    assert published.parameters["arguments"]["text"] == "[REDACTED]"
    assert published.response["content"][0]["text"] == "[REDACTED]"
    assert published.input_tokens == 10
    assert published.output_tokens == 8  # "echo:secret-value-123456789" = 27 bytes


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_a_broken_estimator_never_breaks_the_tool_call(flavor, capture, monkeypatch):
    """The estimator runs inside the customer's request. Force it to raise
    and the client must still receive the tool's own answer; the call's
    analytics may be lost, the response never is."""

    def explode(_value):
        raise RuntimeError("estimator bug")

    monkeypatch.setattr("agentcat.modules.callpath.estimate_input_tokens", explode)
    monkeypatch.setattr("agentcat.modules.callpath.estimate_output_tokens", explode)

    built = flavor.build("token-estimates-broken")
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        called = await flavor.call(client, "echo", {"text": "hi there"})

    assert not called.is_error
    assert "echo:hi there" in called.text
