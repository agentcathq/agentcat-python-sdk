"""Byte-parity guard for agent-facing copy.

The TypeScript SDK is the single source of truth for every agent-facing string:
`agentcat-typescript-sdk/src/modules/constants.ts` (and `tools.ts` for the
get_more_tools copy). The `TS_*` literals below were copied verbatim from those
files. If one of these tests fails the SDKs have drifted -- fix the copy; do not
edit the expected literal unless the TypeScript source changed too.
"""

import ast
import re
from pathlib import Path

from agentcat.modules import constants as c
from agentcat.modules import tools as t

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "agentcat"

# ── Expected bytes, copied verbatim from TS constants.ts ────────────────────
TS_SESSION_ID_PARAM_DESCRIPTION = "Session continuity handle, one of two values: the ses_ ID issued for the task underway, or start. This server cannot link your calls between requests on its own, so session continuity travels in this parameter instead. If you were handed a session_id for this task — for example by the agent that spawned you — send that exact value from your first call. Otherwise send start on your first call; the server will issue an opaque correlation ID in the mcp_session field of the result, or in a text block at the start of the result beginning [session_id issued. Then send that exact ses_ value on every later call and hand it to any subagents working the same task. start always begins a new, unrelated task — never send it mid-task. If you send a value this server does not recognize, the result reports it: mcp_session.status of unrecognized, or a text block beginning [session_id unrecognized; re-send the ID issued for this task, or start if none was issued yet. Never invent a ses_ value."  # noqa: E501
TS_SESSION_ID_PARAM_PATTERN = "^(start|ses_[0-9A-Za-z]{27})$"
TS_SESSION_START_SENTINEL = "start"
TS_AGENT_ID_PARAM_DESCRIPTION = "Agent identity handle, required on every call including your first. This server cannot tell concurrent agents apart on its own; agent_id is how your calls are attributed to you. It is a self-chosen identifier in the spirit of a User-Agent string — join your model version, your harness name, and a short suffix of 4-6 letters or digits, with '|'. Example: opus-4.80-1m|claude-code|k3n9x. Choose the suffix once at the start of your task and send that same exact value on every call for the entire task; never change it mid-task, and a new task gets a fresh suffix. agent_id identifies exactly one agent and is never inherited: a subagent you spawn generates a new one rather than carrying yours, and if you were spawned by another agent, generate your own rather than reusing your parent's. A call without agent_id cannot be attributed to you."  # noqa: E501
TS_MINT_BACK_HEADER_ISSUED = "[session_id issued — see this tool's session_id parameter description]"  # noqa: E501
TS_MINT_BACK_ISSUED_BODY = "This is the first-call issuance described in this tool's session_id parameter description."  # noqa: E501
TS_MINT_BACK_HEADER_UNRECOGNIZED = "[session_id unrecognized — see this tool's session_id parameter description]"  # noqa: E501
TS_MINT_BACK_UNRECOGNIZED_BODY = "The value sent was not issued by this server. Re-send the session_id issued earlier for this task; if none was issued yet, send start and one will be issued."  # noqa: E501
TS_MCP_SESSION_FIELD_DESCRIPTION = "Session continuity and agent attribution state for this task, returned on completed responses that carry structured output. This server cannot link your calls between requests on its own, so session continuity travels here instead."  # noqa: E501
TS_MCP_SESSION_FIELD_DESCRIPTION_HOOK_MODE = "Agent attribution state for this task, returned on completed responses that carry structured output."  # noqa: E501
TS_MCP_SESSION_SESSION_ID_DESCRIPTION = "Opaque correlation ID for this task, issued by this server. Use this as the session_id argument of every later call, and hand it to any subagents working the same task. Absent when status is unrecognized; no replacement is issued in that response — recovery is described under status."  # noqa: E501
TS_MCP_SESSION_AGENT_ID_DESCRIPTION = "Present only when you sent agent_id on this call. Your agent_id, echoed as received. Continue sending this exact value on every call; it is never inherited — a subagent you spawn generates its own."  # noqa: E501
TS_MCP_SESSION_STATUS_DESCRIPTION = "issued: first call of a task; the session_id above was just created. active: the session_id you sent was accepted; keep sending it. unrecognized: the value sent was not issued by this server — re-send the one issued earlier for this task; if none was issued yet, send start to be issued a new one."  # noqa: E501
TS_DEFAULT_CONTEXT_PARAMETER_DESCRIPTION = 'Explain why you are calling this tool and how it fits into the user\'s overall goal. This parameter is used for analytics and user intent tracking. YOU MUST provide 15-25 words (count carefully). NEVER use first person (\'I\', \'we\', \'you\') - maintain third-person perspective. NEVER include sensitive information such as credentials, passwords, or personal data. Example (20 words): "Searching across the organization\'s repositories to find all open issues related to performance complaints and latency issues for team prioritization."'  # noqa: E501

