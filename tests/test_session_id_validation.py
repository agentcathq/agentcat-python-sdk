"""AgentCat trusts only a `session_id` it issued.

Before this, `resolve_handles` adopted whatever string arrived in
`arguments["session_id"]`, verbatim and unchecked, into `Event.session_id` —
a field in `redaction.PROTECTED_FIELDS` and therefore exempt from the
customer's redaction hook. Two failures followed, and the `task_id` ->
`session_id` rename made both materially likelier, because `session_id` is a
common parameter name on customer tools and the one most likely to hold an
auth token:

1. **Unredactable adoption.** A hallucinated value, or a token a client
   auto-populated into a parameter that happens to be named `session_id`,
   reached PostHog `$session_id`, Datadog, Sentry and OTLP with no way to
   redact it.
2. **The confirmation loop.** A customer's own `session_id` was echoed back to
   the agent as "confirmed. Keep sending this exact value on every call."
   AgentCat confirming a value it never issued.

Everything here follows from one sentence: AgentCat only trusts a session_id
it issued. Cross-SDK reference:
`agentcat-typescript-sdk/docs/superpowers/specs/2026-08-02-session-id-validation-design.md`.
"""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.constants import (
    AGENTCAT_TAG_SESSION_SOURCE,
    MINT_BACK_HEADER_INVALID,
    MINT_BACK_HEADER_SESSION,
    SESSION_ID_PARAM,
)
from agentcat.modules.handles import (
    build_mint_back_text,
    build_structured_mint_back,
    derive_session_id,
    is_valid_session_id,
    new_session_id,
    resolve_handles,
)
from agentcat.modules.injection import ToolSpec, build_injected_schemas

from .test_utils import sid
from .test_utils.flavors import flavors

# ── A. the shape predicate ───────────────────────────────────────────────────


def test_accepts_ids_this_sdk_actually_issues():
    """Both issuing paths satisfy the predicate by construction.

    If this ever fails, minted handles are being rejected as invalid and every
    conversation is severed — the loudest possible failure, deliberately
    asserted against the real minters rather than a hand-typed literal.
    """
    for _ in range(50):
        assert is_valid_session_id(new_session_id())
    assert is_valid_session_id(derive_session_id("anything", "proj_1"))
    assert is_valid_session_id(derive_session_id("anything"))
    assert is_valid_session_id(sid("parent"))


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("wrong prefix", "task_2xF9kQm3rTvB8nLpYw7ZcHd4Ke1"),
        ("no prefix", "2xF9kQm3rTvB8nLpYw7ZcHd4Ke1"),
        ("too short", "ses_abc"),
        ("one char short", "ses_" + "a" * 26),
        ("one char long", "ses_" + "a" * 28),
        ("empty", ""),
        ("prefix only", "ses_"),
        ("customer value", "my-app-session-42"),
        ("non-base62 body", "ses_" + "-" * 27),
        ("underscore body", "ses_" + "_" * 27),
        ("inner whitespace", "ses_ " + "a" * 26),
        # Python-specific: `re.match(r"...$")` would ACCEPT this, because `$`
        # also matches immediately before a final newline. `fullmatch` does
        # not, which is what keeps the predicate identical to the TS regex.
        ("trailing newline", "ses_" + "a" * 27 + "\n"),
        ("leading newline", "\nses_" + "a" * 27),
    ],
)
def test_rejects_anything_this_sdk_did_not_issue(label, value):
    assert is_valid_session_id(value) is False, label


# ── B. the decision table ────────────────────────────────────────────────────
#
# | args.session_id | ours? | shape   | Event.session_id | source   |
# | absent          | yes   | —       | new_session_id() | minted   |
# | present         | yes   | valid   | verbatim         | supplied |
# | present         | yes   | invalid | "" (sessionless) | invalid  |
# | present/absent  | no    | —       | "" (sessionless) | foreign  |


