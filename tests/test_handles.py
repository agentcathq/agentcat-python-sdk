"""Handle primitives: minting, derivation, extraction, mint-back, tags.

Port of the TypeScript SDK's `src/tests/handles.test.ts`. The four derivation
golden vectors and the mint-back byte expectations are frozen cross-SDK
contracts: a change here changes every previously-derived session id on the
wire and splits customer sessions across an upgrade. Fix the implementation,
never the literal.
"""

from agentcat.modules.handles import (
    HandleResolution,
    build_handle_tags,
    build_mint_back_text,
    build_structured_mint_back,
    derive_session_id,
    extract_handle,
    mirror_into_structured_content,
    new_session_id,
    resolve_handles,
)
from agentcat.types import AgentCatOptions

from .test_utils import sid

A = "opus-4.80-1m|claude-code|k3n9x"


def test_mint_shape():
    a, b = new_session_id(), new_session_id()
    assert a.startswith("ses_") and len(a) == 4 + 27 and a != b


def test_golden_vectors():
    assert derive_session_id("customer-abc", "proj_1") == "ses_2cOHEO0LYGADMzRvWTXXVbbgxgm"  # noqa: E501
    assert derive_session_id("customer-abc") == "ses_2cZY3tvyI25O2AmL2CGVo2B1IIj"
    assert derive_session_id(" x ", "p") == "ses_2c3yR5mYKQdLaXsJNgZH6erbfQK"  # no trim
    assert derive_session_id("x", "p") == "ses_2bw285VY9apdgUgTPXKFnT6P4G0"


# TS handles.test.ts:26-44 — the structural properties the golden vectors pin
# by example: derivation is deterministic, project-scoped, and works project-less.
def test_derive_is_deterministic_and_project_scoped():
    assert derive_session_id("customer-abc", "proj_1") == derive_session_id(
        "customer-abc", "proj_1"
    )
    assert derive_session_id("customer-abc", "proj_1").startswith("ses_")
    assert derive_session_id("customer-abc", "proj_1") != derive_session_id(
        "customer-abc", "proj_2"
    )
    assert derive_session_id("customer-abc") == derive_session_id("customer-abc")
    assert derive_session_id("customer-abc") != derive_session_id(
        "customer-abc", "proj_1"
    )


# extract_handle's contract is UNCHANGED by validation: it still returns any
# non-empty trimmed string. Shape checking happens afterwards, in
# resolve_handles — see test_an_unrecognized_session_id_is_never_adopted.
def test_extract_handle_returns_any_non_empty_string():
    assert extract_handle({"session_id": f" {sid('x')} "}, "session_id") == sid("x")
    assert extract_handle({"session_id": "my-own-correlation-id"}, "session_id") == "my-own-correlation-id"  # noqa: E501
    bad_values = ({"session_id": ""}, {"session_id": " "}, {"session_id": 4}, None, "s")
    for bad in bad_values:
        assert extract_handle(bad, "session_id") is None


# TS handles.test.ts:71-79 — the agent_id key and the missing-key case.
def test_extract_handle_reads_only_the_named_key():
    assert extract_handle({"agent_id": f" {A} "}, "agent_id") == A
    assert extract_handle({"agent_id": A}, "session_id") is None
    assert extract_handle({}, "session_id") is None
    assert extract_handle(None, "agent_id") is None


async def test_prompted_supplied_vs_minted():
    o = AgentCatOptions()
    r1 = await resolve_handles({"session_id": sid("supplied")}, o, "proj", None, None)
    assert (r1.session_id, r1.session_source, r1.hook_mode) == (sid("supplied"), "supplied", False)  # noqa: E501
    r2 = await resolve_handles({}, o, "proj", None, None)
    assert r2.session_source == "minted" and r2.session_id.startswith("ses_")


