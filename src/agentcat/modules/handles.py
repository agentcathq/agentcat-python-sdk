"""Explicit-handle primitives: minting, derivation, extraction, mint-back.

Cross-SDK contract: 2026-07-28-cross-sdk-changelog.md §3-§4; TS reference
src/modules/handles.ts. Derivation golden vectors are frozen — changing them
splits customer sessions across an upgrade.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from agentcat.modules.constants import (
    AGENT_ID_PARAM,
    AGENTCAT_TAG_AGENT_ID,
    AGENTCAT_TAG_AGENT_SOURCE,
    AGENTCAT_TAG_MRTR,
    AGENTCAT_TAG_PROTOCOL_VERSION,
    AGENTCAT_TAG_SESSION_SOURCE,
    MCP_INSTRUCTIONS_KEY,
    MINT_BACK_CLOSER,
    MINT_BACK_HEADER_INVALID,
    MINT_BACK_HEADER_SESSION,
    MINT_BACK_INVALID_LINE,
    SESSION_ID_PARAM,
    SESSION_ID_PREFIX,
    mint_back_confirmed,
    mint_back_session_line,
)
from agentcat.modules.hooks import run_hook
from agentcat.modules.logging import write_to_log
from agentcat.thirdparty.ksuid import Ksuid
from agentcat.types import AgentCatOptions
from agentcat.utils import generate_prefixed_ksuid

_KSUID_EPOCH_MS = 1_400_000_000_000
_DERIVE_EPOCH_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
_YEAR_MS = 365 * 24 * 60 * 60 * 1000

# 27 is the KSUID string length; `ses_` is the prefix both issuing paths use.
_SESSION_ID_RE = re.compile(rf"{SESSION_ID_PREFIX}_[0-9A-Za-z]{{27}}")

SessionSource = Literal["supplied", "minted", "hook", "invalid", "foreign"]


@dataclass
class HandleResolution:
    # Empty string means sessionless — the event publishes with no session at
    # all, which is what `invalid` and `foreign` both resolve to. The wire
    # boundary turns it into null; nothing downstream sees "".
    session_id: str
    session_source: SessionSource
    agent_id: str | None = None
    agent_source: Literal["supplied"] | None = None
    hook_mode: bool = False
    # Whether AgentCat put its own `session_id` parameter on THIS tool. False
    # in three cases: hook mode (none is injected anywhere), a tool whose
    # customer schema already declared the name, and a composed
    # (oneOf/allOf/anyOf) schema the injection pass skipped wholesale. Gates
    # the mint-back, because a mint-back is an instruction to send
    # `session_id=ses_…` on the next call — and if the tool has no such
    # parameter, that instruction names a slot the agent cannot fill, or worse,
    # one that belongs to the customer's domain. Telling an agent to overwrite
    # it would change what the customer's tool does, which nothing AgentCat
    # does may do.
    #
    # Deliberately NOT the same question as "may we READ the argument" — see
    # `session_param_is_ours` on `resolve_handles`. They differ on the composed
    # schema, which is ours to read but has no parameter to prompt for.
    prompts_session_id: bool = True


def is_valid_session_id(value: str) -> bool:
    """True only for a session ID this SDK issued.

    Both issuing paths — `new_session_id` and `derive_session_id` — satisfy
    this by construction, so a value that fails was invented by the agent or
    belongs to someone else.

    `fullmatch`, not `match` against a trailing `$`: Python's `$` also matches
    immediately BEFORE a final newline, so the literal transcription of the TS
    regex would accept `"ses_" + "a" * 27 + "\\n"` that TypeScript rejects.
    """
    return _SESSION_ID_RE.fullmatch(value) is not None


def new_session_id() -> str:
    return generate_prefixed_ksuid(SESSION_ID_PREFIX)


def derive_session_id(id: str, project_id: str | None = None) -> str:
    # NOTE: does not trim — callers trim (resolve_handles trims hook output).
    payload_input = f"{id}:{project_id}" if project_id else id
    digest = hashlib.sha256(payload_input.encode("utf-8")).digest()
    ts_ms = _DERIVE_EPOCH_MS + (int.from_bytes(digest[0:4], "big") % _YEAR_MS)
    ts_field = (ts_ms - _KSUID_EPOCH_MS) // 1000
    raw = ts_field.to_bytes(4, "big") + digest[4:20]
    return f"{SESSION_ID_PREFIX}_{Ksuid.from_bytes(raw)}"


def extract_handle(arguments: Any, name: str) -> str | None:
    if not isinstance(arguments, dict):
        return None
    value = arguments.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


async def resolve_handles(
    arguments: Any,
    options: AgentCatOptions,
    project_id: str | None,
    request: Any,
    extra: Any,
    injected: frozenset[str] | None = None,
    session_param_is_ours: bool = True,
) -> HandleResolution:
    """Resolve this call's session and agent handles. Never raises.

    Two independent questions, deliberately not the same gate:

    `session_param_is_ours` — may we READ `arguments["session_id"]`? False only
    when the customer's own schema declared that name, which `callpath` reads
    off `AgentCatData.declared_session_params`. Reading a parameter they own
    would sever the agent's real session AND route a customer-domain
    identifier into `Event.session_id`, a field the customer's redaction hook
    is not allowed to touch.

    `injected` — which parameter names did AgentCat actually put on THIS tool
    (`injection.injected_parameter_names`)? None means "assume ours", the same
    fallback the strip takes when no registry exists. It drives `agent_id`
    reads and `prompts_session_id`, the mint-back gate.

    They diverge on exactly one shape: a composed (oneOf/allOf/anyOf) schema,
    where AgentCat injected nothing but the customer declared nothing either.
    That tool is ours to read — an echoed valid ID still correlates — but has
    no parameter to prompt for, so it gets no mint-back.
    """
    hook = options.resolve_session_id
    prompts_session_id = injected is None or SESSION_ID_PARAM in injected
    ours_agent_id = injected is None or AGENT_ID_PARAM in injected
    agent_id = (
        extract_handle(arguments, AGENT_ID_PARAM)
        if options.enable_agent_tracking and ours_agent_id
        else None
    )
    agent_source: Literal["supplied"] | None = "supplied" if agent_id else None

    if callable(hook):
        try:
            value = await run_hook(hook, "resolve_session_id", request, extra)
        except Exception as e:  # hook errors mint silently
            write_to_log(f"Warning: resolve_session_id hook raised: {e}")
            value = None
        if isinstance(value, str) and value.strip():
            return HandleResolution(
                derive_session_id(value.strip(), project_id),
                "hook",
                agent_id,
                agent_source,
                hook_mode=True,
                prompts_session_id=False,
            )
        return HandleResolution(
            new_session_id(),
            "minted",
            agent_id,
            agent_source,
            hook_mode=True,
            prompts_session_id=False,
        )

    if not session_param_is_ours:
        # The tool declares its own `session_id`. Nothing in the arguments is
        # ours to read, and minting one per call would manufacture a phantom
        # session per call — noise shaped like data. Sessionless is the honest
        # signal, and it resolves the moment the customer adopts
        # `resolve_session_id`: hook mode reads no arguments at all, so their
        # parameter stays entirely theirs.
        return HandleResolution(
            "", "foreign", agent_id, agent_source, prompts_session_id=False
        )

    supplied = extract_handle(arguments, SESSION_ID_PARAM)
    if supplied:
        if is_valid_session_id(supplied):
            return HandleResolution(
                supplied,
                "supplied",
                agent_id,
                agent_source,
                prompts_session_id=prompts_session_id,
            )
        # Not an ID this server issued. Publish sessionless rather than adopt
        # it: `Event.session_id` is exempt from both redaction hooks, so an
        # agent's hallucination — or an auth token a client auto-populated
        # into a parameter that happens to be named `session_id` — would reach
        # every downstream exporter unredactable.
        return HandleResolution(
            "", "invalid", agent_id, agent_source, prompts_session_id=prompts_session_id
        )
    return HandleResolution(
        new_session_id(),
        "minted",
        agent_id,
        agent_source,
        prompts_session_id=prompts_session_id,
    )


def _echoes_session_id(res: HandleResolution) -> bool:
    """Whether the agent has an AgentCat `session_id` value to echo back.

    Three ways to have none: hook mode and the no-parameter cases collapsed
    into `prompts_session_id`, plus `invalid` — where the parameter is ours
    but there is no value to confirm. That branch corrects the agent rather
    than issuing a replacement, so naming a `session_id` would be a lie.
    """
    return (
        res.prompts_session_id and not res.hook_mode and res.session_source != "invalid"
    )


def build_mint_back_text(res: HandleResolution) -> str | None:
    if res.hook_mode or not res.prompts_session_id:
        return None
    if res.session_source == "minted":
        return "\n".join(
            [
                MINT_BACK_HEADER_SESSION,
                mint_back_session_line(res.session_id),
                MINT_BACK_CLOSER,
            ]
        )
    if res.session_source == "invalid":
        # No replacement is handed out. An agent that sent something was
        # usually already issued a good ID, and giving it a second one splits a
        # session that was never split. The closing sentence of
        # MINT_BACK_INVALID_LINE is the way out for the agent that was never
        # issued one: omit the parameter and take the `minted` branch.
        return "\n".join(
            [MINT_BACK_HEADER_INVALID, MINT_BACK_INVALID_LINE, MINT_BACK_CLOSER]
        )
    return None


def build_structured_mint_back(res: HandleResolution) -> dict[str, Any] | None:
    """The persistent handle state mirrored into `structuredContent`.

    Unlike `build_mint_back_text` (mint announcements only), this is present
    on EVERY response, so an agent can re-read its own handles mid-session.
    Handles the agent cannot echo are never named.

    Suppression is per-HANDLE, not per-response: a `session_id` collision skips
    only `session_id`. `agent_id` is a separate injection and still landed in
    that tool's schema, so it is still ours to confirm. Dropping the whole
    mirror would withhold a handle AgentCat issued purely because a
    neighbouring one belongs to the customer.
    """
    echoes = _echoes_session_id(res)
    names: list[str] = []
    if echoes:
        names.append(SESSION_ID_PARAM)
    if res.agent_id:
        names.append(AGENT_ID_PARAM)
    text = build_mint_back_text(res)
    # `not names` alone would drop the `invalid` correction whenever no
    # agent_id is in play — the one branch that has something to say and
    # nothing to echo.
    if not names and not text:
        return None
    mint: dict[str, Any] = {}
    if echoes:
        mint[SESSION_ID_PARAM] = res.session_id
    if res.agent_id:
        mint[AGENT_ID_PARAM] = res.agent_id
    mint["instructions"] = text or mint_back_confirmed(names)
    return mint


def mirror_into_structured_content(
    sc: Any, mint: dict[str, Any]
) -> dict[str, Any] | None:
    if not isinstance(sc, dict) or MCP_INSTRUCTIONS_KEY in sc:
        return None
    return {**sc, MCP_INSTRUCTIONS_KEY: mint}


def _clamp_tag_value(value: str) -> str:
    """The value rule `validate_tags` enforces, applied to an SDK tag.

    SDK tags are merged AFTER `validate_tags` — deliberately, so they win on
    collision and ride outside the customer's 50-tag cap (§6.5) — which also
    means nothing else checks them. Both values here come from the CLIENT:
    `agent_id` is whatever the agent typed into a string parameter, and the
    protocol version is read off untrusted request meta. Neither may put an
    unbounded, newline-bearing string on the wire.
    """
    return value.replace("\r", " ").replace("\n", " ")[:200]


def build_handle_tags(
    res: HandleResolution,
    protocol_version: str | None = None,
    mrtr: str | None = None,
) -> dict[str, str]:
    tags: dict[str, str] = {AGENTCAT_TAG_SESSION_SOURCE: res.session_source}
    if res.agent_id and res.agent_source:
        tags[AGENTCAT_TAG_AGENT_ID] = _clamp_tag_value(res.agent_id)
        tags[AGENTCAT_TAG_AGENT_SOURCE] = res.agent_source
    if protocol_version:
        tags[AGENTCAT_TAG_PROTOCOL_VERSION] = _clamp_tag_value(protocol_version)
    if mrtr:
        tags[AGENTCAT_TAG_MRTR] = mrtr
    return tags