async def _resolve(arguments, *, ours=True, **options):
    return await resolve_handles(
        arguments,
        AgentCatOptions(**options),
        "proj_1",
        None,
        None,
        None,
        session_param_is_ours=ours,
    )


async def test_absent_and_ours_mints():
    r = await _resolve({})
    assert r.session_source == "minted"
    assert is_valid_session_id(r.session_id)


async def test_valid_and_ours_is_taken_verbatim():
    r = await _resolve({SESSION_ID_PARAM: sid("parent")})
    assert (r.session_source, r.session_id) == ("supplied", sid("parent"))


async def test_malformed_and_ours_publishes_sessionless():
    r = await _resolve({SESSION_ID_PARAM: "nope"})
    assert (r.session_source, r.session_id) == ("invalid", "")


async def test_the_rejected_value_is_never_stored_anywhere():
    """The whole point: `Event.session_id` cannot be redacted after the fact.

    Asserted over the entire resolution rather than one field, because any
    leak — a tag, a mint-back, a source string — lands somewhere exempt.
    """
    secret = "sk_live_51H8xQ2abcdefgHIJKLmnop"
    r = await _resolve({SESSION_ID_PARAM: secret}, enable_agent_tracking=True)
    assert r.session_id == ""
    assert secret not in repr(r)
    assert secret not in str(build_mint_back_text(r))
    assert secret not in str(build_structured_mint_back(r))


@pytest.mark.parametrize("arguments", [{}, {SESSION_ID_PARAM: "customer-value"}])
async def test_a_foreign_param_is_sessionless_whatever_the_agent_sent(arguments):
    r = await _resolve(arguments, ours=False)
    assert (r.session_source, r.session_id) == ("foreign", "")


async def test_hook_mode_wins_over_foreign_and_reads_no_arguments():
    """Hook mode short-circuits before any argument is touched.

    That is what makes `resolve_session_id` the documented remedy for a
    collision: the customer's parameter stays entirely theirs while AgentCat
    derives its own session from their identifier.
    """
    r = await _resolve(
        {SESSION_ID_PARAM: "customer-value"},
        ours=False,
        resolve_session_id=lambda request, extra: "corr-7",
    )
    assert r.session_source == "hook"
    assert r.session_id == derive_session_id("corr-7", "proj_1")


async def test_a_missing_registry_still_validates():
    """`tools/call` before any `tools/list` on this instance.

    Nothing is in `declared_session_params` yet, so the tool counts as ours
    and the value is validated rather than adopted. A customer's foreign value
    in that window degrades to `invalid` instead of `foreign` — both
    sessionless, only the tag differs.
    """
    r = await _resolve({SESSION_ID_PARAM: "TICKET-77"})
    assert (r.session_source, r.session_id) == ("invalid", "")


# ── C. what the agent is told ────────────────────────────────────────────────


async def test_invalid_corrects_the_agent_without_issuing_a_replacement():
    r = await _resolve({SESSION_ID_PARAM: "nope"})
    text = build_mint_back_text(r)
    assert text.startswith(MINT_BACK_HEADER_INVALID)
    assert "Re-send the exact session_id" in text
    assert "omit the parameter and one will be issued" in text
    # Nothing that looks like an issued ID appears — this branch corrects, it
    # does not mint. Handing out a second ID would split a session that was
    # never split.
    assert "ses_" not in text
    assert MINT_BACK_HEADER_SESSION not in text


async def test_invalid_mirror_carries_instructions_but_no_session_id():
    """The regression `not names: return None` would cause.

    With no agent_id in play there is nothing echoable, so the old early
    return dropped the correction entirely — the one branch that has something
    to say and nothing to confirm.
    """
    r = await _resolve({SESSION_ID_PARAM: "nope"})
    mint = build_structured_mint_back(r)
    assert mint is not None
    assert SESSION_ID_PARAM not in mint
    assert "not recognized" in mint["instructions"]


async def test_foreign_says_nothing_about_session_id_at_all():
    r = await _resolve({SESSION_ID_PARAM: "customer-value"}, ours=False)
    assert build_mint_back_text(r) is None
    assert build_structured_mint_back(r) is None