# TS handles.test.ts:248-271 — the agent is never minted server-side, so an
# omitted agent_id stays unresolved in both the minted and the supplied
# (subagent-continuation) flows.
async def test_prompted_mode_never_mints_an_agent_id():
    on = AgentCatOptions(enable_agent_tracking=True)
    minted = await resolve_handles({}, on, "proj_1", None, None)
    assert minted.session_source == "minted" and minted.hook_mode is False
    assert minted.agent_id is None and minted.agent_source is None

    supplied = await resolve_handles(
        {"session_id": sid("parent")}, on, "proj_1", None, None
    )
    assert (supplied.session_id, supplied.session_source) == (sid("parent"), "supplied")
    assert supplied.agent_id is None and supplied.agent_source is None


# TS handles.test.ts:273-286 — supplied handles are taken verbatim (trimmed only).
async def test_prompted_supplied_handles_are_verbatim():
    """A well-formed session_id is honored byte-for-byte; agent_id always is.

    `agent_id` is deliberately NOT shape-validated: the agent composes it
    itself (model|harness|nonce) and AgentCat never issues one, so there is no
    "did we mint this" question to ask. It also never reaches
    `Event.session_id` — it rides in tags, which the clamp already bounds.
    """
    r = await resolve_handles(
        {"session_id": sid("supplied"), "agent_id": f" {A} "},
        AgentCatOptions(enable_agent_tracking=True),
        "proj_1",
        None,
        None,
    )
    assert (r.session_id, r.session_source) == (sid("supplied"), "supplied")
    assert (r.agent_id, r.agent_source) == (A, "supplied")


async def test_hook_mode_derives_and_falls_back():
    o = AgentCatOptions(resolve_session_id=lambda req, extra: " customer-abc ")
    r = await resolve_handles({"session_id": "ignored"}, o, "proj_1", None, None)
    # trimmed by the caller
    assert r.session_id == derive_session_id("customer-abc", "proj_1")
    assert r.session_source == "hook" and r.hook_mode is True

    async def async_hook(req, extra):
        return "customer-abc"

    r = await resolve_handles(
        {}, AgentCatOptions(resolve_session_id=async_hook), "proj_1", None, None
    )
    assert r.session_source == "hook"

    def boom(req, extra):
        raise RuntimeError("hook broke")

    r = await resolve_handles(
        {}, AgentCatOptions(resolve_session_id=boom), "proj_1", None, None
    )
    assert r.session_source == "minted" and r.hook_mode is True
    r = await resolve_handles(
        {}, AgentCatOptions(resolve_session_id=lambda q, e: None), None, None, None
    )
    assert r.session_source == "minted" and r.hook_mode is True


# TS handles.test.ts:311-319 — the same hook value derives the same session id.
async def test_hook_mode_is_deterministic_across_calls():
    o = AgentCatOptions(resolve_session_id=lambda req, extra: "customer-42")
    a = await resolve_handles({}, o, "proj_1", None, None)
    b = await resolve_handles({}, o, "proj_1", None, None)
    assert a.hook_mode is True and a.session_source == "hook"
    assert a.session_id == b.session_id and a.session_id.startswith("ses_")


# TS handles.test.ts:343-361 — guards the mint-back ack line, which fires on
# session_source == "supplied". That must be unreachable in hook mode: a hook that
# returns None or raises while the agent happens to send session_id is the danger
# case, and it must fall back to "minted", never adopt the agent's value.
async def test_hook_fallback_never_reports_supplied():
    def boom(req, extra):
        raise RuntimeError("db down")

    for hook in (lambda req, extra: None, boom):
        r = await resolve_handles(
            {"session_id": sid("agent_sent")},
            AgentCatOptions(resolve_session_id=hook),
            "proj_1",
            None,
            None,
        )
        assert r.session_source == "minted" and r.hook_mode is True
        assert r.session_id != sid("agent_sent")


# TS handles.test.ts:376-392 — hook mode does not change agent resolution.
async def test_hook_mode_still_resolves_a_supplied_agent_id():
    o = AgentCatOptions(
        resolve_session_id=lambda req, extra: "c", enable_agent_tracking=True
    )
    supplied = await resolve_handles({"agent_id": A}, o, "proj_1", None, None)
    assert (supplied.agent_id, supplied.agent_source) == (A, "supplied")
    omitted = await resolve_handles({}, o, "proj_1", None, None)
    assert omitted.agent_id is None and omitted.agent_source is None