# ── Expected bytes, copied verbatim from TS tools.ts ────────────────────────
TS_GET_MORE_TOOLS_DESCRIPTION = "Check for additional tools whenever your task might benefit from specialized capabilities - even if existing tools could work as a fallback."  # noqa: E501
TS_GET_MORE_TOOLS_CONTEXT_DESCRIPTION = "A description of your goal and what kind of tool would help accomplish it."  # noqa: E501
TS_REPORT_MISSING_RESPONSE_TEXT = "Unfortunately, we have shown you the full tool list. We have noted your feedback and will work to improve the tool list in the future."  # noqa: E501


def test_param_names_and_keys():
    assert c.SESSION_ID_PARAM == "session_id"
    assert c.AGENT_ID_PARAM == "agent_id"
    assert c.CONTEXT_PARAM == "context"
    assert c.GET_MORE_TOOLS_NAME == "get_more_tools"
    assert c.MCP_SESSION_KEY == "mcp_session"
    assert c.META_CLIENT_INFO_KEY == "io.modelcontextprotocol/clientInfo"
    assert c.META_PROTOCOL_VERSION_KEY == "io.modelcontextprotocol/protocolVersion"
    assert c.AGENTCAT_TAG_SESSION_SOURCE == "agentcat_session_id_source"
    assert c.AGENTCAT_TAG_AGENT_ID == "agentcat_agent_id"
    assert c.AGENTCAT_TAG_AGENT_SOURCE == "agentcat_agent_id_source"
    assert c.AGENTCAT_TAG_PROTOCOL_VERSION == "agentcat_protocol_version"
    assert c.AGENTCAT_TAG_MRTR == "agentcat_mrtr"
    assert c.AGENTCAT_CUSTOM_EVENT_TYPE == "agentcat:custom"
    assert c.AGENT_ID_PREFIX == "agt"


def test_mint_back_assembly():
    assert c.MINT_BACK_HEADER_ISSUED == TS_MINT_BACK_HEADER_ISSUED
    assert c.MINT_BACK_ISSUED_BODY == TS_MINT_BACK_ISSUED_BODY
    assert c.mint_back_session_line("ses_X") == "session_id: ses_X"


def test_headers_match_the_param_description_promise():
    """The param description promises a text block "beginning [session_id
    issued" / "beginning [session_id unrecognized"; the headers must keep
    those prefixes or the promise breaks."""
    assert c.MINT_BACK_HEADER_ISSUED.startswith("[session_id issued")
    assert c.MINT_BACK_HEADER_UNRECOGNIZED.startswith("[session_id unrecognized")
    assert "[session_id issued" in c.SESSION_ID_PARAM_DESCRIPTION
    assert "[session_id unrecognized" in c.SESSION_ID_PARAM_DESCRIPTION


def test_unrecognized_correction_copy_matches_ts():
    """The `unrecognized` branch corrects the agent without issuing a
    replacement.

    The closing sentence is load-bearing: an agent that invented a session_id
    on its FIRST call was never issued one, so "re-send what you were given"
    names a value that does not exist. Sending `start` puts it back on the
    `minted` path.
    """
    assert c.MINT_BACK_HEADER_UNRECOGNIZED == TS_MINT_BACK_HEADER_UNRECOGNIZED
    assert c.MINT_BACK_UNRECOGNIZED_BODY == TS_MINT_BACK_UNRECOGNIZED_BODY
    assert c.MINT_BACK_UNRECOGNIZED_BODY.endswith(
        "send start and one will be issued."
    )
    # No value is handed out anywhere in the correction.
    correction = c.MINT_BACK_HEADER_UNRECOGNIZED + c.MINT_BACK_UNRECOGNIZED_BODY
    assert "ses_" not in correction


def test_param_descriptions_match_ts():
    assert c.SESSION_ID_PARAM_DESCRIPTION == TS_SESSION_ID_PARAM_DESCRIPTION
    assert c.SESSION_ID_PARAM_DESCRIPTION.startswith(
        "Session continuity handle, one of two values:"
    )
    assert c.SESSION_ID_PARAM_DESCRIPTION.endswith("Never invent a ses_ value.")
    assert c.AGENT_ID_PARAM_DESCRIPTION == TS_AGENT_ID_PARAM_DESCRIPTION
    assert c.AGENT_ID_PARAM_DESCRIPTION.startswith("Agent identity handle,")
    assert c.AGENT_ID_PARAM_DESCRIPTION.endswith(
        "A call without agent_id cannot be attributed to you."
    )


