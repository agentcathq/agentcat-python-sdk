# Migrating `agentcat` 1.x to 2.0 — Explicit handles replace MCP session correlation

MCP protocol 2026-07-28 (SEP-2567) removed protocol-level sessions, so AgentCat now correlates work with two explicit, server-minted handles that agents echo back as tool parameters:

- `session_id` — one goal, start to finish. Subagents share their parent's session_id. It is stored in the existing `session_id` event field with the same `ses_` prefix, so dashboards, queries, saved filters, and exporters are unaffected. **No backend migration.**
- `agent_id` — one per agent; subagents get their own. Rides on events as the `agentcat_agent_id` tag. Off by default — opt in with `enable_agent_tracking=True`.

### This changes your tools' public interface

Upgrading takes no configuration, but it does change what your MCP server publishes to its callers. These are your schemas and your responses — review them before you roll out.

**Every tracked tool's input schema gains** `session_id` — type `string`, optional. Agents echo it back on later calls, and AgentCat strips it before your handler runs.

```diff
  {
    "name": "search_orders",
    "inputSchema": {
      "type": "object",
      "properties": {
        "query": { "type": "string" },
+       "session_id": { "type": "string", "description": "REQUIRED on every call after your first…" }
      }
    }
  }
```

`additionalProperties: false` **is removed** from tracked input schemas. If you declared it deliberately to reject unknown parameters, that constraint no longer appears in the schema AgentCat publishes.

**With** `enable_agent_tracking=True`**,** `agent_id` **is added to the schema's** `required` **array.** This is the one addition a strict client will enforce — a schema-validating MCP client refuses to send a call that omits it. Server-side enforcement is soft: a call without `agent_id` still succeeds, and the event is simply published without agent identity. This is why agent tracking is off by default.

**Tools with a plain-object** `outputSchema` **gain an optional** `_mcp_instructions` **property**, so validating clients accept the handle mirrored into `structuredContent`. Schemas built from `oneOf` / `allOf` / `anyOf` have no single properties bag to extend and are skipped — mint-back stays content-only there.

**Responses that mint a handle gain a trailing** `[MCP INSTRUCTIONS]:` **text block.** It is wire-only: recorded event responses and error messages contain only your tool's own output. The same block appears when an agent sends a `session_id` this server never issued, correcting it without handing out a replacement.

**Tools that already declare** `session_id`**,** `agent_id`**, or** `context` **keep their own parameter.** No injection happens for that name on that tool, and the value reaches your handler untouched. `agent_id` and `context` log a warning; `session_id` logs an **error**, once per tool, because it costs you correlation on that tool.

AgentCat also stops treating that name as its own there, which is what you want and worth stating plainly: a tool whose `session_id` is *yours* — a ticket ID, a job ID, a row key — never becomes the analytics handle, and is never confirmed back to the agent. Those calls publish **without a session** rather than with a minted one: a fresh handle per call on a tool that can never carry it manufactures a phantom session per call, which looks like data and is not. The practical consequence: **calls to a tool with its own `session_id` are not correlated with each other.** If you want them correlated, either rename your parameter or supply the handle yourself with `resolve_session_id`, which injects nothing anywhere and reads no arguments at all — your parameter stays entirely yours.

**AgentCat honors only handles it issued.** A supplied `session_id` that is not a `ses_` KSUID from this server is rejected rather than adopted: the call publishes without a session, tagged `agentcat_session_id_source=invalid`, and the agent is told to re-send the ID it was given (or to omit the parameter and be issued one). `Event.session_id` is exempt from `redact_sensitive_information` and from `redact_event`, so a value AgentCat did not mint could not be redacted after the fact — which is exactly why it is never written there.

One caveat on all of the above: it depends on a `tools/list` having run. A call arriving at an instance that never served a listing rebuilds the registry from your list source; only if *that* fails does it fall back to treating all three names as AgentCat's.

### Most integrations need no code changes

`track(server, project_id, options)` keeps its signature, and `AgentCatOptions` is additive apart from one removal (`stateless`, below). Every 1.x option keeps its name: `identify`, `redact_sensitive_information`, `exporters`, `enable_report_missing`, `enable_tracing`, `enable_tool_call_context`, `custom_context_description`, `event_tags`, `event_properties`, `debug_mode`, `api_base_url`, and `disable_diagnostics`.