# TS handles.test.ts:394-424 — the flagship documented use reads a header off
# `extra`, so both objects must reach the hook unchanged (identity, not a copy
# or the arguments dict), and the returned header must actually drive derivation.
async def test_hook_receives_the_request_and_extra_objects():
    calls = []
    request = {"params": {"name": "add_todo", "arguments": {"session_id": sid("sent")}}}
    extra = {"request_info": {"headers": {"x-correlation-id": "corr-1"}}}

    def hook(req, ext):
        calls.append((req, ext))
        return ext["request_info"]["headers"]["x-correlation-id"]

    r = await resolve_handles(
        {"session_id": sid("sent")},
        AgentCatOptions(resolve_session_id=hook),
        "proj_1",
        request,
        extra,
    )
    assert len(calls) == 1
    assert calls[0][0] is request and calls[0][1] is extra
    assert r.session_source == "hook"
    assert r.session_id == derive_session_id("corr-1", "proj_1")


async def test_a_handle_agentcat_did_not_inject_is_never_read():
    """A parameter the customer's own schema declared is never read.

    It stayed in their handler's arguments (the strip spares it), so reading
    it here would sever the agent's real session AND route a customer-domain
    value into `session_id`, which is exempt from the customer's redaction
    hook. Ownership of `session_id` is `session_param_is_ours`; ownership of
    `agent_id` is still the injection registry.
    """
    o = AgentCatOptions(enable_agent_tracking=True)
    args = {"session_id": "SESSION-1234", "agent_id": "THEIR-AGENT"}

    theirs = await resolve_handles(
        args, o, "proj", None, None, frozenset(), session_param_is_ours=False
    )
    assert theirs.session_source == "foreign"
    # Sessionless, not a fresh mint: a mint per call on a tool that can never
    # carry our handle manufactures a phantom session per call.
    assert theirs.session_id == ""
    assert theirs.agent_id is None and theirs.agent_source is None
    # No slot of ours to echo into, so no mint-back: telling the agent to send
    # session_id=ses_… would overwrite a parameter that means something else.
    assert theirs.prompts_session_id is False
    assert build_mint_back_text(theirs) is None
    assert build_structured_mint_back(theirs) is None

    ours = await resolve_handles(
        {"session_id": sid("mine"), "agent_id": "THEIR-AGENT"},
        o,
        "proj",
        None,
        None,
        frozenset({"session_id", "agent_id"}),
    )
    assert (ours.session_id, ours.session_source) == (sid("mine"), "supplied")
    assert (ours.agent_id, ours.agent_source) == ("THEIR-AGENT", "supplied")
    assert ours.prompts_session_id is True


async def test_the_gate_is_per_handle():
    """Injecting one handle and colliding on the other is a real shape: the
    customer's schema declared `session_id` but not `agent_id`.

    Suppression is per-handle. `agent_id` is a separate injection that DID
    land in that tool's schema, so it is still ours to confirm — withholding
    it because a neighbouring parameter belongs to the customer would leave
    agents seeing agent_id confirmed on some tools and not others.
    """
    o = AgentCatOptions(enable_agent_tracking=True)
    args = {"session_id": "SESSION-1234", "agent_id": "agt|x|1"}

    r = await resolve_handles(
        args,
        o,
        "proj",
        None,
        None,
        frozenset({"agent_id"}),
        session_param_is_ours=False,
    )
    assert r.session_source == "foreign" and r.session_id == ""
    assert r.agent_id == "agt|x|1"
    # There IS something to confirm — the agent_id — but it must not carry a
    # session_id the agent has nowhere to put, and must not confirm the
    # customer's own value back to the agent.
    mint = build_structured_mint_back(r)
    assert mint is not None and "session_id" not in mint
    assert mint["agent_id"] == "agt|x|1"
    assert "SESSION-1234" not in str(mint)