def test_session_value_contract_constants_match_ts():
    """The v4 value contract: session_id is `start` or an ID this SDK issued.

    The schema pattern must equal the issued-ID shape with the `start`
    alternative added — never looser — and the sentinel must be the exact
    spelling the parameter description and the mint-back bodies tell agents
    to send. The pattern itself is strict lowercase `start`; the lenient
    case-insensitive reading lives in resolution, not in the schema.
    """
    assert c.SESSION_ID_PARAM_PATTERN == TS_SESSION_ID_PARAM_PATTERN
    assert c.SESSION_START_SENTINEL == TS_SESSION_START_SENTINEL
    accepted = re.compile(c.SESSION_ID_PARAM_PATTERN)
    assert accepted.fullmatch(c.SESSION_START_SENTINEL)
    assert accepted.fullmatch("ses_2cOHEO0LYGADMzRvWTXXVbbgxgm")
    for rejected in ("Start", " start", "ses_" + "a" * 26, "ses_" + "a" * 28, ""):
        assert not accepted.fullmatch(rejected), rejected
    # The copy that tells agents what to send names both alternatives.
    assert ", or start." in c.SESSION_ID_PARAM_DESCRIPTION
    assert "send start on your first call" in c.SESSION_ID_PARAM_DESCRIPTION
    assert "send start and one will be issued." in c.MINT_BACK_UNRECOGNIZED_BODY
    assert "send start to be issued a new one." in c.MCP_SESSION_STATUS_DESCRIPTION




def test_mcp_session_descriptions_match_ts():
    assert c.MCP_SESSION_FIELD_DESCRIPTION == TS_MCP_SESSION_FIELD_DESCRIPTION
    assert (
        c.MCP_SESSION_FIELD_DESCRIPTION_HOOK_MODE
        == TS_MCP_SESSION_FIELD_DESCRIPTION_HOOK_MODE
    )
    assert (
        c.MCP_SESSION_SESSION_ID_DESCRIPTION == TS_MCP_SESSION_SESSION_ID_DESCRIPTION
    )
    assert c.MCP_SESSION_AGENT_ID_DESCRIPTION == TS_MCP_SESSION_AGENT_ID_DESCRIPTION
    assert c.MCP_SESSION_STATUS_DESCRIPTION == TS_MCP_SESSION_STATUS_DESCRIPTION
    # Every response state is pre-announced in the schema copy.
    for state in ("issued", "active", "unrecognized"):
        assert state in c.MCP_SESSION_STATUS_DESCRIPTION


def test_existing_context_description_unchanged():
    """v1 copy must survive the v2 upgrade byte-for-byte."""
    assert c.DEFAULT_CONTEXT_DESCRIPTION == TS_DEFAULT_CONTEXT_PARAMETER_DESCRIPTION


async def test_get_more_tools_copy_unchanged():
    """v1 get_more_tools copy must survive the v2 upgrade byte-for-byte."""
    assert t.GET_MORE_TOOLS_DESCRIPTION == TS_GET_MORE_TOOLS_DESCRIPTION
    context_schema = t.GET_MORE_TOOLS_SCHEMA["properties"]["context"]
    assert context_schema["description"] == TS_GET_MORE_TOOLS_CONTEXT_DESCRIPTION
    assert t.REPORT_MISSING_RESPONSE_TEXT == TS_REPORT_MISSING_RESPONSE_TEXT
    result = await t.handle_report_missing({"context": "why"})
    assert result.content[0].text == TS_REPORT_MISSING_RESPONSE_TEXT


def _declared_descriptions() -> list[tuple[str, int, str]]:
    """Every literal `description=` / `"description":` string under src/agentcat.

    Adjacent string literals are joined by the parser, so implicitly
    concatenated copy is compared as the single string an agent would see.
    """
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            values = []
            if isinstance(node, ast.keyword) and node.arg == "description":
                values.append(node.value)
            elif isinstance(node, ast.Dict):
                values.extend(
                    v
                    for k, v in zip(node.keys, node.values, strict=True)
                    if isinstance(k, ast.Constant) and k.value == "description"
                )
            for value in values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    name = str(path.relative_to(SRC_ROOT))
                    found.append((name, value.lineno, value.value))
    return found


def test_no_adapter_ships_a_second_copy_of_get_more_tools():
    """Any inline get_more_tools copy an adapter grows must match the TS bytes.

    v2 has one home for this copy — `modules/tools.py` — and every adapter
    registers from there. The context description is still a literal the walk
    can see (the `"description"` key inside GET_MORE_TOOLS_SCHEMA), so its
    presence is still asserted; the tool description is now a named constant
    the walk cannot reach, which is why only that one lost its presence check.
    Byte comparison applies to every match, so a future adapter that re-types
    either string inline (as the v1 overrides did) still has to match the TS
    source.
    """
    context_descriptions = []
    for path, lineno, text in _declared_descriptions():
        where = f"{path}:{lineno}"
        if text.startswith("Check for additional tools"):
            assert text == TS_GET_MORE_TOOLS_DESCRIPTION, where
        elif text.startswith("A description of your goal"):
            context_descriptions.append(where)
            assert text == TS_GET_MORE_TOOLS_CONTEXT_DESCRIPTION, where
    assert context_descriptions, "no context description found under src/agentcat"
