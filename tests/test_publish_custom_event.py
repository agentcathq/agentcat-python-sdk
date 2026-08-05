"""`publish_custom_event` — the one event v2 publishes by hand.

TS parity: `publishCustomEvent` (TS `src/index.ts:363`), design §3.4. The task
attribution is VERBATIM in both forms: whatever the caller supplies lands in
`Event.session_id` untouched — never trimmed, prefixed, validated or derived.

Pure Python fakes for everything the semantics can be shown on: this module
has to run under both SDK eras. The one exception is the last section, which
walks every real server shape the installed dependency set can build — because
"is this a tracked server?" is answered by a key derivation that differs per
facade, and no fake can be wrong about it in the way a real one was.

The published event is inspected at the queue, so every test asserts what
actually goes on the wire rather than that the call returned.
"""

from typing import Any

import pytest

import agentcat
from agentcat.modules import event_queue as event_queue_module
from agentcat.modules.internal import (
    reset_server_tracking_data,
    set_server_tracking_data,
)
from agentcat.types import (
    AgentCatData,
    AgentCatOptions,
    EventType,
    UnredactedEvent,
)

from .test_utils.flavors import flavors


class FakeServer:
    """A weakref-able stand-in for a tracked server. No MCP anywhere."""


class FakeQueue:
    """The global event queue, replaced so nothing leaves the process."""

    def __init__(self) -> None:
        self.events: list[UnredactedEvent] = []

    def add(self, event: UnredactedEvent) -> None:
        self.events.append(event)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[UnredactedEvent]:
    """Every event that reached the queue, in order."""
    fake = FakeQueue()
    monkeypatch.setattr(event_queue_module, "event_queue", fake)
    return fake.events


@pytest.fixture
def tracked() -> Any:
    """A server tracked to `proj_tracked`, cleaned up after the test."""
    server = FakeServer()
    set_server_tracking_data(
        server,
        AgentCatData(
            project_id="proj_tracked",
            options=AgentCatOptions(),
            server_name="todo-server",
            server_version="4.2.0",
        ),
    )
    yield server
    reset_server_tracking_data(server)


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Lines the SDK logged, so a no-op can be shown to be a loud one."""
    lines: list[str] = []
    monkeypatch.setattr(agentcat, "write_to_log", lambda msg: lines.append(str(msg)))
    return lines


# ── Task attribution: verbatim, both forms ───────────────────────────────────


def test_string_form_uses_the_string_verbatim(published):
    """A caller's own correlation ID is the session ID. No `ses_`, no minting."""
    agentcat.publish_custom_event("order-9f2c/checkout", "proj_abc")

    assert len(published) == 1
    assert published[0].session_id == "order-9f2c/checkout"


def test_session_id_in_event_data_beats_the_string(published):
    agentcat.publish_custom_event(
        "ignored-correlation-id", "proj_abc", {"session_id": "ses_from_event_data"}
    )

    assert published[0].session_id == "ses_from_event_data"


def test_tracked_server_uses_event_data_session_id_verbatim(published, tracked):
    agentcat.publish_custom_event(tracked, "proj_abc", {"session_id": "ses_LIVE"})

    assert published[0].session_id == "ses_LIVE"


def test_tracked_server_without_a_session_id_publishes_untethered(published, tracked):
    """Handles are per-request in v2, so a tracked server has no ambient task.

    The event still publishes — it just lands untethered rather than being
    attributed to a task that was never supplied.
    """
    agentcat.publish_custom_event(tracked, "proj_abc", {"resource_name": "cron"})

    assert len(published) == 1
    assert published[0].session_id is None
    assert published[0].resource_name == "cron"


def test_an_empty_session_id_is_no_task_at_all(published, tracked):
    agentcat.publish_custom_event("", "proj_abc")
    agentcat.publish_custom_event(tracked, "proj_abc", {"session_id": ""})

    assert len(published) == 2
    assert all(event.session_id is None for event in published)


def test_a_non_string_session_id_is_ignored_never_stringified(published, tracked):
    """Verbatim cuts both ways: what cannot be sent as-is is not derived."""
    agentcat.publish_custom_event("fallback-id", "proj_abc", {"session_id": 42})
    agentcat.publish_custom_event(tracked, "proj_abc", {"session_id": {"a": 1}})

    assert published[0].session_id == "fallback-id"
    assert published[1].session_id is None


# ── Event shape ──────────────────────────────────────────────────────────────


def test_event_type_is_agentcat_custom(published, tracked):
    agentcat.publish_custom_event("ses_T", "proj_abc")
    agentcat.publish_custom_event(tracked, "proj_abc")

    assert [event.event_type for event in published] == ["agentcat:custom"] * 2
    assert published[0].event_type == EventType.AGENTCAT_CUSTOM.value