async def test_a_composed_schema_tool_is_ours_to_read_but_never_prompted():
    """The one shape where the two gates disagree.

    A oneOf/allOf/anyOf schema skips the injection pass wholesale, so AgentCat
    put no `session_id` on it — but the customer declared none either, so
    nothing in the arguments belongs to them. It stays ours to correlate, and
    stays silent because there is no parameter to name.

    Deliberate divergence from the TS SDK, which emits the instruction here.
    """
    o = AgentCatOptions()
    supplied = await resolve_handles(
        {"session_id": sid("echoed")}, o, "proj", None, None, frozenset()
    )
    assert (supplied.session_id, supplied.session_source) == (sid("echoed"), "supplied")
    assert supplied.prompts_session_id is False
    assert build_mint_back_text(supplied) is None
    assert build_structured_mint_back(supplied) is None

    minted = await resolve_handles({}, o, "proj", None, None, frozenset())
    assert minted.session_source == "minted"
    assert minted.session_id.startswith("ses_")
    assert build_mint_back_text(minted) is None


async def test_no_registry_at_all_keeps_the_old_reading():
    """The degraded path (tools/call before any listing, rebuild failed) is
    where the strip removes all three names on a hunch. Handle extraction
    follows the same hunch, so the two never disagree."""
    o = AgentCatOptions()
    r = await resolve_handles({"session_id": sid("x")}, o, "proj", None, None, None)
    assert (r.session_id, r.session_source) == (sid("x"), "supplied")
    assert r.prompts_session_id is True


async def test_hook_mode_never_prompts_for_a_session_id():
    o = AgentCatOptions(resolve_session_id=lambda request, extra: "corr-1")
    r = await resolve_handles({}, o, "proj_1", None, None, frozenset())
    assert r.hook_mode is True and r.prompts_session_id is False


async def test_agent_extraction_gated_on_option():
    on = AgentCatOptions(enable_agent_tracking=True)
    r = await resolve_handles({"agent_id": " a|b|c "}, on, None, None, None)
    assert (r.agent_id, r.agent_source) == ("a|b|c", "supplied")
    off = AgentCatOptions()
    r = await resolve_handles({"agent_id": "a|b|c"}, off, None, None, None)
    assert r.agent_id is None


def test_mint_back_text_rules():
    minted = HandleResolution(session_id=sid("T"), session_source="minted")
    assert build_mint_back_text(minted) == (
        "[MCP INSTRUCTIONS]: session_id issued.\n"
        f"  session_id={sid('T')} — required on every subsequent tool call\n"
        "Without session_id, this server does not function as intended."
    )
    assert build_mint_back_text(HandleResolution(sid("T"), "supplied")) is None
    assert build_mint_back_text(HandleResolution(sid("T"), "minted", hook_mode=True)) is None  # noqa: E501


# TS handles.test.ts:91-118 — the text block is task-only; a supplied agent_id
# never appears in it (the agent is never minted, so there is nothing to announce).
def test_mint_back_text_ignores_a_supplied_agent():
    with_agent = HandleResolution(
        sid("T"), "minted", agent_id=A, agent_source="supplied"
    )
    text = build_mint_back_text(with_agent)
    assert "agent_id" not in text
    assert text == build_mint_back_text(HandleResolution(sid("T"), "minted"))


def test_structured_mint_back_omission_rules():
    both = HandleResolution(sid("T"), "supplied", agent_id="A", agent_source="supplied")
    m = build_structured_mint_back(both)
    assert m == {
        "session_id": sid("T"),
        "agent_id": "A",
        "instructions": "[MCP INSTRUCTIONS]: session_id and agent_id confirmed. Keep sending these exact values on every call.",  # noqa: E501
    }
    hook_agent = HandleResolution(
        sid("T"), "hook", agent_id="A", agent_source="supplied", hook_mode=True
    )
    m = build_structured_mint_back(hook_agent)
    assert "session_id" not in m and m["agent_id"] == "A" and "agent_id confirmed" in m["instructions"]  # noqa: E501
    assert build_structured_mint_back(HandleResolution(sid("T"), "hook", hook_mode=True)) is None  # noqa: E501
    minted = HandleResolution(sid("T"), "minted")
    assert build_structured_mint_back(minted)["instructions"].startswith("[MCP INSTRUCTIONS]: session_id issued.")  # noqa: E501


