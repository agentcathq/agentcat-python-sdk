# AgentCat × MCP 2026-07-28 — Cross-SDK Changelog & Implementation Brief

**Audience:** maintainers of the AgentCat Python and Go SDKs.
**Reference implementation:** `agentcat` (TypeScript) `2.0.0-beta.4`, branch `feat/explicit-handles-v2`.
**Spec context:** [MCP 2026-07-28 release](https://blog.modelcontextprotocol.io/posts/2026-07-28/) (SEP-2567).

This document is language-agnostic: it describes the *behavior* every AgentCat SDK
must converge on, with the TypeScript implementation as the worked example. Where
exact bytes matter (agent-facing copy, tag names, wire keys, ID derivation), they
are specified verbatim in the appendices — **do not reword or re-derive them.**

---

## 1. What the protocol changed and why we care

MCP 2026-07-28 makes the protocol **stateless by default**:

- The `initialize`/`initialized` handshake and the `Mcp-Session-Id` header are
  gone. There is no protocol-level session to attach analytics to.
- Every request is self-describing: client identity and protocol version travel
  per-request under reserved `io.modelcontextprotocol/*` metadata keys.
- Servers are commonly built from **per-request factories** (a fresh server
  object per HTTP request), landing behind round-robin load balancers with no
  shared storage. Any instance may serve `tools/call` without ever having
  served `tools/list`.
- **Multi Round-Trip Requests (MRTR)** replace held-open elicitation streams: a
  single logical tool call can span several HTTP rounds (`input_required`
  intermediate results, then a continuation carrying the client's input
  responses).
- Tasks moved to the formal `io.modelcontextprotocol/tasks` extension
  (poll-based). *Note: AgentCat's `task_id` handle is unrelated to this
  extension — see §7 Non-goals.*

Everything AgentCat previously leaned on for correlation — the initialize
handshake, transport session IDs, inactivity-based session rollover, cached
client identity — is either gone or unreliable in this world. The 2.0 response
replaces all of it with **explicit, stateless, per-request mechanisms**.

---

## 2. TL;DR — old model vs. new model

| Concern | Pre-2026 (1.x) | 2026-era (2.0) |
| --- | --- | --- |
| Correlation | Transport `sessionId` + inactivity rollover | Explicit `task_id` handle, echoed by the agent as a tool parameter |
| Session events | `mcp:initialize`, `mcp:tools/list`, `agentcat:identify` published | **Removed.** Only tool-call and custom events remain |
| Actor identity | `identify` hook + per-session identity cache | `identify` runs on **every** tool call; result stamped on that event; **no cache** |
| Client name/version | Captured once at initialize, cached | Resolved **per request** from the envelope/`_meta`, stamped on every event |
| Agent identity | (none) | Opt-in self-chosen `agent_id`, carried as event tags |
| Server state | Per-session state | **Stateless resolution** — nothing stored between requests; per-server state only in weak/ephemeral maps keyed by the server object |

Backward compatibility on the wire: **the task ID is stored in the existing
`sessionId` event field with the existing `ses_` prefix.** Dashboards, queries,
the ingestion API, and exporters are unaffected. No backend changes required.

---

## 3. Task handles replace session maintenance

### 3.1 What is removed

- All transport-session logic: reading `extra.sessionId` (or your language's
  equivalent), session caches, inactivity-based session rollover/timeouts.
  `extra.sessionId` is **ignored entirely**, even when the transport still
  provides one.
- The `mcp:initialize` and `mcp:tools/list` event types are no longer
  published. (`tools/list` is still *intercepted* — for schema injection — it
  just doesn't emit an event.)
- The `agentcat:identify` event type and the identity cache are gone. Actor
  fields ride on every event instead.

### 3.2 The `task_id` handle

- One `task_id` covers one goal, start to finish. Subagents share their
  parent's `task_id`.
- Format: a KSUID with the `ses` prefix (e.g. `ses_2a4F...`), deliberately
  reusing the session prefix and the `Event.sessionId` field.
- Injected into **every** tool's input schema as an **optional** string
  parameter named `task_id` — including AgentCat's own `get_more_tools` tool
  (its calls publish events, so it must be able to carry handles). It is never
  added to the schema's `required` list: **omission is the minting signal.**

### 3.3 Per-call resolution algorithm (prompted mode — the default)

For each `tools/call`, statelessly:

```
supplied = args["task_id"] if it is a string whose trimmed value is non-empty
if supplied:
    task_id = supplied          # trusted VERBATIM — no shape validation
    task_source = "supplied"
else:
    task_id = new ses_ KSUID    # random mint
    task_source = "minted"
```

Nothing is stored on the server between requests, so concurrent requests can
never clobber each other. A supplied value is trusted verbatim (agents echo
what we minted; a strict format check would sever tasks over cosmetic
differences).

### 3.4 Mint-back: telling the agent its handle

Two delivery channels, both applied to the wire response only:

**(a) Trailing text content block** — appended **only on the call that minted
a new task** (i.e. `task_source == "minted"`, and never in hook mode):

```
[MCP INSTRUCTIONS]: task_id issued.
  task_id=<the ses_ id> — required on every subsequent tool call
Without task_id, this server does not function as intended.
```

Append it to error results too (`isError: true`) — the retry after an error
must carry the same task. Only requirement to append: the result has an array
`content`. Never mutate the customer's result object — copy.

**(b) Structured mirror in `structuredContent`** — unlike (a), this is
persistent handle state present on **every** response (supplied handles are
re-confirmed so an agent can re-read its own handles mid-conversation). The
SDK adds a `_mcp_instructions` object:

```jsonc
"_mcp_instructions": {
  "task_id": "ses_...",        // omitted in hook mode
  "agent_id": "opus-...|...",  // only when the agent supplied one
  "instructions": "<mint-back text if minted this call, else the 'confirmed' copy>"
}
```

Rules:
- Never name a handle the agent cannot echo: no `task_id` key in hook mode, no
  `agent_id` key when the agent didn't supply one. If neither is present,
  mirror nothing.
- Only mirror into a **plain-object** `structuredContent`. If the customer's
  result already contains a `_mcp_instructions` key, it is customer data — it
  wins, skip the mirror.
- Gate the mirror on the output-injection registry (§5.3): only mirror for
  tools whose declared `outputSchema` we successfully extended. Exception: if
  no registry exists at all (rebuild failed), mirror anyway — the client
  cannot be validating against a schema we know about.

**Why the schema declaration matters:** 2026-era clients ajv/schema-validate
`structuredContent` against the tool's advertised `outputSchema`, and common
schema generators emit `additionalProperties: false`. An undeclared extra key
would fail the customer's *entire* result. So for every tool that declares a
plain-object `outputSchema`, inject an **optional** `_mcp_instructions`
property (object, with the sub-property descriptions from Appendix A) at
list-time. Composed schemas (`oneOf`/`allOf`/`anyOf`) have no single
properties bag — skip them (log a warning; mint-back stays content-only for
that tool).

**Mint-back is wire-only.** The recorded event's `response` is the customer's
original result — no `[MCP INSTRUCTIONS]` block, no `_mcp_instructions` field.
Same for error messages.

### 3.5 Hook mode — customers who bring their own correlation IDs

New option `resolveTaskId(request, extra) -> string | null` (name it
idiomatically per language). When configured:

- **No `task_id` parameter is injected anywhere** and no task instructions are
  ever shown to the agent. The customer owns task state.
- The returned string is combined with the project ID and **deterministically
  derived** into a `ses_` KSUID (§3.6), so the same customer ID always maps to
  the same task across processes, restarts — and, once you implement this,
  across *languages*. `task_source = "hook"`.
- A nullish return or a thrown error mints a random task silently
  (`task_source = "minted"`, still no agent-facing instructions — the agent
  has no parameter to echo, so announcing an ID it can never send would be
  noise). Log the hook error. A configured hook should answer every request.

### 3.6 Deterministic derivation — MUST match across SDKs

```
input   = project_id ? f"{customer_id}:{project_id}" : customer_id
hash    = SHA-256(input)                           # 32 bytes
ts_ms   = 1704067200000                            # 2024-01-01T00:00:00Z, fixed epoch
        + (uint32_be(hash[0:4]) % 31536000000)     # offset < 365 days, keeps KSUID valid
payload = hash[4:20]                               # 16 bytes
ksuid   = KSUID(timestamp = floor((ts_ms - 1400000000000) / 1000) as uint32-BE,
                payload   = payload)               # standard 20-byte KSUID, base62 → 27 chars
result  = "ses_" + base62(ksuid)
```

(`1400000000000` is the standard KSUID epoch, 2017-05-13T16:53:20Z. The
customer ID is trimmed before hashing.) Add a cross-language golden-vector
test: pick a few `(customer_id, project_id)` pairs and assert the exact
`ses_…` output matches the TypeScript SDK.

### 3.7 Custom events

`publishCustomEvent` no longer derives a task from its session-id string
argument — the string is used **verbatim** as the task ID. Prefer an explicit
`task_id` field on the custom-event data (TS: `CustomEventData.taskId`), which
takes precedence. The tracked-server form publishes **without** a task unless
one is explicitly given.

---

## 4. Optional self-chosen `agent_id`

Distinguishes parallel agents working the same task. **Off by default** —
opt-in via `enableAgentTracking: true` (breaking-change posture: quiet by
default).

- Injected into every tool's input schema as a string parameter named
  `agent_id`, **marked `required` in the schema**.
- The value is **self-chosen by the agent** — there is no server-side agent
  minting (an earlier design minted `agt_` KSUIDs server-side; it was removed,
  the `agt` prefix stays reserved). The parameter description instructs the
  agent to generate `model|harness|nonce`, e.g. `opus-4.80-1m|claude-code|k3n9x`,
  and to keep it stable for the whole task. Subagents MUST generate their own
  (opposite of `task_id`, which subagents share).
- **Enforcement is client-side, soft server-side.** A strict schema-validating
  MCP client refuses to send a call omitting a required parameter — that is
  the actual enforcement mechanism. Server-side, an omitted `agent_id` NEVER
  rejects the call: the event is simply published without agent identity, and
  no mint-back/echo mentions `agent_id`.
- Extraction mirrors `task_id`: trimmed non-empty string, trusted verbatim,
  else treated as omitted.
- Carried on events **as tags**, not as a first-class event field:
  - `agentcat_agent_id` — the supplied value, clamped for the tag channel:
    CR/LF replaced with spaces, truncated to 200 chars. (The tag channel
    bypasses customer tag validation/redaction/truncation, hence the clamp.
    The un-clamped value still appears in the recorded raw request.)
  - `agentcat_agent_id_source` — always `"supplied"` today.
- Echoed back in `_mcp_instructions.agent_id` (with the "keep sending this
  exact value" instructions) only when supplied. Never announced in the text
  mint-back block.
- Works in hook mode too: `enableAgentTracking` and `resolveTaskId` compose.
  In hook mode use the hook-mode variant of the parameter description
  (Appendix A) — it must not reference a `task_id` parameter the agent cannot
  see.

---

## 5. Per-request client identity, protocol version, and actor identity

### 5.1 Client name/version — resolved on every request, never cached

Resolution ladder (first hit wins):

1. **2026 envelope** — `extra.mcpReq.envelope["io.modelcontextprotocol/clientInfo"]`
   (or your SDK's equivalent). 2026-era server SDKs lift the reserved
   `io.modelcontextprotocol/*` keys out of `_meta` before dispatch and expose
   them under their **fully-qualified** names on the request envelope; the
   envelope is the only place they exist there.
2. **`_meta` passthrough** — `request.params._meta["io.modelcontextprotocol/clientInfo"]`
   (pre-2026 server SDKs pass the key through untouched).
3. **Legacy initialize capture** — the server's cached client info from the
   old handshake (`getClientVersion()` in TS). Keeps identity working for
   pre-2026 clients; absent on 2026-pinned stdio.

Narrow defensively: accept `name`/`version` only if each is individually a
string; a non-string field must not reach the event payload.

The resolved `clientName`/`clientVersion` are stamped **directly on every
event** at publish time, alongside server name/version, SDK language, and
AgentCat SDK version. There is no cache and no session-info object shared
between requests.

### 5.2 Protocol version

Same ladder (envelope → `_meta`) for
`io.modelcontextprotocol/protocolVersion`. When present, stamp it as the
`agentcat_protocol_version` tag on the event. This gives the platform
fleet-level visibility into protocol adoption.

### 5.3 Actor identity

The `identify` hook now runs on **every tool call**, and its result is stamped
directly onto that call's event (`identifyActorGivenId` / `identifyActorName`
/ `identifyActorData`). There is no identity cache and no separate identify
event. Hooks should be cheap; document that for customers.

**Divergence — Python awaits `identify`, TypeScript's status unverified.** The
Python SDK originally shipped `identify` as the only one of the five customer
hooks that was never awaited, so an `async def` hook built a coroutine, ran none
of its body, and published the call anonymously with no error the customer could
see. Python now resolves all five hooks through one contract
(`src/agentcat/modules/hooks.py`): sync or async, narrowed on `isawaitable` — so
an `asyncio.Task` or any `__await__` implementer works too, not just native
coroutines. A parity sweep should confirm the TypeScript side accepts the same
range before this row is called settled.

---

## 6. Mechanics you must replicate (the parts nobody puts in the headline)

### 6.1 Schema injection pipeline (at `tools/list` time)

- Parameter order in each tool's schema: **customer params, `task_id`,
  `agent_id`, `context`** (the intent-capture param — its injector runs after
  the handle injector).
- Deep-copy every schema before modifying; never mutate the customer's
  registered tool in place on the list path.
- If the input schema has `additionalProperties: false`, remove it (injected
  params must not fail validation).
- Skip injection entirely for composed input schemas (`oneOf`/`allOf`/`anyOf`)
  — log a warning.
- **Name collisions:** if a tool already defines `task_id` (or `agent_id`),
  skip injecting that parameter for that tool, log a warning, and — critically
  — make sure the customer's own parameter **reaches their handler untouched**
  (it must not be stripped).
- Record every (tool → params actually injected) pair in an
  **injected-params registry**, and every tool whose `outputSchema` you
  extended in an **output-injection registry**. These drive stripping and
  mirroring.
- The whole pipeline — config + listed tools in → advertised tools +
  registries out — must be **pure and deterministic**. §6.3 depends on it.

### 6.2 Argument stripping (at `tools/call` time)

- Before invoking the customer's handler, strip **only** the params the
  registry says were injected for that tool, on a **cloned** request.
- The published event records the **raw, unstripped** request (handles and
  `context` included) — the event shows exactly what the agent sent.
- Fallback when a call arrives with no registry (see §6.3 for why): strip all
  three names (`task_id`, `agent_id`, `context`) heuristically — except
  `get_more_tools`, whose `context` is a real parameter and must survive.

### 6.3 Per-request topology: rebuild-on-demand

2026-era factories create a fresh server per request, so a `tools/call` can
land on an instance that never served `tools/list`. On the first call, if the
registries are missing, **rebuild them** by invoking the original (unwrapped)
`tools/list` handler and running its result through the same pure injection
pipeline. Because the pipeline is deterministic, the rebuilt registries match
what any listing instance advertised. Only if the rebuild fails do you fall
back to the §6.2 heuristic strip.

Related invariants for factory topologies:

- Document `track()`-inside-the-factory as the integration pattern.
- Module-level state (event queue, telemetry manager, diagnostics) initializes
  once, first-wins, and is reused across `track()` calls. Per-server state
  lives in maps that don't outlive the server object (WeakMap in TS; use your
  language's equivalent or explicit lifecycle).
- `track()` **never throws** — any failure logs a warning and returns the
  untracked server.

### 6.4 MRTR (multi round-trip requests) tagging

A 2026-era tool call can return an intermediate result with
`resultType: "input_required"` and later complete on a continuation round
carrying the client's input responses.

- **Intermediate round** (`resultType == "input_required"`): tag the event
  `agentcat_mrtr = "input_required"`, and **do not decorate the result** — no
  text mint-back, no structured mirror. The completing round carries the
  mint-back.
- **Continuation round** (the request envelope carries `inputResponses`): tag
  the event `agentcat_mrtr = "continuation"`.
- Each round publishes its own event; they correlate through the shared
  `task_id` like everything else.

### 6.5 SDK tag namespace

All SDK-owned tags (Appendix B) are merged **after** customer tags (SDK wins
on collision) and are **exempt from the customer 50-tag cap**. Every event
gets `agentcat_task_id_source`; the others are conditional.

### 6.6 `get_more_tools`

- **Not** exempt from handle injection (it publishes events), but its bespoke
  `context` parameter is its own — only `task_id`/`agent_id` are stripped.
- Carries the read-only MCP tool annotation (`readOnlyHint`).
- Still answers when tracing is disabled.

### 6.7 Dual-generation SDK support

The TS SDK supports both MCP SDK majors through one `track()` call, using
**per-object feature detection** (never importing either SDK), per-major
adapters for the one property that differs (which field holds the dispatched
function), and a unified interception engine. Mirror the *approach* if your
language has two coexisting SDK generations: detection by probing the object
in hand, single-sourced probe list logged as a shape fingerprint (with a
diagnostics beacon on unrecognized shapes, for fleet-level drift detection),
and version-specific knowledge confined to tiny adapters.

---

## 7. Non-goals — deliberately NOT part of this design

- **No use of the `io.modelcontextprotocol/tasks` extension** for correlation.
  AgentCat's `task_id` is an analytics handle echoed as a tool argument; it is
  unrelated to the poll-based tasks extension despite the name.
- **No reliance on the `Mcp-Method` / `Mcp-Name` routing headers.**
- **No server-side session or handle storage of any kind.** Resolution is
  fully stateless per request.
- **No server-side minting of `agent_id`** (removed during design — a
  server-minted agent ID can't survive the agent's own context boundaries,
  and only the agent knows who it is).
- **No hard rejection of calls missing `agent_id`.** Required-in-schema +
  strict clients is the enforcement; the server never breaks a customer's
  tool over analytics.

---

## 8. Suggested rollout order (mirrors the TS commit history)

1. Handle primitives: minting, deterministic derivation (+ golden vectors),
   extraction, mint-back text builder, tag builder.
2. Per-request resolution (prompted + hook modes) and per-request client
   identity/protocol version.
3. Schema injection + injected-params registry + safe stripping.
4. Call-path wiring: resolve → strip → dispatch → mint-back → publish; then
   remove session machinery, identity caches, and the retired event types.
5. Structured mint-back: outputSchema declaration + `structuredContent`
   mirror.
6. `agent_id`: opt-in flag, required-in-schema, self-chosen copy, tags.
7. 2026-era topology: rebuild-on-demand, MRTR tagging, factory-pattern docs,
   concurrency regression tests (parallel calls with distinct handles must
   not cross-attribute).

---

## Appendix A — Canonical agent-facing copy (byte-identical across SDKs)

This copy was iterated for LLM compliance and is the single source of truth
(TS: `src/modules/constants.ts`, spec:
`docs/superpowers/specs/2026-07-29-explicit-handles-copy.md`). Reuse it
verbatim; fleet-wide consistency of the prompt surface is the point.

**`task_id` parameter description:**

> REQUIRED on every call after your first. This MCP server associates all of your tool calls for a given task using task_id; a call that arrives without it cannot be associated with your earlier work and is treated as the start of an unrelated task, disconnected from the original goal. Omit it on your first call only — the server will issue one in the _mcp_instructions field of the result (or a trailing [MCP INSTRUCTIONS] text block) — then echo that exact value on every later call. Never invent a value, and do not issue parallel tool calls until the server has issued your task_id. One task_id covers the whole goal from start to finish: when you spawn subagents you MUST give them this same task_id, or their work is severed from yours. Without task_id, this server does not function as intended.

**`agent_id` parameter description (default mode):**

> REQUIRED on every call, including your first. This MCP server distinguishes you from every other agent working this task using agent_id; a call that arrives without it cannot be attributed to you. Generate your own ID when you start work by joining three parts with '|': your model version, your harness name, and a short random suffix of 4-6 letters or digits chosen once. Example: opus-4.80-1m|claude-code|k3n9x. Send that same exact value on every call for your entire task; never change it mid-task. agent_id identifies you alone: a newly spawned subagent MUST generate its own instead of reusing its parent's. Without agent_id, this server does not function as intended.

**`agent_id` parameter description (hook mode):** identical except the first
sentence's task framing — it says "distinguishes you from every other agent"
(no "working this task") so it never references a `task_id` parameter the
agent cannot see. See `AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE` in the TS
constants for the exact string.

**Text mint-back block (task minted this call):**

```
[MCP INSTRUCTIONS]: task_id issued.
  task_id=<ses_ id> — required on every subsequent tool call
Without task_id, this server does not function as intended.
```

**Structured-mirror `instructions` when nothing was minted this call:**

```
[MCP INSTRUCTIONS]: <names> confirmed. Keep sending this exact value on every call.
```

where `<names>` is `task_id`, `agent_id`, or `task_id and agent_id`, and the
tail pluralizes to "these exact values" when both are present.

**`_mcp_instructions` outputSchema field descriptions:**

- field: "Your handles for this task, confirmed by this MCP server on every response, and the instructions for echoing them on later calls. Read and follow."
- `task_id` sub-property: "Echo this exact value as the task_id argument on every subsequent tool call."
- `agent_id` sub-property: "Your agent_id as this server received it. Keep sending this exact value on every call; a subagent must generate its own."

---

## Appendix B — Wire keys, tags, and ID prefixes

**Reserved metadata keys (read-only, defined by MCP):**

| Key | Purpose |
| --- | --- |
| `io.modelcontextprotocol/clientInfo` | Per-request client `{name, version}` |
| `io.modelcontextprotocol/protocolVersion` | Per-request protocol version |

**Injected parameter names:** `task_id`, `agent_id`, `context` (pre-existing).

**Structured mint-back key:** `_mcp_instructions`.

**SDK-owned event tags (post-customer merge, exempt from the 50-tag cap):**

| Tag | Values |
| --- | --- |
| `agentcat_task_id_source` | `supplied` \| `minted` \| `hook` (always present) |
| `agentcat_agent_id` | the supplied agent_id, newlines→space, max 200 chars |
| `agentcat_agent_id_source` | `supplied` |
| `agentcat_protocol_version` | e.g. `2026-07-28` (when the request carries one) |
| `agentcat_mrtr` | `input_required` \| `continuation` |

**ID prefixes:** `ses_` (tasks — deliberately the session prefix, keeps
`Event.sessionId` compatible), `evt_` (events), `agt_` (reserved; server-side
agent minting was removed).

**Event field mapping:** task ID → `Event.sessionId`. Client identity →
`Event.clientName` / `Event.clientVersion` on every event. Actor →
`Event.identifyActor*` on every event.