def test_custom_event_data_reaches_the_event(published):
    agentcat.publish_custom_event(
        "ses_T",
        "proj_abc",
        {
            "resource_name": "checkout",
            "parameters": {"cart": 3},
            "response": {"status": "ok"},
            "message": "order confirmed",
            "duration": 1234,
            "is_error": True,
            "error": {"message": "card declined", "code": "ERR_001"},
            "tags": {"env": "prod"},
            "properties": {"amount": 42.5},
        },
    )

    event = published[0]
    assert event.resource_name == "checkout"
    assert event.parameters == {"cart": 3}
    assert event.response == {"status": "ok"}
    # `message` is the human explanation — the same field intent capture uses.
    assert event.user_intent == "order confirmed"
    assert event.duration == 1234
    assert event.is_error is True
    assert event.error == {"message": "card declined", "code": "ERR_001"}
    assert event.tags == {"env": "prod"}
    assert event.properties == {"amount": 42.5}


def test_event_data_is_optional(published, tracked):
    agentcat.publish_custom_event(tracked, "proj_abc")

    assert len(published) == 1
    assert published[0].event_type == "agentcat:custom"
    assert published[0].timestamp is not None


def test_tags_are_validated_and_bad_entries_dropped(published):
    """Customer tags ride the same validator every other event's tags do."""
    agentcat.publish_custom_event(
        "ses_T",
        "proj_abc",
        {"tags": {"env": "prod", "bad key!": "x", "numeric": 7}},
    )

    assert published[0].tags == {"env": "prod"}


def test_tags_that_are_not_a_dict_are_dropped_out_loud(published, logged):
    """Every other degradation here logs; this one used to be the exception."""
    agentcat.publish_custom_event("ses_T", "proj_abc", {"tags": "env=prod"})

    assert published[0].tags is None
    assert any("tags" in line for line in logged)


# ── Nothing a caller can pass may make the event vanish ──────────────────────


def test_a_non_dict_response_is_kept_not_dropped(published):
    """`PublishEventRequest.response` is `Optional[Dict[str, Any]]`.

    A non-dict fails pydantic construction, and construction failures are
    swallowed at the publish site — so an unwrapped string response would take
    the whole event down with it, silently. `CustomEventData["response"]` is
    typed `Any` and comes straight from the caller, so a string, a list or a
    number is a plausible input, not a bug to be punished.
    """
    agentcat.publish_custom_event("ses_T", "proj_abc", {"response": "shipped"})

    assert len(published) == 1
    assert published[0].response == {"value": "shipped"}


def test_non_dict_parameters_and_properties_are_kept(published):
    agentcat.publish_custom_event(
        "ses_T", "proj_abc", {"parameters": [1, 2], "properties": "why"}
    )

    assert published[0].parameters == {"value": [1, 2]}
    assert published[0].properties == {"value": "why"}


def test_a_non_dict_error_becomes_a_message(published):
    """`error` has a known shape (`ErrorData`): the message is what consumers read."""
    agentcat.publish_custom_event(
        "ses_T", "proj_abc", {"is_error": True, "error": "card declined"}
    )

    assert published[0].is_error is True
    assert published[0].error == {"message": "card declined"}


@pytest.mark.parametrize(
    ("event_data", "wire_field"),
    [
        ({"duration": 12.5}, "duration"),
        ({"duration": True}, "duration"),
        ({"is_error": 1}, "is_error"),
        ({"resource_name": 42}, "resource_name"),
        ({"message": {"not": "a string"}}, "user_intent"),
        ({"tags": "not-a-dict"}, "tags"),
    ],
)
def test_a_mistyped_field_costs_that_field_not_the_event(
    published, event_data, wire_field
):
    """Strict wire types (`StrictInt`/`StrictBool`/`StrictStr`) reject a near
    miss outright, so the field is dropped — not coerced — and the event still
    publishes."""
    agentcat.publish_custom_event("ses_T", "proj_abc", event_data)

    assert len(published) == 1
    assert published[0].session_id == "ses_T"
    assert getattr(published[0], wire_field) is None


def test_custom_events_survive_the_generated_clients_stale_event_type_enum():
    """`agentcat_api` 1.0.0 validates `event_type` against a spec enum that
    predates `agentcat:custom`, so the generated model rejects the very event
    this API exists to publish. `Event` overrides that check with the SDK's own
    `EventType`, which is the source of truth for what v2 publishes."""
    for event_type in EventType:
        assert UnredactedEvent(event_type=event_type.value).event_type == (
            event_type.value
        )


# ── Fire-and-forget: never raises, whatever it is handed ─────────────────────


@pytest.mark.parametrize(
    "first_argument",
    [None, 42, {"not": "a server"}, ["nope"], FakeServer()],
    ids=["none", "int", "dict", "list", "untracked-server"],
)
def test_a_bad_first_argument_never_raises(published, logged, first_argument):
    agentcat.publish_custom_event(first_argument, "proj_abc")

    assert published == []
    assert logged, "a dropped event must say so in the log"


def test_a_non_dict_event_data_never_raises(published):
    agentcat.publish_custom_event("ses_T", "proj_abc", "not-event-data")

    assert len(published) == 1
    assert published[0].session_id == "ses_T"


