# AgentCat Python SDK Examples

These examples show how to integrate AgentCat into MCP servers built with both generations of the official MCP Python SDK and the community FastMCP framework.

Each example is a standalone echo server that runs over Streamable HTTP, self-contained in a single file via a [PEP 723](https://peps.python.org/pep-0723/) inline-metadata header.

## Examples

| Example | Port | Description |
|---------|------|-------------|
| [officialsdk/factory](officialsdk/factory) | 8090 | Stateless per-request serving — `track()` inside `create_server()`, the expected 2026 deployment shape (official MCP SDK 2.x) |
| [officialsdk/basic](officialsdk/basic) | 8091 | Minimal 3-line AgentCat integration with the official MCP SDK 2.x (`MCPServer`) |
| [officialsdk/advanced](officialsdk/advanced) | 8092 | Full AgentCat v2 options (per-call `identify`, `enable_agent_tracking`, hook mode, redaction, debug) with the official MCP SDK 2.x |
| [officialsdk/legacy](officialsdk/legacy) | 8093 | The official MCP SDK **1.x** shape (`mcp.server.fastmcp.FastMCP`) — same `track()` call, prior generation |
| [fastmcp/basic](fastmcp/basic) | 8094 | Minimal 3-line AgentCat integration with community [FastMCP](https://github.com/jlowin/fastmcp) v4 |
| [fastmcp/advanced](fastmcp/advanced) | 8095 | Full AgentCat v2 options with community FastMCP v4 |
| [fastmcp/v3](fastmcp/v3) | 8096 | Community FastMCP **v3** — same `track()` call, prior generation |

## Running an Example

Each example is a self-contained script. To run one:

```bash
uv run --no-project examples/officialsdk/basic/main.py
```

The PEP 723 header at the top of each file pins that example's MCP generation and pulls `agentcat` from this checkout as an editable install. `uv run` resolves it into an isolated, cached environment — the first run takes a moment, later runs start instantly — and **never touches the project's `.venv`**. That matters here: the repo's `mcp-legacy` and `mcp-modern` dependency groups conflict, and a bare `uv run` inside a legacy-synced checkout would re-sync it to modern (see [CONTRIBUTING.md](../CONTRIBUTING.md)). Script environments are exempt from all of that, so the legacy examples run from a modern checkout and vice versa.

The server starts on its configured port (see table above) and accepts Streamable HTTP connections at `/mcp`.

## Running All of Them

```bash
make run-examples    # start all seven in the background
make smoke-examples  # POST an MCP initialize to every port
make stop-examples   # stop them all (by port)
```

The committed [`.mcp.json`](../.mcp.json) at the repo root points at all seven servers, so Claude Code opened in this repo sees them automatically once they're running — check with `/mcp`.

To use one with any other MCP client, point the client at the URL. For instance, in a Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "echo": {
      "url": "http://localhost:8091/mcp"
    }
  }
}
```

Or with the Claude Code CLI outside this repo:

```bash
claude mcp add echo-server http://localhost:8091/mcp
```

## What the Examples Demonstrate

### Basic

The basic examples show that AgentCat integration is just 3 lines of code added to a normal MCP server:

```python
project_id = os.environ.get("AGENTCAT_PROJECT_ID") or "proj_YOUR_PROJECT_ID"
agentcat.track(server, project_id)
```

Every tool call is captured automatically. MCP 2026-07-28 has no protocol sessions, so AgentCat correlates the calls belonging to one task through an explicit `session_id` parameter it adds to each tool schema, mints back to the agent on its first call, and strips out again before your handler runs. `track()` never raises — a shape AgentCat does not support is logged to `~/agentcat.log` and your server comes back untracked rather than failing to start.

### Advanced

The advanced examples show the v2 options:

- **`identify`** — attach actor identity (ID, name, metadata) to a call's event. It runs on **every tool call**, uncached, and stamps only that event; keep it cheap and make no network calls in it
- **`enable_agent_tracking`** — also inject a required `agent_id` parameter so parallel agents on one task can be told apart (off by default)
- **`resolve_session_id` (hook mode)** — shown in a comment block: return your own correlation ID and AgentCat derives the session from it deterministically. In hook mode no `session_id` is injected anywhere and no session instructions are shown to the agent
- **`redact_sensitive_information`** — strip sensitive data (e.g. emails) before it leaves the process
- **`debug_mode`** — enable debug logging to `~/agentcat.log`
- **`enable_tool_call_context`** / **`enable_report_missing`** — shown in a comment block: opt out of the injected `context` parameter and the `get_more_tools` tool (both enabled by default)

AgentCat trusts only a `session_id` it issued (`ses_` plus a 27-character KSUID). Anything else publishes without a session and the agent is told to re-send the real one — so a tool that declares its own `session_id` parameter cannot be correlated. Use `resolve_session_id` if you already manage sessions yourself.

### Factory

`officialsdk/factory` is the stateless deployment shape: the server is built in a `create_server()` factory that calls `track()` on the instance it is about to return, then served with `stateless_http=True` so nothing survives between requests. It demonstrates that:

- module-level state (publisher, logger, diagnostics) initializes once no matter how many servers you track, and per-server state is weakly keyed and released when a server goes away — a factory does not leak;
- correlation survives statelessness, because the `session_id` handle travels on the wire rather than in server memory;
- **rebuild on demand** works: a stateless client can send `tools/call` to an instance that never served a `tools/list`, and AgentCat rebuilds its injection registries from that server's own tool list on the first call;
- shutdown is process-wide: there is no handle to hold, the event queue drains itself at exit.

### Legacy generations

`officialsdk/legacy` (official SDK 1.x) and `fastmcp/v3` (community FastMCP v3) are the prior-generation shapes a large installed base still runs. agentcat supports all four from one install — `track()` classifies whatever object you hand it and installs the matching adapter — so these files differ from their modern siblings only in the server class and its serving API, never in the AgentCat integration.

## Configuration

All examples read the project ID from the `AGENTCAT_PROJECT_ID` environment variable, falling back to `MCPCAT_PROJECT_ID`, then to `"proj_YOUR_PROJECT_ID"` — the same precedence the SDK itself uses for `AGENTCAT_API_URL` / `MCPCAT_API_URL`.

```bash
export AGENTCAT_PROJECT_ID="proj_abc123"
uv run --no-project examples/officialsdk/basic/main.py
```

Set `AGENTCAT_DEBUG_MODE=true` to get verbose SDK logging in `~/agentcat.log` (the advanced examples turn this on via `debug_mode=True`).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — resolves and runs the self-contained scripts (Python ≥3.10 is fetched automatically if needed)
- An AgentCat project ID from [agentcat.com](https://agentcat.com) — set via `AGENTCAT_PROJECT_ID` or edit the fallback in the code