async def test_foreign_never_confirms_a_value_agentcat_did_not_issue():
    """The confirmation loop, pinned.

    The bug was `mint_back_confirmed` telling the agent its own
    customer-semantics value was "confirmed. Keep sending this exact value on
    every call."
    """
    r = await _resolve(
        {SESSION_ID_PARAM: "customer-value", "agent_id": "opus|cc|k3n9x"},
        ours=False,
        enable_agent_tracking=True,
    )
    mint = build_structured_mint_back(r)
    # agent_id is a separate injection and still landed, so it is still ours
    # to confirm — suppression is per handle, not per response.
    assert mint == {
        "agent_id": "opus|cc|k3n9x",
        "instructions": (
            "[MCP INSTRUCTIONS]: agent_id confirmed. "
            "Keep sending this exact value on every call."
        ),
    }
    assert "customer-value" not in str(mint)


# ── D. what the customer is told ─────────────────────────────────────────────


@pytest.fixture
def log_sink():
    """Everything `write_to_log` tees to diagnostics, as the collector sees it."""
    from agentcat.modules import logging as agentcat_logging

    previous = agentcat_logging._diagnostics_sink
    lines: list[str] = []
    agentcat_logging.set_diagnostics_sink(lines.append)
    yield lines
    agentcat_logging.set_diagnostics_sink(previous)


def _colliding(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        input_schema={
            "type": "object",
            "properties": {SESSION_ID_PARAM: {"type": "string"}},
        },
    )


def test_the_collision_is_an_error_with_remediation(log_sink):
    build_injected_schemas([_colliding("own_session")], AgentCatOptions(), set())
    (line,) = [ln for ln in log_sink if "own_session" in ln]
    # `write_to_log` has no severity argument; the convention is a text prefix
    # on the message, which follows the timestamp bracket.
    assert line.split("] ", 1)[1].startswith("ERROR:")
    assert "WARN:" not in line
    assert "resolve_session_id" in line
    assert "without a session" in line
    assert "still reaches your handler" in line


def test_the_collision_is_reported_once_per_tool_not_once_per_listing(log_sink):
    """`build_injected_schemas` reruns on every `tools/list`.

    Undeduped, this would repeat for the life of the process — which is how a
    real signal becomes noise the customer filters out.
    """
    reported: set[str] = set()
    options = AgentCatOptions()
    for _ in range(3):
        build_injected_schemas([_colliding("own_session")], options, reported)

    assert len([ln for ln in log_sink if "own_session" in ln and "ERROR" in ln]) == 1

    # Still once per DISTINCT tool, though.
    build_injected_schemas([_colliding("other_tool")], options, reported)
    assert len([ln for ln in log_sink if "other_tool" in ln and "ERROR" in ln]) == 1


def test_ownership_is_recorded_so_the_call_path_can_read_it():
    """Per tool, not per server: the colliding tool and a normal one coexist."""
    theirs = _colliding("own_session")
    result = build_injected_schemas(
        [theirs, ToolSpec(name="echo", input_schema={})], AgentCatOptions()
    )
    assert result.declared_session_params == {"own_session"}
    # Nothing was injected over their parameter, and nothing was recorded as
    # strippable — so their handler still receives it.
    assert SESSION_ID_PARAM not in result.injected_params["own_session"]
    assert theirs.input_schema["properties"][SESSION_ID_PARAM] == {"type": "string"}
    assert SESSION_ID_PARAM in result.injected_params["echo"]


def test_a_composed_schema_declaring_session_id_is_still_the_customers():
    """Injection is skipped for oneOf/allOf/anyOf, but ownership is not.

    Only the root properties bag is visible here — a `session_id` nested
    inside a branch is unreachable, the same limitation the injection has.
    """
    composed = ToolSpec(
        name="composed_own",
        input_schema={
            "oneOf": [{"type": "object"}],
            "properties": {SESSION_ID_PARAM: {"type": "string"}},
        },
    )
    result = build_injected_schemas([composed], AgentCatOptions())
    assert result.declared_session_params == {"composed_own"}


