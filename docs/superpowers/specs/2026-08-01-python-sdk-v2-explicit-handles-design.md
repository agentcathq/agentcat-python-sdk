# AgentCat Python SDK v2 — Explicit Handles Design

**Date:** 2026-08-01
**Status:** Approved (design review 2026-08-01)
**Inputs:** `2026-07-28-cross-sdk-changelog.md` (behavioral spec; Appendices A/B
byte-authoritative), audit
`docs/superpowers/specs/2026-08-01-mcp-2026-07-28-python-v2-audit.md`,
AgentCat TypeScript SDK `2.0.0-beta.4` (`feat/explicit-handles-v2`, north star).

> **Superseded in part (2026-08-03).** Everything below calls the handle
> `task_id`. It is now **`session_id`** throughout — the old name collided with
> the MCP Task extension (`tasks/*`), and AgentCat's handle is not an MCP task.
> The rename is mechanical (`TASK_ID_PARAM` → `SESSION_ID_PARAM`,
> `agentcat_task_id_source` → `agentcat_session_id_source`, `resolve_task_id` →
> `resolve_session_id`, `derive_task_id` → `derive_session_id`, and so on), and
> the four frozen derivation vectors are unchanged. One behavior change came
> with it: AgentCat now honors only handles it issued — see
> `agentcat-typescript-sdk/docs/superpowers/specs/2026-08-02-session-id-validation-design.md`
> and `tests/test_session_id_validation.py`. Read this document for the
> architecture; read the code for the names.
**Target:** `agentcat` `2.0.0b1` (prerelease train; stable `2.0.0` follows).

## 0. Locked decisions