# TS handles.test.ts:428-441 — a minted task with a supplied agent echoes both
# ids but keeps the issued (not confirmed) copy, and never claims to have
# issued an agent_id.
def test_structured_mint_back_minted_task_with_supplied_agent():
    m = build_structured_mint_back(
        HandleResolution(sid("T"), "minted", agent_id=A, agent_source="supplied")
    )
    assert m["session_id"] == sid("T") and m["agent_id"] == A
    assert "session_id issued" in m["instructions"]
    assert "agent_id issued" not in m["instructions"]


# TS handles.test.ts:458-469 — agent tracking off: task only, singular copy.
def test_structured_mint_back_task_only_uses_singular_confirmed_copy():
    m = build_structured_mint_back(HandleResolution(sid("T"), "supplied"))
    assert m == {
        "session_id": sid("T"),
        "instructions": "[MCP INSTRUCTIONS]: session_id confirmed. Keep sending this exact value on every call.",  # noqa: E501
    }


def test_mirror_rules():
    mint = {"session_id": sid("T"), "instructions": "i"}
    assert mirror_into_structured_content(None, mint) is None
    assert mirror_into_structured_content([1], mint) is None
    assert mirror_into_structured_content({"_mcp_instructions": "customer"}, mint) is None  # noqa: E501
    out = mirror_into_structured_content({"a": 1}, mint)
    assert out == {"a": 1, "_mcp_instructions": mint}


# TS handles.test.ts:498-516 — customer objects are never mutated, and any
# non-plain-object structured content is left alone.
def test_mirror_never_mutates_and_skips_non_mappings():
    mint = {"session_id": sid("T"), "instructions": "i"}
    sc = {"a": 1}
    out = mirror_into_structured_content(sc, mint)
    assert sc == {"a": 1} and out is not sc
    assert mirror_into_structured_content("nope", mint) is None
    assert mirror_into_structured_content(42, mint) is None


def test_tag_clamp():
    res = HandleResolution(sid("T"), "supplied", agent_id="a\r\nb" + "x" * 300, agent_source="supplied")  # noqa: E501
    tags = build_handle_tags(res, protocol_version="2026-07-28", mrtr="continuation")
    assert tags["agentcat_session_id_source"] == "supplied"
    assert tags["agentcat_agent_id"].startswith("a  b") and len(tags["agentcat_agent_id"]) == 200  # noqa: E501
    assert tags["agentcat_agent_id_source"] == "supplied"
    assert tags["agentcat_protocol_version"] == "2026-07-28"
    assert tags["agentcat_mrtr"] == "continuation"
    # The protocol version is read off untrusted client meta and, like
    # agent_id, is merged AFTER validate_tags — so nothing else bounds it.
    hostile = build_handle_tags(res, protocol_version="2026\n" + "z" * 500)
    assert len(hostile["agentcat_protocol_version"]) == 200
    assert "\n" not in hostile["agentcat_protocol_version"]
    assert build_handle_tags(HandleResolution("s", "minted")) == {"agentcat_session_id_source": "minted"}  # noqa: E501


# TS handles.test.ts:174-240 — the full tag map for a normal agent_id, and the
# clamp/newline-strip applying to the tag copy only, never the resolution.
def test_tags_pass_a_normal_agent_id_through():
    res = HandleResolution(sid("T"), "supplied", agent_id=A, agent_source="supplied")
    assert build_handle_tags(res, protocol_version="2026-07-28") == {
        "agentcat_session_id_source": "supplied",
        "agentcat_agent_id": A,
        "agentcat_agent_id_source": "supplied",
        "agentcat_protocol_version": "2026-07-28",
    }


def test_tag_clamp_and_newline_strip_leave_the_resolution_verbatim():
    long_id = "a" * 500
    res = HandleResolution(sid("T"), "supplied", agent_id=long_id, agent_source="supplied")  # noqa: E501
    assert build_handle_tags(res)["agentcat_agent_id"] == "a" * 200
    assert res.agent_id == long_id

    multiline = "line1\nline2\r\nline3"
    res = HandleResolution(sid("T"), "supplied", agent_id=multiline, agent_source="supplied")  # noqa: E501
    assert build_handle_tags(res)["agentcat_agent_id"] == "line1 line2  line3"
    assert res.agent_id == multiline