Three of them changed *behavior*, and all three are covered below: `identify` and the other request hooks receive a different object ([the hook argument](#the-hook-argument-changed)), `identify` / `event_tags` / `event_properties` now run at different points in the call ([behavior changes](#behavior-changes-worth-knowing)), and `redact_sensitive_information` now actually runs.

If your integration is a bare `track(server, "proj_...")` with no callbacks, upgrading is a version bump. Handles are injected and stripped inside the SDK, so your tool handlers never see the extra parameters, and handles keep landing in the `session_id` field with the `ses_` prefix — your existing dashboards, queries, and exporter pipelines keep working untouched. If you pass callbacks, read the two sections above first: both changes are one-line edits, but both are silent if you skip them.

```bash
pip install --upgrade "agentcat>=2"
# or, for Jlowin's/Prefect's FastMCP support:
pip install --upgrade "agentcat[community]>=2"
```

### Update your code only if…

**You pass** `AgentCatOptions(stateless=...)`**.** The option is gone, along with its auto-detection. Every handle, actor, and client identity is now resolved per request from the request itself, so a stateless server and a stateful one take exactly the same code path — there is nothing left to configure. Passing it raises `TypeError` from the dataclass constructor; delete the argument.

```diff
- agentcat.track(server, "proj_abc", AgentCatOptions(stateless=True))
+ agentcat.track(server, "proj_abc")
```

If you set it because you run stateless HTTP, also read the note on header-derived `client_name` under [Behavior changes worth knowing](#behavior-changes-worth-knowing) — that is the one place where stateless deployments see a visible difference.

**You run community FastMCP 2.x.** `agentcat>=2` supports FastMCP 3.x and 4.x. On a 2.x server, `track()` logs a warning to `~/agentcat.log` and returns your server **untracked** — it does not raise, and your server keeps serving. Either upgrade FastMCP or pin `agentcat<2`, which stays published and keeps working.

```bash
pip install "agentcat<2"     # staying on FastMCP 2.x
```

**You built dashboards on** `mcp:initialize`**,** `mcp:tools/list`**, or** `agentcat:identify` **events.** None of the three is published anymore. `tools/list` is still intercepted — that is how schema injection happens — it just emits no event. The actor your `identify` hook returns now rides on **every** tool-call event (`identify_actor_given_id`, `identify_actor_name`, `identify_data`), so requery against the tool-call events themselves.

**You import** `EventType`**.** It has two members: `MCP_TOOLS_CALL` (`"mcp:tools/call"`) and `AGENTCAT_CUSTOM` (`"agentcat:custom"`). Everything else was removed.

**You import** `AgentCatData`**,** `SessionInfo` **or** `ToolRegistration`**.** `SessionInfo` and `ToolRegistration` are gone. `AgentCatData` keeps `project_id` and `options`; its session and patching bookkeeping — `session_id`, `session_info`, `last_activity`, `is_stateless`, `tool_registry`, `wrapped_tools`, `monkey_patched`, `tracker_initialized` — is gone, replaced by per-request resolution. Most integrations never reference these types.

**You import from** `agentcat.modules.session`**,** `agentcat.modules.compatibility`**,** `agentcat.modules.version_detection`**,** `agentcat.modules.context_parameters`**, or anything under** `agentcat.modules.overrides`**.** All of them were deleted, the whole `overrides` package included — monkey-patching is gone, replaced by the adapters in `agentcat.modules.adapters`. The top-level names those modules exported went with them: `override_lowlevel_mcp_server`, `get_session_info`, `new_session_id`, `COMPATIBILITY_ERROR_MESSAGE`, `is_compatible_server`, `is_community_fastmcp_v2`, `is_community_fastmcp_v3`, `is_official_fastmcp_server`, `add_context_parameter_to_schema` and `add_context_parameter_to_tools`. Server classification lives in `agentcat.modules.detection` (`detect_server(server).flavor`), and there is no session module because there are no sessions.

**Your** `identify`**,** `event_tags`**,** `event_properties` **or** `resolve_session_id` **hook reads** `request.params`**.** Drop the hop — it is `request.arguments` and `request.name` now. This one fails silently; see [The hook argument changed](#the-hook-argument-changed).

**You set** `redact_sensitive_information`**.** The hook never actually ran in 1.x. It does now — check that yours is narrow enough before you upgrade. See [Behavior changes worth knowing](#behavior-changes-worth-knowing).

**You depend on** `track()` **raising.** It no longer does — see below.

**You snapshot tool schemas in tests.** The schema additions above will fail exact-match assertions. Parameter order is: your params, `session_id`, `agent_id`, `context`.

### The hook argument changed

**Your** `identify`**,** `event_tags`**,** `event_properties` **and** `resolve_session_id` **callbacks now receive the tool call's request PARAMS, not the enclosing request.** 1.x built a synthetic request object with a `.params` attribute; 2.0 hands over the params model itself, which is the one shape all four adapters can produce — the official 2.x SDK gives its handler `(ctx, params)` with no request object anywhere, and community FastMCP's message is params too.

```diff
  def identify_user(request, extra):
-     token = request.params.arguments["token"]
+     token = request.arguments["token"]
      return UserIdentity(user_id=token, user_name=None, user_data=None)
```

**This fails silently if you miss it.** A hook that raises is caught, logged to `~/agentcat.log`, and treated as "no identity" — so an un-migrated `identify` does not break your server, it just publishes every event anonymously. Grep your hooks for `.params` before you roll out, or run once with `AgentCatOptions(debug_mode=True)` and check the log.

`extra` is unchanged in spirit — it is the request context your framework exposes — but it is your adapter's own object, so keep reading it defensively (`getattr(getattr(extra, "request", None), "headers", {}) or {}`) as the examples here do.

### Behavior changes worth knowing

- **`track()` never raises.** A missing `project_id` with no exporters, an unsupported server generation, or an object AgentCat cannot recognize is logged to `~/agentcat.log` and returns your server untracked. 1.x raised `ValueError`/`TypeError` from `track()`; if you wrapped the call in `try`/`except` to keep a bad config from taking your server down, that guard is now redundant. If you *relied* on the exception to fail your startup loudly, check `~/agentcat.log` (or run with `AgentCatOptions(debug_mode=True)`) instead.
- **`identify` now runs on every tool call**, and its result is stamped directly on that call's event. There is no identity cache and no `agentcat:identify` event. If your hook does a database or API lookup, it is now on the hot path for every call — add your own caching if that matters for your latency budget.
- **`event_tags` and `event_properties` now resolve _after_ your tool handler returns.** They receive the same `(request, extra)` pair as before, but they see a later snapshot: 1.x resolved them before invoking the tool, 2.0 resolves them while building the event that reports the finished call. If your callback reads request-scoped context that your framework tears down when the handler exits (a scoped DB session, a context-local that a middleware closes, a request object your ASGI stack recycles), it may now observe a closed or mutated context. Failures degrade to "no tags/properties on that event" — the callback's exception is logged and swallowed, never raised into your tool. If your hooks read anything request-scoped, capture what you need eagerly rather than lazily.
- **`client_name` / `client_version` are no longer derived from HTTP headers.** 1.x fell back to parsing the `user-agent` header and reading `x-mcp-client-name` / `x-mcp-client-version`. 2.0 resolves client identity from three per-request sources only: the reserved `io.modelcontextprotocol/clientInfo` metadata key (2026-era clients), the same key passed through `_meta`, and the `clientInfo` your client sent at `initialize`. **If your callers are identified only by headers — the common case for pre-2026 clients on stateless HTTP, where each request gets a fresh session that never handshakes — `client_name` will start arriving empty on your events.** Nothing else about those events changes, and tool calls are unaffected. Two ways forward: have your clients send `clientInfo` at `initialize` (any stateful HTTP or stdio transport), or attach the header yourself with `event_tags`, which puts it on every event and is filterable in the dashboard:

  ```python
  from agentcat import AgentCatOptions

  def client_tag(request, extra):
      headers = getattr(getattr(extra, "request", None), "headers", {}) or {}
      name = headers.get("x-mcp-client-name") or headers.get("user-agent")
      return {"client": name} if name else None

  agentcat.track(server, "proj_abc", AgentCatOptions(event_tags=client_tag))
  ```

- **`redact_sensitive_information` now actually runs.** In 1.x the hook was configured, documented, and never invoked on a published event: the redactor walked `str`/`list`/`dict` and was handed the event *model*, so it returned it untouched. 2.0 fixes that, which means a hook you set in 1.x starts taking effect the moment you upgrade. It runs on every string in the event — `parameters`, `response`, `user_intent`, `client_name`, `server_name` and the rest — except the fields AgentCat needs to attribute the event: `session_id`, `id`, `project_id`, `event_type`, `resource_name`, `actor_id`, the three `identify_*` fields, and your own `tags` and `properties`. **If your hook is an aggressive catch-all** (`lambda s: "[REDACTED]"`), it will now blank fields your dashboards read, such as `client_name`. Narrow it to the patterns you actually care about before upgrading. A hook that raises drops the event rather than publishing it unredacted, and an async hook is supported.
- **MCP `extra.sessionId` is ignored entirely**, and inactivity-based session rollover is gone. The transport's `mcp-session-id` still rides along untouched under `parameters.extra.sessionId` on each event if you need it.
- **Request-path hooks now run contained: sync hooks on a worker thread, everything under a 5-second cap.** A sync `identify` (or `event_tags` / `event_properties` / `resolve_session_id`) that blocks — a database read, an HTTP call — no longer suspends your server's event loop; it suspends only its own call, exactly how your framework runs a sync tool body. Two consequences. First, a *sync* hook can no longer call asyncio APIs (`asyncio.ensure_future`, reading a loop-bound future): worker threads have no running loop, so make that hook `async def` — its body then runs on the loop as before. Second, a hook slower than 5 seconds has its result discarded and the call proceeds as if the hook had raised (anonymous / untagged / freshly minted, per that hook's documented degradation); the timeout is logged to `~/agentcat.log`. A hook that raises `SystemExit` or `CancelledError` is contained the same way — nothing a hook does reaches your request path.
- **The SDK never touches your process lifecycle.** 2.0.0 beta builds replaced `SIGINT`/`SIGTERM` handlers at import and force-exited via `os._exit(0)` after a drain delay — clobbering `KeyboardInterrupt`, your own handlers, `finally` blocks and `atexit` hooks. Stable 2.0 installs no signal handlers, registers no exit-time event drain, and runs every worker as a daemon thread: your shutdown is entirely yours, and exit is never delayed by AgentCat. The trade is deliberate: telemetry still queued when the process exits is dropped. The one exit hook that remains is the internal-diagnostics beacon — skipped when empty, capped at ~2 seconds.

### Bringing your own session IDs

If you already track your own session or correlation IDs, plug yours in and AgentCat will not prompt the agent about `session_id` at all — no parameter is injected and no instructions are added to your tool descriptions:

```python
import agentcat
from agentcat import AgentCatOptions

def session_from_header(request, extra):
    headers = getattr(getattr(extra, "request", None), "headers", {}) or {}
    return headers.get("x-correlation-id")

agentcat.track(server, "proj_abc", AgentCatOptions(resolve_session_id=session_from_header))
```

The returned string is combined with your project ID into a deterministic `ses_` handle, so the same correlation ID always maps to the same session. The hook may be sync or async and receives the same `(request, extra)` pair as `identify`. Returning `None` or raising mints a fresh handle silently — a configured hook should answer every request.

### Publishing your own events

`publish_custom_event` records work that is not a tool call — a background job, a webhook, a checkout step — against a session:

```python
import agentcat

agentcat.publish_custom_event(server, "proj_abc", {
    "session_id": current_session_id,
    "resource_name": "checkout",
    "message": "order confirmed",
})
```

The session ID is used **verbatim** — never validated, derived or reformatted, because this is your deliberate server-side call and not an agent's guess — and events published without one land without a session. The first argument may also be a session-ID string instead of a tracked server. Events are typed `agentcat:custom`, are fire-and-forget, and never raise.

**A server tracked with `enable_tracing=False` publishes no custom events.** Turning tracing off silences this entry point exactly as it silences tool-call events, so a server you deliberately muted does not start emitting a new event type. The session-ID-string form has no options to consult and always publishes.

### Known limitations in 2.0

- **Multi-round tool calls that mint their own handle land on separate sessions.** If a tool call spans several round trips and the *first* round is what mints the handle, each round is attributed to its own session instead of one shared session. The other two modes correlate correctly and are protocol-enforced: supplying `session_id` yourself, or deriving it with a `resolve_session_id` hook. If your server relies on multi-round tool calls, prefer one of those two.
- **Errors forwarded from a proxied community tool carry no stack detail.** When a community FastMCP server proxies a tool to an upstream server and the upstream returns an error result, no Python exception is raised locally, so the event records the message without a stack trace. Errors raised by your own tool code are unaffected. This applies from fastmcp 3.4, which taught the proxy provider to pass an upstream error result through; on 3.0–3.3 the proxy collapsed it into a raised `ToolError` instead, so those versions do record full detail.

### Supported versions

| Runtime | Supported | Notes |
| --- | --- | --- |
| Official MCP SDK (`mcp`) 1.x | ✅ | Low-level `Server` and `mcp.server.fastmcp.FastMCP` |
| Official MCP SDK (`mcp`) 2.x | ✅ | Low-level server and `MCPServer` |
| Community FastMCP (`fastmcp`) 3.x | ✅ | Requires the `agentcat[community]` extra |
| Community FastMCP (`fastmcp`) 4.x | ✅ | Requires the `agentcat[community]` extra |
| Community FastMCP (`fastmcp`) 2.x | ❌ | Logged and returned untracked — pin `agentcat<2` |
| Python | 3.10+ | Unchanged |

One `track()` call handles every supported shape; AgentCat classifies the server it is handed and installs the matching adapter. A shape it does not recognize is logged with a diagnostic fingerprint and returned untracked.

The declared floors are `mcp>=1.2.0,<3` and `fastmcp>=3.0.0,<5`, and every minor in both ranges runs the suite on each change. AgentCat works across the whole range, but the oldest MCP releases lack SDK seams that some features are built on — on `mcp<1.10` a bare low-level handler's exception type and stack frames cannot be recovered (the surfaced message is still published) and there is no structured mint-back; `mcp<1.9.2` captures no request headers; `mcp<1.8` has no Streamable HTTP at all. Nothing breaks on those versions; the affected features simply go quiet. See the table in [README.md](./README.md).

> **Installing into a fresh environment resolves `mcp` 2.x**, which removed `mcp.server.fastmcp`. AgentCat's dependency is `mcp>=1.2.0,<3` and both generations are supported, so an existing project that pins `mcp<2` is unaffected — but a `pip install agentcat` into an empty environment will give you the 2.x line, where the 1.x `from mcp.server.fastmcp import FastMCP` import does not exist. Pin `mcp<2` if you need it.

---

# Migrating from `mcpcat` to `agentcat`

MCPcat is now **AgentCat** — same team, same product, new name. The PyPI package has been renamed from `mcpcat` to [`agentcat`](https://pypi.org/project/agentcat/), starting fresh at `v1.0.0`.

## Nothing breaks if you stay

We keep every existing surface alive **permanently** — not on a deprecation timer:

- The `mcpcat` PyPI package stays published and functional
- `api.mcpcat.io` keeps accepting events forever
- The `MCPCAT_API_URL` environment variable keeps working
- Your project, data, and history stay unified regardless of which SDK sends them

If you never touch your integration, nothing stops working. Migrate on your own schedule — new features only land in `agentcat`.

## What changed

| | `mcpcat` (old) | `agentcat` (new) |
|---|---|---|
| PyPI package | `mcpcat` | `agentcat` (starts at `v1.0.0`) |
| Import | `import mcpcat` | `import agentcat` |
| Default endpoint | `https://api.mcpcat.io` | `https://api.agentcat.com` |
| Public types | `MCPCatOptions` / `MCPCatData` | `AgentCatOptions` / `AgentCatData` |
| Endpoint override | `MCPCAT_API_URL` | `AGENTCAT_API_URL` (`MCPCAT_API_URL` still honored) |
| Debug logging | `MCPCAT_DEBUG_MODE` | `AGENTCAT_DEBUG_MODE` (no fallback) |
| Local log file | `~/mcpcat.log` | `~/agentcat.log` |

There are no other API changes in the rename itself — `track()`, its options, the `identify` and redaction hooks, and the telemetry exporters all work exactly as before. (Going from 1.x to 2.0 is a separate step, covered at the top of this document.)

> **Note:** `agentcat` does not install a `mcpcat` compatibility module — a shim would collide with the real `mcpcat` distribution when both are installed. The import rename is required.

## Steps

1. **Swap the package:**

   ```bash
   pip uninstall mcpcat
   pip install agentcat
   # or, for Jlowin's/Prefect's FastMCP support:
   pip install "agentcat[community]"
   ```

2. **Rename your imports:**

   ```diff
   - import mcpcat
   - from mcpcat import MCPCatOptions
   + import agentcat
   + from agentcat import AgentCatOptions

   - mcpcat.track(server, "proj_0000000", MCPCatOptions(identify=identify_user))
   + agentcat.track(server, "proj_0000000", AgentCatOptions(identify=identify_user))
   ```

3. **Rename any imported types 1:1** — `MCPCatOptions` → `AgentCatOptions`, `MCPCatData` → `AgentCatData`. (`UserIdentity` is unchanged.)

4. **Environment variables (optional):** if you override the endpoint, prefer `AGENTCAT_API_URL` (the old `MCPCAT_API_URL` name is still read as a fallback). If you use debug logging, rename `MCPCAT_DEBUG_MODE` → `AGENTCAT_DEBUG_MODE` — this one has no fallback.

5. **Log tooling (if any):** the SDK now writes to `~/agentcat.log` instead of `~/mcpcat.log`.

Your project ID does not change, and your dashboard history is continuous.

## Or let an AI agent do it

Paste this into your coding agent (Claude Code, Cursor, Copilot, etc.) from your project root:

```text
Migrate this project from the `mcpcat` PyPI package to its renamed successor `agentcat` (same API, new package name):

1. Replace the `mcpcat` dependency with `agentcat` using this project's package manager (pip/uv/poetry; e.g. `pip uninstall mcpcat && pip install agentcat`). If the project uses the FastMCP extra, install "agentcat[community]".
2. Update every `import mcpcat` / `from mcpcat import ...` to `import agentcat` / `from agentcat import ...`. There is no compatibility shim — this rename is required.
3. Rename these types 1:1 wherever they're used: MCPCatOptions → AgentCatOptions, MCPCatData → AgentCatData. (UserIdentity is unchanged.)
4. If the env var MCPCAT_API_URL appears anywhere (code, .env files, CI, deploy config), rename it to AGENTCAT_API_URL. (Optional — the old name is still read as a fallback.)
5. If the env var MCPCAT_DEBUG_MODE appears anywhere, rename it to AGENTCAT_DEBUG_MODE. (Required — it has NO fallback.)
6. Update any references to the log path ~/mcpcat.log → ~/agentcat.log.
7. Do NOT change the project ID passed to track() — it stays the same.
8. Run the project's tests to verify, and report anything that referenced mcpcat which you could not migrate mechanically (e.g. dashboards or filters keying on source=mcpcat).
```

## Heads-up if you forward telemetry to your own tools

If you use the exporters (Datadog, Sentry, OTLP), the `source` value and tag namespaces stamped into **your** observability platform change from `mcpcat` to `agentcat`. Update any saved filters, monitors, or dashboards that key on them — a one-time change on your side.

## FAQ

**Do I have to migrate?** No — and there is no deadline. The old package and endpoint stay up permanently.

**Will my data/history split?** No. Both SDKs report into the same platform and your history stays unified under your project.

**What about the GitHub repo?** The org is being renamed; old repo URLs will redirect automatically, and stars/issues are preserved.

**Questions?** Open an issue or email [hi@agentcat.com](mailto:hi@agentcat.com).
