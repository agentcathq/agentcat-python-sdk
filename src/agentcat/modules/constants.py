LOG_PATH = "agentcat.log"  # Default log file path
SESSION_ID_PREFIX = "ses"
EVENT_ID_PREFIX = "evt"
AGENTCAT_API_URL = "https://api.agentcat.com"  # Default API URL for AgentCat events
AGENTCAT_SOURCE = "agentcat"  # Source attribution for telemetry exporters
DEFAULT_CONTEXT_DESCRIPTION = "Explain why you are calling this tool and how it fits into the user's overall goal. This parameter is used for analytics and user intent tracking. YOU MUST provide 15-25 words (count carefully). NEVER use first person ('I', 'we', 'you') - maintain third-person perspective. NEVER include sensitive information such as credentials, passwords, or personal data. Example (20 words): \"Searching across the organization's repositories to find all open issues related to performance complaints and latency issues for team prioritization.\""

# Maximum number of exceptions to capture in a cause chain
MAX_EXCEPTION_CHAIN_DEPTH = 10

# Maximum number of stack frames to capture per exception
MAX_STACK_FRAMES = 50

# Internal SDK diagnostics (privacy-first, metadata-only OTLP logs)
DIAGNOSTICS_SCOPE_NAME = "agentcat-diagnostics"
DEFAULT_DIAGNOSTICS_ENDPOINT = "https://otel.agentcat.com"
# Public shared ingestion key — NOT a secret; ships in the package to deter
# drive-by traffic, paired with a server-side rate limit. Override with the
# DIAGNOSTICS_TOKEN env var. Must match the collector's bearer token (same
# literal as the TypeScript SDK).
DEFAULT_DIAGNOSTICS_TOKEN = "dgk_sdk_diag_3f9a2c7e1b8d4065af2e9c1d7b6a4f80"

# ── Explicit handles: injected parameter names & wire keys ───────────────────
SESSION_ID_PARAM = "session_id"
AGENT_ID_PARAM = "agent_id"
CONTEXT_PARAM = "context"
GET_MORE_TOOLS_NAME = "get_more_tools"
AGENT_ID_PREFIX = "agt"  # reserved; server-side agent minting was removed
MCP_INSTRUCTIONS_KEY = "_mcp_instructions"
META_CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
META_PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
AGENTCAT_TAG_SESSION_SOURCE = "agentcat_session_id_source"
AGENTCAT_TAG_AGENT_ID = "agentcat_agent_id"
AGENTCAT_TAG_AGENT_SOURCE = "agentcat_agent_id_source"
AGENTCAT_TAG_PROTOCOL_VERSION = "agentcat_protocol_version"
AGENTCAT_TAG_MRTR = "agentcat_mrtr"
AGENTCAT_CUSTOM_EVENT_TYPE = "agentcat:custom"

# ── Explicit handles: agent-facing copy (byte-identical to TS constants.ts) ──
# Source of truth: agentcat-typescript-sdk/src/modules/constants.ts (and the
# copy spec it cites). Do not reword here without changing the TS SDK first;
# tests/test_constants_copy.py enforces byte parity.
SESSION_ID_PARAM_DESCRIPTION = "REQUIRED on every call after your first. This MCP server associates all of your tool calls for a given task using session_id; a call that arrives without it cannot be associated with your earlier work and is treated as the start of an unrelated task, disconnected from the original goal. Omit it on your first call only — the server will issue one in the _mcp_instructions field of the result (or a trailing [MCP INSTRUCTIONS] text block) — then echo that exact value on every later call. Never invent a value, and do not issue parallel tool calls until the server has issued your session_id. One session_id covers the whole goal from start to finish: when you spawn subagents you MUST give them this same session_id, or their work is severed from yours. Without session_id, this server does not function as intended."
AGENT_ID_PARAM_DESCRIPTION = "REQUIRED on every call, including your first. This MCP server distinguishes you from every other agent working this task using agent_id; a call that arrives without it cannot be attributed to you. Generate your own ID when you start work by joining three parts with '|': your model version, your harness name, and a short random suffix of 4-6 letters or digits chosen once. Example: opus-4.80-1m|claude-code|k3n9x. Send that same exact value on every call for your entire task; never change it mid-task. agent_id identifies you alone: a newly spawned subagent MUST generate its own instead of reusing its parent's. Without agent_id, this server does not function as intended."
AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE = "REQUIRED on every call, including your first. This MCP server distinguishes you from every other agent using agent_id; a call that arrives without it cannot be attributed to you. Generate your own ID when you start work by joining three parts with '|': your model version, your harness name, and a short random suffix of 4-6 letters or digits chosen once. Example: opus-4.80-1m|claude-code|k3n9x. Send that same exact value on every call for your entire task; never change it mid-task. agent_id identifies you alone: a newly spawned subagent MUST generate its own instead of reusing its parent's. Without agent_id, this server does not function as intended."
MINT_BACK_HEADER_SESSION = "[MCP INSTRUCTIONS]: session_id issued."
MINT_BACK_HEADER_INVALID = "[MCP INSTRUCTIONS]: session_id not recognized."
MINT_BACK_CLOSER = "Without session_id, this server does not function as intended."
MINT_BACK_INVALID_LINE = "  The session_id you sent was not issued by this server. Re-send the exact session_id this server issued to you earlier in this conversation. Never invent a value. If this server has not issued you a session_id yet, omit the parameter and one will be issued."
MCP_INSTRUCTIONS_FIELD_DESCRIPTION = "Your handles for this task, confirmed by this MCP server on every response, and the instructions for echoing them on later calls. Read and follow."
MCP_INSTRUCTIONS_SESSION_ID_DESCRIPTION = (
    "Echo this exact value as the session_id argument on every subsequent tool call."
)
MCP_INSTRUCTIONS_AGENT_ID_DESCRIPTION = "Your agent_id as this server received it. Keep sending this exact value on every call; a subagent must generate its own."


def mint_back_session_line(session_id: str) -> str:
    return f"  session_id={session_id} — required on every subsequent tool call"


def mint_back_confirmed(names: list[str]) -> str:
    tail = "these exact values" if len(names) > 1 else "this exact value"
    return (
        f"[MCP INSTRUCTIONS]: {' and '.join(names)} confirmed. "
        f"Keep sending {tail} on every call."
    )
