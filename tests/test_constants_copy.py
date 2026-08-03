"""Byte-parity guard for agent-facing copy.

The TypeScript SDK is the single source of truth for every agent-facing string:
`agentcat-typescript-sdk/src/modules/constants.ts` (and `tools.ts` for the
get_more_tools copy). The `TS_*` literals below were copied verbatim from those
files. If one of these tests fails the SDKs have drifted -- fix the copy; do not
edit the expected literal unless the TypeScript source changed too.
"""

import ast
from pathlib import Path

from agentcat.modules import constants as c
from agentcat.modules import tools as t

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "agentcat"

# ── Expected bytes, copied verbatim from TS constants.ts ────────────────────
TS_SESSION_ID_PARAM_DESCRIPTION = "REQUIRED on every call after your first. This MCP server associates all of your tool calls for a given task using session_id; a call that arrives without it cannot be associated with your earlier work and is treated as the start of an unrelated task, disconnected from the original goal. Omit it on your first call only — the server will issue one in the _mcp_instructions field of the result (or a trailing [MCP INSTRUCTIONS] text block) — then echo that exact value on every later call. Never invent a value, and do not issue parallel tool calls until the server has issued your session_id. One session_id covers the whole goal from start to finish: when you spawn subagents you MUST give them this same session_id, or their work is severed from yours. Without session_id, this server does not function as intended."  # noqa: E501
TS_AGENT_ID_PARAM_DESCRIPTION = "REQUIRED on every call, including your first. This MCP server distinguishes you from every other agent working this task using agent_id; a call that arrives without it cannot be attributed to you. Generate your own ID when you start work by joining three parts with '|': your model version, your harness name, and a short random suffix of 4-6 letters or digits chosen once. Example: opus-4.80-1m|claude-code|k3n9x. Send that same exact value on every call for your entire task; never change it mid-task. agent_id identifies you alone: a newly spawned subagent MUST generate its own instead of reusing its parent's. Without agent_id, this server does not function as intended."  # noqa: E501
TS_AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE = "REQUIRED on every call, including your first. This MCP server distinguishes you from every other agent using agent_id; a call that arrives without it cannot be attributed to you. Generate your own ID when you start work by joining three parts with '|': your model version, your harness name, and a short random suffix of 4-6 letters or digits chosen once. Example: opus-4.80-1m|claude-code|k3n9x. Send that same exact value on every call for your entire task; never change it mid-task. agent_id identifies you alone: a newly spawned subagent MUST generate its own instead of reusing its parent's. Without agent_id, this server does not function as intended."  # noqa: E501
TS_MINT_BACK_HEADER_SESSION = "[MCP INSTRUCTIONS]: session_id issued."
TS_MINT_BACK_HEADER_INVALID = "[MCP INSTRUCTIONS]: session_id not recognized."
TS_MINT_BACK_CLOSER = "Without session_id, this server does not function as intended."
TS_MINT_BACK_INVALID_LINE = "  The session_id you sent was not issued by this server. Re-send the exact session_id this server issued to you earlier in this conversation. Never invent a value. If this server has not issued you a session_id yet, omit the parameter and one will be issued."  # noqa: E501
TS_MCP_INSTRUCTIONS_FIELD_DESCRIPTION = "Your handles for this task, confirmed by this MCP server on every response, and the instructions for echoing them on later calls. Read and follow."  # noqa: E501
TS_MCP_INSTRUCTIONS_SESSION_ID_DESCRIPTION = "Echo this exact value as the session_id argument on every subsequent tool call."  # noqa: E501
TS_MCP_INSTRUCTIONS_AGENT_ID_DESCRIPTION = "Your agent_id as this server received it. Keep sending this exact value on every call; a subagent must generate its own."  # noqa: E501
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
    assert c.MCP_INSTRUCTIONS_KEY == "_mcp_instructions"
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
    assert c.MINT_BACK_HEADER_SESSION == TS_MINT_BACK_HEADER_SESSION
    assert c.MINT_BACK_HEADER_SESSION == "[MCP INSTRUCTIONS]: session_id issued."
    assert c.MINT_BACK_CLOSER == TS_MINT_BACK_CLOSER
    assert (
        c.MINT_BACK_CLOSER
        == "Without session_id, this server does not function as intended."
    )
    assert (
        c.mint_back_session_line("ses_X")
        == "  session_id=ses_X — required on every subsequent tool call"
    )
    assert c.mint_back_confirmed(["session_id"]) == (
        "[MCP INSTRUCTIONS]: session_id confirmed. "
        "Keep sending this exact value on every call."
    )
    assert c.mint_back_confirmed(["session_id", "agent_id"]) == (
        "[MCP INSTRUCTIONS]: session_id and agent_id confirmed. "
        "Keep sending these exact values on every call."
    )
    assert c.mint_back_confirmed(["agent_id"]) == (
        "[MCP INSTRUCTIONS]: agent_id confirmed. "
        "Keep sending this exact value on every call."
    )


def test_invalid_correction_copy_matches_ts():
    """The `invalid` branch corrects the agent without issuing a replacement.

    The closing sentence is load-bearing: an agent that hallucinated a
    session_id on its FIRST call was never issued one, so "re-send what you
    were given" names a value that does not exist. Omitting the parameter puts
    it back on the `minted` path.
    """
    assert c.MINT_BACK_HEADER_INVALID == TS_MINT_BACK_HEADER_INVALID
    assert c.MINT_BACK_INVALID_LINE == TS_MINT_BACK_INVALID_LINE
    assert c.MINT_BACK_INVALID_LINE.endswith(
        "If this server has not issued you a session_id yet, omit the parameter "
        "and one will be issued."
    )
    # No value is handed out anywhere in the correction.
    assert "ses_" not in c.MINT_BACK_HEADER_INVALID + c.MINT_BACK_INVALID_LINE


def test_session_id_param_description_matches_ts():
    assert c.SESSION_ID_PARAM_DESCRIPTION == TS_SESSION_ID_PARAM_DESCRIPTION
    assert c.SESSION_ID_PARAM_DESCRIPTION.startswith(
        "REQUIRED on every call after your first."
    )
    assert c.SESSION_ID_PARAM_DESCRIPTION.endswith(
        "Without session_id, this server does not function as intended."
    )
    assert "session_id and agent_id" not in c.SESSION_ID_PARAM_DESCRIPTION


def test_agent_id_param_descriptions_match_ts():
    assert c.AGENT_ID_PARAM_DESCRIPTION == TS_AGENT_ID_PARAM_DESCRIPTION
    assert (
        c.AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE
        == TS_AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE
    )
    assert "working this task" in c.AGENT_ID_PARAM_DESCRIPTION
    assert "working this task" not in c.AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE
    assert (
        c.AGENT_ID_PARAM_DESCRIPTION.replace(" working this task", "")
        == c.AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE
    )


def test_mcp_instructions_descriptions_match_ts():
    assert (
        c.MCP_INSTRUCTIONS_FIELD_DESCRIPTION == TS_MCP_INSTRUCTIONS_FIELD_DESCRIPTION
    )
    assert (
        c.MCP_INSTRUCTIONS_SESSION_ID_DESCRIPTION
        == TS_MCP_INSTRUCTIONS_SESSION_ID_DESCRIPTION
    )
    assert (
        c.MCP_INSTRUCTIONS_AGENT_ID_DESCRIPTION
        == TS_MCP_INSTRUCTIONS_AGENT_ID_DESCRIPTION
    )


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
