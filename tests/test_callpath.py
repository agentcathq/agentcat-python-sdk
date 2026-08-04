"""Shared per-call orchestration: resolve / strip / decorate / publish.

`callpath` is the one code path every adapter (lowlevel v1, lowlevel v2,
community FastMCP) runs for `tools/call`, so it is exercised here with pure
Python fakes and no MCP imports at all — the module must import and behave
identically under mcp 1.x and mcp 2.x.

Behavior contract: 2026-07-28-cross-sdk-changelog.md §3.4, §6.2-§6.5; TS
reference src/engine/callWrap.ts.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from agentcat.modules import event_queue
from agentcat.modules.callpath import (
    ResolvedCall,
    decorate_content,
    detect_mrtr,
    get_stripped_arguments,
    publish_tool_call_event,
    resolve_call,
    structured_mirror,
)
from agentcat.modules.client_identity import ClientIdentity
from agentcat.modules.handles import HandleResolution
from agentcat.modules.injection import ToolSpec
from agentcat.types import (
    AgentCatData,
    AgentCatOptions,
    UnredactedEvent,
    UserIdentity,
)

from .test_utils import sid

CLIENT_KEY = "io.modelcontextprotocol/clientInfo"
PV_KEY = "io.modelcontextprotocol/protocolVersion"


def make_data(options: AgentCatOptions | None = None, **kwargs: Any) -> AgentCatData:
    """Tracking data in its v2 shape: a project, options, and engine state."""
    return AgentCatData(
        project_id="proj_1",
        options=options if options is not None else AgentCatOptions(),
        **kwargs,
    )


def text_block(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


# ── detect_mrtr ──────────────────────────────────────────────────────────────


def test_detect_mrtr_matrix():
    assert detect_mrtr("input_required", False) == "input_required"
    assert detect_mrtr("input_required", True) == "input_required"  # intermediate wins
    assert detect_mrtr(None, True) == "continuation"
    assert detect_mrtr("complete", False) is None


# An ordinary completing round carries neither signal; a continuation that
# completes is tagged `continuation`, not dropped (§6.4).
def test_detect_mrtr_plain_and_completing_continuation():
    assert detect_mrtr(None, False) is None
    assert detect_mrtr("complete", True) == "continuation"
    assert detect_mrtr("", False) is None


def test_detect_mrtr_tags_the_request_state_only_continuation():
    """A resumed round carries `requestState` and no responses at all.

    The SEP-2322 driver answers an intermediate round that asked no questions
    by retrying after a backoff with `inputResponses=None` and the echoed
    `requestState` — the shape FastMCP 4's own
    `InputRequiredResult(request_state=...)` produces. Keying the tag on
    `inputResponses` alone left it untagged.
    """
    assert detect_mrtr(None, False, True) == "continuation"
    assert detect_mrtr("complete", False, True) == "continuation"
    # Intermediate still wins over either witness.
    assert detect_mrtr("input_required", False, True) == "input_required"
    # And the default keeps every existing two-argument call site honest.
    assert detect_mrtr(None, True) == "continuation"
    assert detect_mrtr(None, False) is None


# ── decorate_content ─────────────────────────────────────────────────────────


def test_decorate_content_only_when_minted_prompted():
    minted = HandleResolution(sid("T"), "minted")
    out = decorate_content([{"type": "text", "text": "x"}], minted, text_block)
    assert out is not None
    assert "[MCP INSTRUCTIONS]: session_id issued." in out[-1]["text"]
    assert decorate_content([{"t": 1}], HandleResolution(sid("T"), "supplied"), dict) is None  # noqa: E501
    assert decorate_content([{"t": 1}], HandleResolution(sid("T"), "minted", hook_mode=True), dict) is None  # noqa: E501
    assert decorate_content("not-a-list", minted, dict) is None


# §3.4a: the appended text carries the id the agent must echo, built by the one
# source of truth (handles.build_mint_back_text) rather than re-derived here.
def test_decorate_content_appends_the_minted_id():
    out = decorate_content([], HandleResolution(sid("ABC"), "minted"), text_block)
    assert out is not None and len(out) == 1
    assert "session_id=ses_ABC" in out[0]["text"]
    assert out[0]["text"].endswith(
        "Without session_id, this server does not function as intended."
    )


# §3.4a: "Append it to error results too" — decorate_content never inspects
# error state, so an isError result decorates on exactly the same terms.
def test_decorate_content_ignores_error_state():
    error_content = [{"type": "text", "text": "boom: tool failed"}]
    out = decorate_content(error_content, HandleResolution(sid("T"), "minted"), text_block)  # noqa: E501
    assert out is not None and len(out) == 2
    assert "[MCP INSTRUCTIONS]" in out[-1]["text"]


# Never mutate the customer's result: the returned list is new and the
# original is untouched.
def test_decorate_content_returns_a_new_list():
    original = [{"type": "text", "text": "x"}]
    out = decorate_content(original, HandleResolution(sid("T"), "minted"), text_block)
    assert out is not original
    assert original == [{"type": "text", "text": "x"}]


def test_decorate_content_none_content():
    assert decorate_content(None, HandleResolution(sid("T"), "minted"), dict) is None


# ── structured_mirror ────────────────────────────────────────────────────────


def test_structured_mirror_gate_matrix():
    res = HandleResolution(sid("T"), "minted")
    # In the output-injection registry: the tool's outputSchema declares the key.
    mirrored = structured_mirror({"ok": True}, res, "search", {"search"})
    assert mirrored is not None
    assert mirrored["ok"] is True
    assert mirrored["_mcp_instructions"]["session_id"] == sid("T")
    # Registry exists but this tool is not in it: mirroring would fail the
    # customer's own schema validation.
    assert structured_mirror({"ok": True}, res, "other", {"search"}) is None
    # No registry at all (rebuild failed) — mirror anyway (§3.4b).
    assert structured_mirror({"ok": True}, res, "other", None) is not None
    # Empty registry is a real registry, not a missing one.
    assert structured_mirror({"ok": True}, res, "search", set()) is None


def test_structured_mirror_delegates_shape_rules():
    res = HandleResolution(sid("T"), "minted")
    # Non-dict structuredContent (or none at all) is never mirrored into.
    assert structured_mirror(None, res, "search", None) is None
    assert structured_mirror(["a"], res, "search", None) is None
    assert structured_mirror("str", res, "search", None) is None
    # Customer data under the key wins.
    assert structured_mirror({"_mcp_instructions": "mine"}, res, "search", None) is None


def test_structured_mirror_hook_mode_without_agent_has_nothing_to_mirror():
    hook = HandleResolution(sid("T"), "hook", hook_mode=True)
    assert structured_mirror({"ok": True}, hook, "search", None) is None

    with_agent = HandleResolution(
        sid("T"), "hook", "agt|x|1", "supplied", hook_mode=True
    )
    mirrored = structured_mirror({"ok": True}, with_agent, "search", None)
    assert mirrored is not None
    assert "session_id" not in mirrored["_mcp_instructions"]
    assert mirrored["_mcp_instructions"]["agent_id"] == "agt|x|1"


# ── get_stripped_arguments ───────────────────────────────────────────────────


async def test_strip_uses_the_existing_registry():
    data = make_data()
    data.injected_params_registry = {
        "search": {"session_id", "context"},
        "other": set(),
    }
    raw = {"q": 1, "session_id": sid("T"), "agent_id": "a", "context": "why"}

    assert await get_stripped_arguments(data, data.options, "search", raw, None) == {
        "q": 1,
        "agent_id": "a",
    }
    # Listed with an empty entry: nothing was injected, so nothing is stripped.
    assert await get_stripped_arguments(data, data.options, "other", raw, None) == raw
    # Unlisted tool: never advertised through the pipeline, strip nothing.
    assert await get_stripped_arguments(data, data.options, "ghost", raw, None) == raw
    # Never mutate the caller's dict.
    assert raw == {"q": 1, "session_id": sid("T"), "agent_id": "a", "context": "why"}


async def test_strip_rebuilds_registries_on_demand():
    data = make_data(AgentCatOptions(enable_agent_tracking=True))
    specs = [
        ToolSpec("search", {"type": "object", "properties": {"q": {"type": "string"}}}),
        ToolSpec("other", {"type": "object", "properties": {}}, {"type": "object"}),
    ]
    calls: list[int] = []

    async def rebuild() -> list[ToolSpec]:
        calls.append(1)
        return specs

    out = await get_stripped_arguments(
        data,
        data.options,
        "search",
        {"q": "x", "session_id": sid("T"), "context": "why"},
        rebuild,
    )
    assert out == {"q": "x"}
    assert calls == [1]
    # Registries landed on data for every later call on this instance.
    assert data.injected_params_registry == {
        "search": {"session_id", "agent_id", "context"},
        "other": {"session_id", "agent_id", "context"},
    }
    assert data.output_injection_registry == {"other"}

    # Second call reuses the stored registry instead of rebuilding.
    await get_stripped_arguments(data, data.options, "search", {"q": "x"}, rebuild)
    assert calls == [1]


# Mode gating (§5): with tracing off nothing is injected, so the rebuilt
# registry strips nothing — the registry, not a heuristic, is the truth.
async def test_strip_registry_reflects_injection_mode_gating():
    data = make_data(
        AgentCatOptions(enable_tracing=False, enable_tool_call_context=False)
    )

    async def rebuild() -> list[ToolSpec]:
        return [ToolSpec("search", {"type": "object", "properties": {}})]

    raw = {"q": 1, "session_id": sid("T"), "context": "why"}
    stripped = await get_stripped_arguments(data, data.options, "search", raw, rebuild)
    assert stripped == raw
    assert data.injected_params_registry == {"search": set()}


async def test_rebuild_failure_falls_back_to_heuristic(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr("agentcat.modules.callpath.write_to_log", logged.append)
    data = make_data()
    # A stale output registry from some earlier state must not survive the
    # failure: it would gate the mirror on knowledge we no longer have.
    data.output_injection_registry = {"some_other_tool"}

    async def broken_rebuild() -> list[ToolSpec]:
        raise RuntimeError("list source gone")

    # "s" is not a minted-shape handle: on the degraded path it is presumed
    # the customer's own parameter and rides through to their handler; only
    # context — which default options would have injected — is stripped.
    out = await get_stripped_arguments(
        data,
        data.options,
        "t",
        {"q": 1, "session_id": "s", "context": "c"},
        broken_rebuild,
    )
    assert out == {"q": 1, "session_id": "s"}
    assert data.output_injection_registry is None  # mirror gate bypassed
    assert any("list source gone" in entry for entry in logged)

    # A minted-shape value is ours even without a registry.
    data2 = make_data()
    out2 = await get_stripped_arguments(
        data2,
        data2.options,
        "t",
        {"q": 1, "session_id": sid("mine"), "context": "c"},
        broken_rebuild,
    )
    assert out2 == {"q": 1}


# Two calls can race the rebuild on a server that never served tools/list.
# A loser's failure must not wipe the registries a winner already stored: every
# later call would then heuristic-strip, eating the `context` parameter of any
# customer tool that owns one.
async def test_a_failed_rebuild_does_not_wipe_a_concurrent_success():
    data = make_data()
    winner_stored = asyncio.Event()

    async def slow_failing_rebuild() -> list[ToolSpec]:
        await winner_stored.wait()
        raise RuntimeError("list source gone")

    async def good_rebuild() -> list[ToolSpec]:
        return [ToolSpec("search", {"type": "object", "properties": {}})]

    # The loser reads the empty registry first, then parks inside its rebuild.
    loser = asyncio.create_task(
        get_stripped_arguments(
            data, data.options, "search", {"q": 1}, slow_failing_rebuild
        )
    )
    await asyncio.sleep(0)
    await get_stripped_arguments(data, data.options, "search", {"q": 1}, good_rebuild)
    assert data.injected_params_registry == {"search": {"session_id", "context"}}

    winner_stored.set()
    assert await loser == {"q": 1}
    # Still the winner's registry, not None.
    assert data.injected_params_registry == {"search": {"session_id", "context"}}


# No rebuild hook at all (adapter has no list source): straight to the
# shape+config-aware fallback, and get_more_tools keeps its own `context`
# parameter (§6.6).
async def test_strip_without_rebuild_uses_the_heuristic():
    data = make_data()
    # Minted-shape session_id is ours; agent_id survives (tracking off by
    # default); context is ours by config.
    raw = {"q": 1, "session_id": sid("m"), "agent_id": "a", "context": "c"}
    assert await get_stripped_arguments(data, data.options, "t", raw, None) == {
        "q": 1,
        "agent_id": "a",
    }
    assert await get_stripped_arguments(
        data, data.options, "get_more_tools", raw, None
    ) == {"q": 1, "agent_id": "a", "context": "c"}
    # A customer-shaped session_id value survives everywhere on this path.
    theirs = {"q": 1, "session_id": "TICKET-9", "context": "c"}
    assert await get_stripped_arguments(data, data.options, "t", theirs, None) == {
        "q": 1,
        "session_id": "TICKET-9",
    }


# ── resolve_call ─────────────────────────────────────────────────────────────


async def test_resolve_call_resolves_handles_client_and_protocol():
    data = make_data()
    rc = await resolve_call(
        data,
        "search",
        {"session_id": f" {sid('supplied')} ", "context": "why the call"},
        request={"params": {}},
        extra=None,
        meta_sources=[
            {CLIENT_KEY: {"name": "cursor", "version": "2.1"}, PV_KEY: "2026-07-28"}
        ],
        legacy_client=lambda: None,
    )
    assert rc.resolution.session_id == sid("supplied")
    assert rc.resolution.session_source == "supplied"
    assert rc.client == ClientIdentity("cursor", "2.1")
    assert rc.protocol_version == "2026-07-28"
    assert rc.intent == "why the call"
    assert rc.actor is None


async def test_resolve_call_protocol_fallback_and_legacy_client():
    data = make_data()
    rc = await resolve_call(
        data,
        "search",
        {},
        request=None,
        extra=None,
        meta_sources=[None],
        legacy_client=lambda: {"name": "legacy", "version": "0.9"},
        protocol_fallback="2025-06-18",
    )
    assert rc.client == ClientIdentity("legacy", "0.9")
    assert rc.protocol_version == "2025-06-18"
    assert rc.resolution.session_source == "minted"
    assert rc.resolution.session_id.startswith("ses_")


async def test_resolve_call_intent_only_when_a_string():
    data = make_data()

    async def intent_for(arguments):
        rc = await resolve_call(
            data,
            "search",
            arguments,
            None,
            None,
            meta_sources=[],
            legacy_client=lambda: None,
        )
        return rc.intent

    assert await intent_for({"context": "third-person explanation"}) == "third-person explanation"  # noqa: E501
    assert await intent_for({}) is None
    assert await intent_for({"context": {"nested": True}}) is None
    assert await intent_for({"context": 42}) is None
    assert await intent_for({"context": None}) is None


# The intent is read from the RAW arguments, before any stripping — the event
# records what the agent actually sent.
async def test_resolve_call_reads_intent_before_stripping():
    data = make_data()
    raw = {"context": "why", "session_id": sid("T")}
    rc = await resolve_call(
        data, "search", raw, None, None, meta_sources=[], legacy_client=lambda: None
    )
    stripped = await get_stripped_arguments(data, data.options, "search", raw, None)
    assert rc.intent == "why"
    assert "context" not in stripped


async def test_resolve_call_identify_hook_error_yields_no_actor():
    def boom(request, extra):
        raise RuntimeError("customer identify blew up")

    data = make_data(AgentCatOptions(identify=boom))
    rc = await resolve_call(
        data, "search", {}, None, None, meta_sources=[], legacy_client=lambda: None
    )
    assert rc.actor is None
    assert rc.resolution.session_id.startswith("ses_")  # the call still resolves


async def test_resolve_call_captures_the_actor():
    identity = UserIdentity(user_id="u1", user_name="Ada", user_data={"plan": "pro"})
    seen: list[tuple[Any, Any]] = []

    def identify(request, extra):
        seen.append((request, extra))
        return identity

    data = make_data(AgentCatOptions(identify=identify))
    rc = await resolve_call(
        data, "search", {}, request="REQ", extra="EXTRA", meta_sources=[], legacy_client=lambda: None  # noqa: E501
    )
    assert rc.actor is identity
    assert seen == [("REQ", "EXTRA")]


# A hook returning something that is not a UserIdentity is not an actor.
async def test_resolve_call_rejects_a_non_identity_return():
    data = make_data(AgentCatOptions(identify=lambda request, extra: {"user_id": "u1"}))
    rc = await resolve_call(
        data, "search", {}, None, None, meta_sources=[], legacy_client=lambda: None
    )
    assert rc.actor is None


# v2 publishes exactly one event per tool call, from publish_tool_call_event.
# Resolution — including the identify hook — publishes nothing.
async def test_resolve_call_publishes_nothing(monkeypatch):
    published: list[Any] = []
    monkeypatch.setattr(event_queue, "publish_event", lambda server, event: published.append(event))  # noqa: E501
    identity = UserIdentity(user_id="u1", user_name=None, user_data=None)
    data = make_data(AgentCatOptions(identify=lambda request, extra: identity))

    rc = await resolve_call(
        data, "search", {}, None, None, meta_sources=[], legacy_client=lambda: None
    )
    assert rc.actor is identity
    assert published == []


# ── publish_tool_call_event ──────────────────────────────────────────────────


def capture_published(monkeypatch) -> list[UnredactedEvent]:
    published: list[UnredactedEvent] = []
    monkeypatch.setattr(
        event_queue, "publish_event", lambda server, event: published.append(event)
    )
    return published


def resolved(**kwargs: Any) -> ResolvedCall:
    defaults: dict[str, Any] = {
        "resolution": HandleResolution(sid("T"), "minted"),
        "actor": None,
        "client": ClientIdentity("cursor", "2.1"),
        "protocol_version": "2026-07-28",
        "intent": "why the call",
    }
    defaults.update(kwargs)
    return ResolvedCall(**defaults)


async def test_publish_tool_call_event_builds_the_event(monkeypatch):
    published = capture_published(monkeypatch)
    data = make_data()
    actor = UserIdentity(user_id="u1", user_name="Ada", user_data={"plan": "pro"})
    raw = {"q": "x", "session_id": sid("T"), "context": "why the call"}
    server = object()

    await publish_tool_call_event(
        server,
        data,
        resolved(actor=actor),
        "search",
        raw,
        response={"content": [{"type": "text", "text": "ok"}]},
        is_error=False,
        error=None,
        duration_ms=42,
        mrtr=None,
        extra_params={"extra": {"requestId": 7}},
    )

    assert len(published) == 1
    event = published[0]
    assert event.event_type == "mcp:tools/call"
    assert event.session_id == sid("T")  # the handle, not any transport session
    assert event.resource_name == "search"
    assert event.user_intent == "why the call"
    # Raw, unstripped arguments plus the adapter's per-request extras.
    assert event.parameters == {"arguments": raw, "extra": {"requestId": 7}}
    assert event.response == {"content": [{"type": "text", "text": "ok"}]}
    assert event.is_error is False
    assert event.error is None
    assert event.duration == 42
    assert event.client_name == "cursor"
    assert event.client_version == "2.1"
    assert event.identify_actor_given_id == "u1"
    assert event.identify_actor_name == "Ada"
    assert event.identify_data == {"plan": "pro"}
    assert event.tags == {
        "agentcat_session_id_source": "minted",
        "agentcat_protocol_version": "2026-07-28",
    }


async def test_publish_tool_call_event_records_errors_and_mrtr(monkeypatch):
    published = capture_published(monkeypatch)
    data = make_data()
    res = HandleResolution(sid("T"), "supplied", "agt|x|1", "supplied")

    await publish_tool_call_event(
        object(),
        data,
        resolved(resolution=res),
        "search",
        {"q": "x"},
        response={"isError": True},
        is_error=True,
        error={"message": "tool exploded", "type": "ValueError", "platform": "python"},
        duration_ms=7,
        mrtr="continuation",
        extra_params=None,
    )

    event = published[0]
    assert event.is_error is True
    # Whatever the adapter captured is carried through verbatim.
    assert event.error == {
        "message": "tool exploded",
        "type": "ValueError",
        "platform": "python",
    }
    assert event.parameters == {"arguments": {"q": "x"}}
    assert event.tags == {
        "agentcat_session_id_source": "supplied",
        "agentcat_agent_id": "agt|x|1",
        "agentcat_agent_id_source": "supplied",
        "agentcat_protocol_version": "2026-07-28",
        "agentcat_mrtr": "continuation",
    }

    # An adapter that could make nothing of the failure still records an error
    # object of the same shape, not a bare flag.
    await publish_tool_call_event(
        object(), data, resolved(), "search", {}, None, True, None, 1, None, None
    )
    assert published[1].error == {
        "message": "Unknown error",
        "type": None,
        "platform": "python",
    }


# §6.5: SDK tags merge over customer tags — SDK wins on collision.
async def test_publish_tool_call_event_sdk_tags_win_on_collision(monkeypatch):
    published = capture_published(monkeypatch)
    data = make_data(
        AgentCatOptions(
            event_tags=lambda request, extra: {
                "agentcat_session_id_source": "fake",
                "env": "prod",
            },
            event_properties=lambda request, extra: {"region": "us-east-1"},
        )
    )

    await publish_tool_call_event(
        object(), data, resolved(), "search", {}, None, False, None, 1, None, None
    )

    event = published[0]
    assert event.tags["agentcat_session_id_source"] == "minted"  # SDK wins
    assert event.tags["env"] == "prod"  # customer tag survives
    assert event.properties == {"region": "us-east-1"}


# §6.5: SDK tags are exempt from the customer 50-tag cap.
async def test_publish_tool_call_event_sdk_tags_exempt_from_the_cap(monkeypatch):
    published = capture_published(monkeypatch)
    customer_tags = {f"tag_{i:02d}": str(i) for i in range(50)}
    data = make_data(
        AgentCatOptions(event_tags=lambda request, extra: dict(customer_tags))
    )

    await publish_tool_call_event(
        object(),
        data,
        resolved(
            resolution=HandleResolution(sid("T"), "minted", "agt|x|1", "supplied")
        ),
        "search",
        {},
        None,
        False,
        None,
        1,
        "input_required",
        None,
    )

    event = published[0]
    sdk_tags = {
        "agentcat_session_id_source": "minted",
        "agentcat_agent_id": "agt|x|1",
        "agentcat_agent_id_source": "supplied",
        "agentcat_protocol_version": "2026-07-28",
        "agentcat_mrtr": "input_required",
    }
    assert all(event.tags[key] == value for key, value in customer_tags.items())
    assert all(event.tags[key] == value for key, value in sdk_tags.items())
    assert len(event.tags) == 55


# The customer callbacks receive the same (request, extra) they always have.
async def test_publish_tool_call_event_hands_callbacks_request_and_extra(monkeypatch):
    capture_published(monkeypatch)
    seen: list[tuple[Any, Any]] = []

    def event_tags(request, extra):
        seen.append((request, extra))
        return {"env": "prod"}

    data = make_data(AgentCatOptions(event_tags=event_tags))
    rc = await resolve_call(
        data,
        "search",
        {},
        request="REQ",
        extra="EXTRA",
        meta_sources=[],
        legacy_client=lambda: None,
    )
    await publish_tool_call_event(
        object(), data, rc, "search", {}, None, False, None, 1, None, None
    )
    assert seen == [("REQ", "EXTRA")]


# Nothing AgentCat does may raise into the customer's server.
async def test_publish_tool_call_event_never_raises(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr("agentcat.modules.callpath.write_to_log", logged.append)

    def boom(server, event):
        raise RuntimeError("queue is gone")

    monkeypatch.setattr(event_queue, "publish_event", boom)
    data = make_data()

    await publish_tool_call_event(
        object(), data, resolved(), "search", {}, None, False, None, 1, None, None
    )
    assert any("queue is gone" in entry for entry in logged)


# An unserializable response would fail event construction; the call still
# survives it.
async def test_publish_tool_call_event_survives_a_bad_event_payload(monkeypatch):
    published = capture_published(monkeypatch)
    data = make_data()

    await publish_tool_call_event(
        object(), data, resolved(), "search", {}, ["not", "a", "dict"], False, None, 1, None, None  # noqa: E501
    )
    assert published == []


# The event timestamps when the call STARTED (TS callWrap.ts uses `startTime`),
# recovered from the duration the adapter measured.
async def test_publish_tool_call_event_timestamps_the_call_start(monkeypatch):
    published = capture_published(monkeypatch)
    data = make_data()
    before = datetime.now(timezone.utc)

    await publish_tool_call_event(
        object(), data, resolved(), "search", {}, None, False, None, 5000, None, None
    )
    await publish_tool_call_event(
        object(), data, resolved(), "search", {}, None, False, None, None, None, None
    )
    after = datetime.now(timezone.utc)

    timed, untimed = published
    assert timed.timestamp is not None and untimed.timestamp is not None
    # 5s of tool work: the call began ~5s before this publish.
    assert before - timedelta(seconds=6) <= timed.timestamp <= before - timedelta(seconds=4)  # noqa: E501
    # No measurement to subtract: publish time is the best available.
    assert before <= untimed.timestamp <= after