def test_a_queue_that_blows_up_never_raises(monkeypatch, logged, tracked):
    class Exploding:
        def add(self, event: UnredactedEvent) -> None:
            raise RuntimeError("queue is on fire")

    monkeypatch.setattr(event_queue_module, "event_queue", Exploding())

    agentcat.publish_custom_event("ses_T", "proj_abc")
    agentcat.publish_custom_event(tracked, "proj_abc")

    assert logged


@pytest.mark.parametrize("project_id", ["", None, 42], ids=["empty", "none", "int"])
def test_the_string_form_needs_a_project_id(published, logged, project_id):
    """Nothing else can supply one: a session ID string carries no tracking data."""
    agentcat.publish_custom_event("ses_T", project_id)

    assert published == []
    assert any("project_id" in line for line in logged)


def test_a_mistyped_project_id_never_costs_a_tracked_event(published, tracked):
    """The tracked server's own project answers for it."""
    agentcat.publish_custom_event(tracked, 42, {"session_id": "ses_T"})

    assert published[0].project_id == "proj_tracked"


# ── Project and SDK attribution ──────────────────────────────────────────────


def test_the_string_form_carries_the_project_it_was_given(published):
    agentcat.publish_custom_event("ses_T", "proj_from_argument")

    assert published[0].project_id == "proj_from_argument"
    assert published[0].sdk_language.startswith("Python ")
    assert published[0].agentcat_version == agentcat.__version__


def test_the_tracked_form_takes_the_project_from_the_tracked_server(published, tracked):
    """The server was tracked to a project; that is the event's project, and
    the server identity captured at `track()` time rides along with it."""
    agentcat.publish_custom_event(tracked, "", {"session_id": "ses_T"})

    event = published[0]
    assert event.project_id == "proj_tracked"
    assert event.server_name == "todo-server"
    assert event.server_version == "4.2.0"
    assert event.sdk_language.startswith("Python ")


def test_a_server_tracked_with_tracing_off_publishes_nothing(published, logged):
    """`enable_tracing=False` silences a server's tool calls in every adapter.

    This entry point does not reach an adapter, and `publish_event` has no gate
    of its own (TS keeps one inside `publishEvent`), so it carries the gate
    itself: a customer who turned tracing off does not start emitting a new
    event type because they called a new API.
    """
    server = FakeServer()
    set_server_tracking_data(
        server,
        AgentCatData(
            project_id="proj_tracked",
            options=AgentCatOptions(enable_tracing=False),
        ),
    )
    try:
        agentcat.publish_custom_event(server, "proj_tracked", {"session_id": "ses_T"})
    finally:
        reset_server_tracking_data(server)

    assert published == []
    assert any("enable_tracing" in line for line in logged)


def test_the_string_form_is_not_gated_on_a_servers_tracing_flag(published):
    """A session ID string carries no server, so there are no options to consult —
    the same place TS lands, since its string form bypasses `publishEvent`."""
    agentcat.publish_custom_event("ses_T", "proj_abc")

    assert len(published) == 1


def test_the_tracked_form_applies_the_customers_redaction(published):
    """A tracked custom event goes through the same publish path every
    tool-call event does, so the customer's redaction hook is attached."""
    server = FakeServer()
    set_server_tracking_data(
        server,
        AgentCatData(
            project_id="proj_tracked",
            options=AgentCatOptions(
                redact_sensitive_information=lambda text: "REDACTED"
            ),
        ),
    )
    try:
        agentcat.publish_custom_event(server, "proj_tracked", {"session_id": "ses_T"})
    finally:
        reset_server_tracking_data(server)

    assert published[0].redaction_fn is not None


# ── Every real server shape resolves ─────────────────────────────────────────


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
def test_a_tracked_server_of_any_flavor_resolves_to_its_own_tracking_data(
    flavor, published
):
    """The customer holds the object they handed `track()`, not what it wraps.

    On both official facades that object is NOT where the data was stored: the
    adapter is installed on the lowlevel server underneath, and the data is
    filed there with it. TS unwraps `serverOrSessionId.server` at the entry
    point; Python re-derives the key inside `_get_server_key`, and nothing
    pinned that it did so for every facade — `MCPServer` was missing, and a
    custom event published against one was silently dropped.
    """
    built = flavor.build("custom-event")
    agentcat.track(built.server, "proj_flavor", AgentCatOptions())
    try:
        agentcat.publish_custom_event(
            built.server, "proj_ignored", {"session_id": "ses_custom", "message": "hi"}
        )
    finally:
        reset_server_tracking_data(built.server)

    assert len(published) == 1, "the server did not resolve to its tracking data"
    event = published[0]
    assert event.event_type == EventType.AGENTCAT_CUSTOM.value
    assert event.session_id == "ses_custom"
    # The project captured at track() time wins over the argument — which is
    # only observable if the tracked server really was found.
    assert event.project_id == "proj_flavor"


# ── Public API ───────────────────────────────────────────────────────────────


def test_exported_from_the_package():
    assert "publish_custom_event" in agentcat.__all__
    assert "CustomEventData" in agentcat.__all__
    assert callable(agentcat.publish_custom_event)