1. Community FastMCP **2.x support dropped**; community support is FastMCP 3.x
   and 4.x. FastMCP-2-shaped servers get a clear log ("not supported by
   agentcat>=2; pin agentcat<2") and are returned untracked.
2. **`publish_custom_event` ships in v2** with TS-parity semantics.
3. Official MCP SDK v2 integration replaces entries in the private
   `Server._request_handlers` dict (frozen `HandlerEntry` swap).
4. Official FastMCP v1 (`mcp.server.fastmcp`) is **unified** through the
   lowlevel-v1 adapter via `server._mcp_server`; the ToolManager monkey-patch
   layer is deleted.
5. Transport `mcp-session-id` is still captured into the raw request's
   `parameters.extra.sessionId` (telemetry only; never correlation).
6. Architecture: **shared engine + thin adapters** (Approach A).

## 1. Goal

Replace session machinery with the 2026-07-28 explicit-handles model: a
stateless per-call `task_id` handle (echoed as an injected tool parameter,
stored in `Event.sessionId` with the `ses_` prefix — zero backend changes) and
an opt-in agent-chosen `agent_id` (event tags). Support servers built on the
official MCP Python SDK **1.x and 2.x** and community FastMCP **3.x and 4.x**
through one `track()` call, with per-object feature detection. Agent-facing
copy is byte-identical to the TS SDK (§9).

## 2. Module layout

```
src/agentcat/
  __init__.py            track(), publish_custom_event(), exports
  types.py               AgentCatOptions, AgentCatData, CustomEventData, EventType (pruned)
  modules/
    constants.py         + Appendix A/B strings, tag names, meta keys, "agt" prefix
    handles.py           NEW  mint / derive / extract / resolve / mint-back / tags
    injection.py         NEW  pure schema-injection pipeline + registries
    callpath.py          NEW  shared tools/call orchestration
    client_identity.py   NEW  per-request client info + protocol version ladder
    detection.py         NEW  six-shape classifier + fingerprint + beacon
                              (replaces compatibility.py + version_detection.py)
    adapters/            NEW  (replaces overrides/)
      lowlevel_v1.py     official SDK 1.x Server; also official FastMCP v1 via _mcp_server
      lowlevel_v2.py     official SDK 2.x Server; also MCPServer via _lowlevel_server
      community.py       FastMCP 3.x/4.x middleware (era flag)
    identify.py          per-call resolver; stamps event fields; no identify event
    tools.py             get_more_tools (+ readOnlyHint annotation)
    request_extra.py     kept; per-flavor context accessors injected
    session.py           DELETED
    event_queue.py, exporters/, diagnostics.py, redaction.py, truncation.py,
    sanitization.py, validation.py, logging.py, internal.py   (kept; targeted edits)
```

Per-server state stays in `internal.py`'s `WeakKeyDictionary`, keyed on the
lowlevel server object for official flavors (v1 `_mcp_server` /
v2 `_lowlevel_server` / the `Server` itself) and on the FastMCP instance for
community flavors. Module-level state (event queue, telemetry manager,
diagnostics) initializes once, first-wins; repeated `track()` on the same
server is idempotent.

## 3. Public API

### 3.1 `track(server, project_id=None, options=None) -> server`

Same signature. **Never raises** (changelog §6.3): all failures — including
missing `project_id`+exporters and incompatible servers — log a warning, emit
a diagnostics beacon, and return the server untracked. (v1 raised
`ValueError`/`TypeError`; breaking change documented in MIGRATION.md.)

### 3.2 `AgentCatOptions` changes

Added:

- `enable_agent_tracking: bool = False` — injects a required `agent_id`
  parameter into every tool. Agents self-generate `model|harness|nonce`;
  echoed in `_mcp_instructions`, stamped as event tags. Omission never rejects
  a call server-side; enforcement is client-side schema validation.
- `resolve_task_id: Callable[[Any, Any], str | None | Awaitable[str | None]] | None = None`
  — hook mode. When set: no `task_id` parameter injected, no task instructions
  prompted; the returned string is trimmed and deterministically derived with
  the project ID into a `ses_` KSUID (`task_source="hook"`). `None` return or
  a raised exception mints silently (`task_source="minted"`, hook error
  logged). Same `(request, extra)` call shape as `identify`; sync or async.

Removed: `stateless` (and `_detect_stateless`). Unchanged: everything else.
`identify` docs updated: runs on **every** tool call, result stamped on that
event; hooks must be cheap.

### 3.3 `AgentCatData` (internal) slims to

`project_id: str | None`, `options: AgentCatOptions`, plus engine state:
`injected_params_registry: dict[str, set[str]] | None`,
`output_injection_registry: set[str] | None`,
`original_list_source: Callable | None` (rebuild seam),
`server_info: (name, version)` captured at track time.
Deleted fields: `session_id`, `session_info`, `last_activity`, `is_stateless`,
`tool_registry`, `wrapped_tools`, `monkey_patched`.

### 3.4 `publish_custom_event(server_or_task_id, project_id, event_data=None) -> None`

TS parity (`publishCustomEvent`, TS `src/index.ts:363`):

- `CustomEventData` (TypedDict, `total=False`): `task_id`, `resource_name`,
  `parameters`, `response`, `message`, `duration`, `is_error`, `error`,
  `tags`, `properties`.
- Tracked-server form: `event_data["task_id"]` used **verbatim** as
  `Event.sessionId`; absent → event publishes **without** a task (empty
  session).
- String form: first argument is a task-id string used **verbatim** (never
  derived); `event_data["task_id"]` takes precedence.
- Event type `agentcat:custom`. Fire-and-forget; never raises.

### 3.5 Exports

Add `publish_custom_event`, `CustomEventData` to `__all__`. Constants gain
`AGENT_ID_PREFIX = "agt"` (reserved — server-side agent minting was removed
from the design; never mint with it).

## 4. Handle primitives (`modules/handles.py`)

- `new_task_id() -> str` — `ses_` + random KSUID (existing
  `generate_prefixed_ksuid`).
- `derive_task_id(id: str, project_id: str | None) -> str` — changelog §3.6,
  integer-only arithmetic:

  ```python
  input = f"{id}:{project_id}" if project_id else id
  h = sha256(input.encode()).digest()
  ts_ms = 1704067200000 + (int.from_bytes(h[0:4], "big") % 31536000000)
  ts_field = (ts_ms - 1400000000000) // 1000        # uint32
  raw = ts_field.to_bytes(4, "big") + h[4:20]        # 20-byte KSUID
  return "ses_" + str(Ksuid.from_bytes(raw))         # base62, zfill 27
  ```

  Does **not** trim (callers trim). Pinned to the four TS golden vectors
  (§10 tests).
- `extract_handle(arguments, name) -> str | None` — `str` whose `.strip()` is
  non-empty → trimmed value; anything else → `None`. Supplied values trusted
  verbatim (no shape validation).
- `resolve_handles(arguments, options, project_id, request, extra) -> HandleResolution`
  — dataclass: `task_id: str`, `task_source: Literal["supplied","minted","hook"]`,
  `agent_id: str | None`, `hook_mode: bool`. Prompted mode per changelog §3.3;
  hook mode per §3.5. `agent_id` extracted only when
  `enable_agent_tracking` (source always `"supplied"`).
- Mint-back builders (§9 copy):
  - `build_mint_back_text(task_id)` — exactly
    `MINT_BACK_HEADER_TASK + "\n" + mint_back_task_line(task_id) + "\n" + MINT_BACK_CLOSER`.
  - `build_confirmed_text(names)` — `mint_back_confirmed` template with
    `" and "` join and singular/plural tail.
  - `build_mcp_instructions(resolution, minted_this_call) -> dict | None` —
    keys: `task_id` (omit in hook mode), `agent_id` (omit unless supplied),
    `instructions` (mint-back text if minted this call, else confirmed copy).
    Returns `None` when neither handle key would be present.
- `build_sdk_tags(resolution, protocol_version, mrtr) -> dict[str, str]` —
  `agentcat_task_id_source` always; `agentcat_agent_id` (CR/LF→`" "`, then
  `[:200]`) + `agentcat_agent_id_source="supplied"` when supplied;
  `agentcat_protocol_version` when present; `agentcat_mrtr` when applicable.

## 5. Injection pipeline (`modules/injection.py`)

Pure, deterministic function used by both list-time injection and
rebuild-on-demand:

```
build_injected_list(tools, options) -> (advertised_tools,
                                        injected_params: dict[str, set[str]],
                                        output_injected: set[str])
```

Rules (changelog §6.1):

1. `get_more_tools` is appended by the adapter **before** the pipeline runs
   (when `enable_report_missing`), so it flows through injection like any
   tool.
2. Injection order per tool: customer params → `task_id` → `agent_id` →
   `context`. Handle injection runs before the context injector.
3. `task_id`: optional string param, description §9.1. Never added to
   `required` (omission is the minting signal). Skipped entirely in hook mode
   and when `enable_tracing=False`.
4. `agent_id` (only when `enable_agent_tracking`): string param, **appended to
   `required`**; description §9.2 (default) or §9.3 (hook mode).
5. `context` (when `enable_tool_call_context`): existing behavior, description
   `custom_context_description`; skipped for `get_more_tools` (bespoke param).
6. Deep-copy before modifying — dict schemas via `copy.deepcopy`, pydantic
   tool objects via `model_copy(deep=True)`/rebuilt dicts. Never mutate the
   customer's registered tool.
7. If the input schema has `additionalProperties: false`, delete it.
8. Composed input schemas (`oneOf`/`allOf`/`anyOf` at top level): skip all
   injection for that tool, log warning, record empty registry entry.
9. Name collision (tool already defines `task_id`/`agent_id`/`context`): skip
   that parameter only, log warning; the customer's own parameter must reach
   their handler (the registry simply omits it, so stripping spares it).
10. Every advertised tool gets a registry entry (possibly empty set).
11. Output side: tools whose `output_schema` is a plain object with a
    `properties` bag get an optional `_mcp_instructions` object property
    (field + sub-property descriptions §9.5); recorded in `output_injected`.
    Composed/missing output schemas: skipped (content-only mint-back), warn
    on composed.

The pipeline operates on a normalized internal representation
(`name`, `description`, `input_schema: dict`, `output_schema: dict | None`,
`annotations`) produced/consumed by per-adapter codecs, so era differences
(camelCase `inputSchema` v1 / snake_case `input_schema` v2 / FastMCP
`parameters`) live in the adapters, not the pipeline.

## 6. Call path (`modules/callpath.py`)

Shared orchestration invoked by every adapter for `tools/call`:

1. **Resolve** handles (§4) from raw arguments.
2. **Identify**: run `options.identify(request, extra)`; never throws; result
   stamps `identify_actor_given_id` / `identify_actor_name` / `identify_data`
   on this event only. No cache, no identify event.
3. **Client identity + protocol version** via `client_identity.py` ladder
   (§7); stamped on the event; protocol version → tag.
4. **Strip** on a cloned request: remove exactly the registry's params for
   this tool. Registry missing → **rebuild-on-demand**: invoke the adapter's
   `original_list_source`, run the pipeline, store registries; if that fails,
   heuristic strip (`task_id`, `agent_id`, `context` — but `get_more_tools`
   keeps `context`) and skip the structured mirror gate (mirror anyway, per
   changelog §3.4b exception).
5. **Dispatch** the customer handler with stripped arguments.
   `get_more_tools` short-circuits to its internal handler (still injected,
   still publishes).
6. **MRTR probe**: intermediate = result `result_type`/`resultType` ==
   `"input_required"` (or `isinstance` of the era's type); continuation =
   request params carry `input_responses`/`inputResponses`. Tag accordingly.
7. **Decorate the wire result only** (never the event copy), skipping
   intermediate rounds entirely:
   - Text block appended to a **copied** `content` array only when
     `task_source == "minted"` and not hook mode — including `is_error`
     results. Only requirement: result has a list `content`.
   - `_mcp_instructions` mirrored into `structured_content` /
     `structuredContent` on **every** completing response — but only when the
     result already carries a **plain-dict** structured content (absent or
     non-dict → no mirror; TS `mirrorStructuredMintBack`, `handles.ts:152-163`),
     gated by the output registry (see rebuild exception above); mirror onto a
     shallow copy; a customer-supplied `_mcp_instructions` key wins.
8. **Publish** one `mcp:tools/call` event: raw unstripped arguments,
   undecorated response, `session_id = task_id`, `user_intent` = captured
   `context`, actor + client fields, duration, error capture, customer
   tags/properties resolved first, then SDK tags merged (SDK wins on
   collision, exempt from the 50-tag customer cap and customer validation).

`tools/list` path: adapter intercepts, appends `get_more_tools`, runs the
pipeline, caches registries + advertised list, returns it. **No event.**
Initialize interception (where the era has one) captures legacy client info
only; **no event**.

## 7. Per-request client identity (`modules/client_identity.py`)

Resolution ladder (first hit wins; changelog §5.1), each rung narrowed to
per-field `isinstance(x, str)`:

1. **Envelope / lifted meta**: lowlevel v2 `ctx.meta[META_CLIENT_INFO_KEY]`;
   FastMCP `fastmcp_context.request_context.meta[...]`.
2. **`_meta` passthrough** (pre-2026 servers): v1
   `request.params.meta` extras (`model_extra`) under the fully-qualified key.
3. **Legacy initialize capture**: v1
   `request_context.session.client_params.clientInfo`; v2
   `ctx.session.client_params.client_info` (may be `None`); community
   handshake-era initialize capture (weak-keyed, per server — a convenience
   cache of static handshake data, not per-request state).

Protocol version: same ladder for `META_PROTOCOL_VERSION_KEY`; lowlevel v2
also has `ctx.protocol_version`. Stamped as `agentcat_protocol_version` tag
when present.

Server name/version + SDK language + agentcat version are stamped at publish
time from track-time capture and package metadata (no SessionInfo cache
object).

## 8. Detection & adapters

### 8.1 Classifier (`modules/detection.py`)

Probe the object in hand; never import version-specific symbols for the
decision. Returns `(flavor, fingerprint)`:

| Flavor | Probes |
| --- | --- |
| `community-v4` | class contains `FastMCP`, module starts `fastmcp`, has `_local_provider` + `add_middleware` + `middleware`, AND (`add_extension` or `_extensions` or `_request_state_security`) |
| `community-v3` | same minus the v4 discriminators, and no `_tool_manager` |
| `community-v2` (**unsupported**) | module starts `fastmcp`, has `_mcp_server` + `_tool_manager` → log + return untracked |
| `official-fastmcp-v1` | module starts `mcp.server.fastmcp`, has `_mcp_server` + `_tool_manager` → adapt lowlevel-v1 on `_mcp_server` |
| `mcpserver-v2` | has `_lowlevel_server` + `_tool_manager` (class `MCPServer`) → adapt lowlevel-v2 on `_lowlevel_server` |
| `lowlevel-v1` | has `request_handlers` dict (type-keyed) + `request_context` in `dir()` |
| `lowlevel-v2` | has `_request_handlers` + `add_request_handler`, no `request_context` |

Unrecognized shape: log the full probe fingerprint, emit a diagnostics beacon
(fleet drift detection), return untracked.

### 8.2 `adapters/lowlevel_v1.py`

Wraps `request_handlers[ListToolsRequest]` and `[CallToolRequest]` (and
`[InitializeRequest]` for legacy client-info capture only). Argument stripping
clones `request.params.arguments` — the in-place `pop` from v1 is gone.
Decoration builds a new `CallToolResult` (camelCase era). Serves both the bare
`Server` and official FastMCP v1 (tracked via `_mcp_server`; `get_more_tools`
appended list-side, call short-circuited — no ToolManager registration,
no `_get_cached_tool_definition` patch; the injected params never reach
FastMCP's validation layer because stripping happens above it).

### 8.3 `adapters/lowlevel_v2.py`

For `"tools/list"` / `"tools/call"`: read `entry = server.get_request_handler(m)`,
store `entry.handler` as the original, write back
`HandlerEntry(entry.params_type, wrapped)` into `_request_handlers[m]`
(frozen dataclass — replace, never mutate). Wrapped handlers receive
`(ctx, params)`; must return complete result models (era codec produces
`ListToolsResult`/`CallToolResult`; `structured_content` is `Any`, safe for
the mirror; never attach unknown attributes to v2 models). MRTR union
respected: `InputRequiredResult` passes through undecorated (tag only).
Re-arm: after `track()`, `add_request_handler` is patched on the instance so
a customer re-registering `tools/*` gets re-wrapped (TS `registrationPatch`
equivalent). Serves both bare v2 `Server` and `MCPServer` via
`_lowlevel_server`.

### 8.4 `adapters/community.py`

One `AgentCatMiddleware(Middleware)` with an era flag (v3/v4), inserted at
**index 0 = outermost** (verified: the chain builds via
`for mw in reversed(self.middleware)`, v4 `server.py:555`, so element 0 is
called first — matching today's v3 integration) so response
caching/dereference middlewares cannot cache around injection; caching below
us keys on **stripped** arguments, which is the desired semantics. `on_list_tools`: run pipeline over the FastMCP `Tool`
objects (`parameters`/`output_schema` attrs, `model_copy(update=...)`).
`on_call_tool`: strip via
`context.copy(message=message.model_copy(update={"arguments": stripped}))` —
preserving `input_responses`, `request_state`, `_meta` and every other field.
Result handling tolerates `ToolResult` and `InputRequiredToolResult` (v4).
snake_case-first field access with camelCase fallback for v3. `on_initialize`
captures legacy client info when it fires (handshake era only). Rebuild seam
(identical in both eras, verified): `await server.list_tools(run_middleware=False)`
(v3 `server.py:651`, v4 `server.py:827`); on failure, heuristic strip. `get_more_tools` registered via
`server.add_tool(Tool.from_function(...))` as today, plus `readOnlyHint`.

## 9. Byte-authoritative agent-facing copy

Source of truth: TS `src/modules/constants.ts` (verified on
`feat/explicit-handles-v2`) and changelog Appendix A. Python constants must
be byte-identical (same em-dashes, quotes, spacing). Inventory:

### 9.1 `TASK_ID_PARAM_DESCRIPTION`

> REQUIRED on every call after your first. This MCP server associates all of your tool calls for a given task using task_id; a call that arrives without it cannot be associated with your earlier work and is treated as the start of an unrelated task, disconnected from the original goal. Omit it on your first call only — the server will issue one in the _mcp_instructions field of the result (or a trailing [MCP INSTRUCTIONS] text block) — then echo that exact value on every later call. Never invent a value, and do not issue parallel tool calls until the server has issued your task_id. One task_id covers the whole goal from start to finish: when you spawn subagents you MUST give them this same task_id, or their work is severed from yours. Without task_id, this server does not function as intended.

### 9.2 `AGENT_ID_PARAM_DESCRIPTION`

> REQUIRED on every call, including your first. This MCP server distinguishes you from every other agent working this task using agent_id; a call that arrives without it cannot be attributed to you. Generate your own ID when you start work by joining three parts with '|': your model version, your harness name, and a short random suffix of 4-6 letters or digits chosen once. Example: opus-4.80-1m|claude-code|k3n9x. Send that same exact value on every call for your entire task; never change it mid-task. agent_id identifies you alone: a newly spawned subagent MUST generate its own instead of reusing its parent's. Without agent_id, this server does not function as intended.

### 9.3 `AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE`

Identical to 9.2 except the first body sentence reads "…distinguishes you
from every other agent using agent_id;" (no "working this task").

### 9.4 Mint-back text

```
MINT_BACK_HEADER_TASK = "[MCP INSTRUCTIONS]: task_id issued."
MINT_BACK_CLOSER = "Without task_id, this server does not function as intended."
mint_back_task_line(task_id) = f"  task_id={task_id} — required on every subsequent tool call"
mint_back_confirmed(names) = f"[MCP INSTRUCTIONS]: {' and '.join(names)} confirmed. Keep sending "
                             + ("these exact values" if len(names) > 1 else "this exact value")
                             + " on every call."
```

### 9.5 `_mcp_instructions` schema descriptions

- Field: "Your handles for this task, confirmed by this MCP server on every response, and the instructions for echoing them on later calls. Read and follow."
- `task_id`: "Echo this exact value as the task_id argument on every subsequent tool call."
- `agent_id`: "Your agent_id as this server received it. Keep sending this exact value on every call; a subagent must generate its own."

### 9.6 Wire keys, tags, prefixes

`MCP_INSTRUCTIONS_KEY = "_mcp_instructions"`,
`META_CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"`,
`META_PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"`,
tags `agentcat_task_id_source` / `agentcat_agent_id` /
`agentcat_agent_id_source` / `agentcat_protocol_version` / `agentcat_mrtr`,
`AGENTCAT_CUSTOM_EVENT_TYPE = "agentcat:custom"`, prefixes `ses` / `evt` /
`agt` (reserved). Existing `DEFAULT_CONTEXT_DESCRIPTION` and the
`get_more_tools` description/context copy are already byte-identical to TS —
assert, don't retype.

## 10. Testing

- **Golden vectors** (cross-language contract, frozen):
  `derive_task_id("customer-abc", "proj_1") == "ses_2cOHEO0LYGADMzRvWTXXVbbgxgm"`,
  `derive_task_id("customer-abc") == "ses_2cZY3tvyI25O2AmL2CGVo2B1IIj"`,
  `derive_task_id(" x ", "p") == "ses_2c3yR5mYKQdLaXsJNgZH6erbfQK"`,
  `derive_task_id("x", "p") == "ses_2bw285VY9apdgUgTPXKFnT6P4G0"`.
- **Constants parity test**: assert every §9 string equals the expected bytes
  (guards accidental rewording; mirrors TS `constants.test.ts`).
- **Handles unit**: mint prefix; extraction trust model incl. non-string/empty;
  agent tag clamp; mint-back exact bytes; confirmed pluralization;
  `_mcp_instructions` omission rules.
- **Injection unit**: order, deep-copy isolation, `additionalProperties`
  removal, composed skip, collision skip + customer param passthrough, output
  extension, registry contents, determinism (run twice, equal).
- **Call-path unit**: supplied/minted/hook; hook error/None → silent mint;
  strip-clone (handler sees clean args, event records raw); error-result
  mint-back; mirror gating incl. rebuild-failure mirror-anyway; customer
  `_mcp_instructions` wins; MRTR intermediate undecorated + tagged;
  continuation tagged; identify per call; ladder rungs; removed events never
  published.
- **Per-flavor suites** under uv conflict groups `mcp-legacy` / `mcp-modern`:
  existing official-v1 + community-v3 harnesses updated; new official-v2
  (lowlevel + MCPServer, streamable HTTP modern path) and community-v4
  (4.0.0b1) harnesses; concurrency regression (parallel calls, distinct
  handles, no cross-attribution) on every flavor; late-`track()` /
  rebuild-on-demand coverage (call before any list).
- **CI**: `mcp-compatibility.yml` matrix extends to mcp 2.x minors and
  fastmcp 3.x/4.x (drop 2.x discovery + the `!=2.9.*` carve-out);
  `mcp-prerelease-compatibility.yml` tracks 2.x/4.x prerelease channels.

## 11. Packaging & migration

- `pyproject.toml`: version `2.0.0b1`; `mcp>=1.2.0,<3`;
  `pydantic>=2.0.0,<3` (re-verify `agentcat-api==1.0.0` under pydantic 2.12
  before merging); community extra `fastmcp>=3.0.0,<5`; keep
  `requires-python>=3.10`; add `[tool.uv] conflicts` groups `mcp-legacy`
  (`mcp>=1.2.0,<2`) / `mcp-modern` (`mcp>=2.0.0,<3`) for dev/test.
- MIGRATION.md: new "agentcat 1.x → 2.0" section (sessions → tasks; removed
  events; removed `stateless`; identify per-call; `track()` never raises;
  FastMCP 2.x dropped; new options; `publish_custom_event`), mirroring TS
  MIGRATION.md structure.
- README + module docstrings for the new options and factory-pattern
  (`track()` inside the request factory) guidance.

## 12. Error-handling posture

`track()` never raises. Hook (`resolve_task_id`) errors mint silently + log.
`identify` errors drop actor fields for that event. Injection failure on one
tool skips that tool's injection (registry entry empty) + logs. Decoration
failure returns the customer's original result. Detection failure returns the
server untracked + beacon. Event publishing keeps existing queue semantics
(redact-fail drops event; sanitize/truncate-fail continue). Nothing AgentCat
does may alter customer tool behavior beyond the specified wire decoration.

## 13. Non-goals (changelog §7)

No use of the `io.modelcontextprotocol/tasks` extension; no `Mcp-Method` /
`Mcp-Name` routing headers; no server-side session or handle storage; no
server-side `agent_id` minting; no hard rejection of calls missing
`agent_id`; no FastMCP 2.x compatibility layer.