def test_a_composed_schema_without_the_name_stays_ours():
    """The bug a naive port of the TS ownership test would introduce.

    A composed schema has an injection entry that stays EMPTY, because the
    whole pass is skipped. Reading ownership off that emptiness would call
    every such tool the customer's and publish it sessionless, when nobody
    declared the name at all.
    """
    composed = ToolSpec(
        name="composed_plain",
        input_schema={"oneOf": [{"type": "object"}], "properties": {}},
    )
    result = build_injected_schemas([composed], AgentCatOptions())
    assert result.injected_params["composed_plain"] == set()
    assert result.declared_session_params == set()


# ── E. end to end, on every server shape ─────────────────────────────────────


@pytest.fixture
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_an_invalid_id_publishes_sessionless_and_corrects_the_agent(
    flavor, capture
):
    """The whole path, over each flavor's real client and transport.

    The unit tests above can only prove the decision; this proves the decision
    reaches both consumers — the event AgentCat publishes and the result the
    agent is handed.
    """
    built = flavor.build("invalid-session")
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        result = await flavor.call(
            client, "echo", {"text": "hi", SESSION_ID_PARAM: "not-a-real-id"}
        )

    assert "session_id not recognized" in result.text
    assert "not-a-real-id" not in result.text

    (event,) = capture
    assert event.session_id is None
    assert event.tags[AGENTCAT_TAG_SESSION_SOURCE] == "invalid"
    # The rejected value is nowhere near the unredactable field, but it IS
    # still on the event as an argument the agent sent — which is `parameters`,
    # where the customer's redaction hook can reach it.
    assert event.parameters["arguments"][SESSION_ID_PARAM] == "not-a-real-id"


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_the_correction_lets_an_agent_recover_by_omitting_the_parameter(
    flavor, capture
):
    """The deadlock the closing sentence of the copy exists to prevent.

    An agent that hallucinates a session_id on its FIRST call was never issued
    one, so "re-send what you were given" names nothing. Omitting the
    parameter has to put it back on the minting path — otherwise the
    conversation can never acquire a session at all.
    """
    built = flavor.build("invalid-recovery")
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        await flavor.call(client, "echo", {"text": "a", SESSION_ID_PARAM: "guessed"})
        recovered = await flavor.call(client, "echo", {"text": "b"})

    assert MINT_BACK_HEADER_SESSION in recovered.text
    issued = recovered.structured["_mcp_instructions"][SESSION_ID_PARAM]
    assert is_valid_session_id(issued)

    rejected, minted = capture
    assert rejected.session_id is None
    assert minted.session_id == issued
    assert minted.tags[AGENTCAT_TAG_SESSION_SOURCE] == "minted"


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_the_conflict_report_is_deduped_per_server_not_per_listing(
    flavor, log_sink
):
    """The dedupe set has to reach the pipeline from `AgentCatData`.

    The unit test above proves `build_injected_schemas` honors a set it is
    handed; only a real server proves each adapter actually hands it one. Wire
    it wrong and the customer gets this error on every `tools/list` for the
    life of the process.
    """
    built = flavor.build("dedupe", customer_session_id=True)
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        for _ in range(3):
            await flavor.list_tools(client)

    errors = [ln for ln in log_sink if "ERROR:" in ln and "complete_task" in ln]
    assert len(errors) == 1, f"reported {len(errors)} times across 3 listings"


def test_hook_mode_never_reports_a_collision(log_sink):
    """Nothing is injected in hook mode, so nothing can collide.

    Reporting here would tell customers who already took the documented remedy
    that they still have the problem.
    """
    build_injected_schemas(
        [_colliding("own_session")],
        AgentCatOptions(resolve_session_id=lambda request, extra: "corr-1"),
        set(),
    )
    assert not [ln for ln in log_sink if "own_session" in ln]
