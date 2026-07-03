# AgentCat Rebrand — Python SDK Rename Branch (Branch B)

**Date:** 2026-07-03
**Status:** Approved design
**Branch:** `rebrand/agentcat` (LOCAL ONLY — no push, no PR, no publish; a PR would leak the rebrand)
**Reference:** `../REBRAND.md` §3.4 / D2 / D3 / D4 / D12 / D15; TS-parallel spec `mcpcat-typescript-sdk/docs/superpowers/specs/2026-07-02-agentcat-rebrand-design.md`

## Goal

Produce the staged **rename branch** for the Python SDK: the distribution becomes `agentcat` 1.0.0b1 (PEP 440 twin of the TS `1.0.0-beta.1`), importable as `import agentcat`, depending on `agentcat-api==1.0.0` (PyPI), with all living brand surfaces renamed MCPCat → AgentCat. Branch A (final `mcpcat` deprecation release) is explicitly **out of scope**.

Published at cutover (Phase 3/5), not before. Until then nothing leaves this machine.

## Constraints & non-goals

- **No behavior changes** beyond renamed identifiers/literals, one env-var addition, and the endpoint default. `track()` signature and semantics unchanged.
- **Stateless sessions NOT adopted:** `agentcat-api` 1.0.0 makes `session_id` nullable; the SDK keeps generating session IDs client-side exactly as today.
- **History untouched:** dated `docs/superpowers/` specs/plans keep their MCPCat references. `dist/`, untracked `.codex/` and `DIAGNOSTICS_PORT_PLAN.md` untouched.
- **`mcp:*` protocol event types never change** (protocol-owned, not brand).
- **Branch A out of scope.**

## Verified inputs (2026-07-03)

- **`agentcat-api` 1.0.0 is live on PyPI** (import name `agentcat_api`). Verified against the wheel — mirrors the npm client's changes:
  - Wire field `mcpcat_version` → `agentcat_version` (hard-renamed; backend confirmed accepting `agentcat_version` 2026-07-02).
  - Event-type validator now lists `agentcat:identify` and **rejects** `mcpcat:identify` — the `EventType` enum rename is mandatory, not cosmetic.
  - `project_id` optional → **required**. Safe: the SDK only calls the API when `event.project_id` is set (`event_queue.py:140`); exporters-only mode never constructs an API request.
  - `session_id` required → nullable. No SDK change (see non-goals).
  - Additive, unused by SDK: `mcp:tasks/get|result|list|cancel` enum members, `NotificationsApi`.
- The Python SDK has **no custom-events feature** — no `mcpcat:custom` literal exists anywhere; the only branded wire literal is `mcpcat:identify`.
- `agentcat` PyPI name is a claimed placeholder — we stage 1.0.0b1 locally, never publish here.
- Exporters present: `otlp.py`, `sentry.py`, `datadog.py` (+ `trace_context.py`; no PostHog exporter in Python).
- TS repo `rebrand/agentcat` now carries **both** final brand assets: `docs/static/og-image.png` (hero) and `docs/static/architecture.png` (Figma export, commit `893774b`) — both are copied here, so the Python README has **no open image punch-list item** (supersedes the "architecture diagram stays open" note from design discussion).

## Decisions (locked with Naseem, 2026-07-03)

1. **No `import mcpcat` shim** — REBRAND.md §3.4's "(+ deprecation shim `import mcpcat`)" is rejected: a top-level `mcpcat` module inside the `agentcat` wheel would collide with the real `mcpcat` dist when both are installed, and the TS no-aliases rationale (brand-new package identity, zero importers) applies equally. → REBRAND.md §3.4 should be updated.
2. **No `MCPCat*` type aliases** — `MCPCatOptions` → `AgentCatOptions`, `MCPCatData` → `AgentCatData`, rename only (supersedes §3.4 "(+ aliases)").
3. **Version `1.0.0b1`** — PEP 440 pre-release; pip won't install it by default, matching npm's `--tag beta` soft launch. Classifier `Development Status :: 2 - Pre-Alpha` → `4 - Beta`.
4. **Branch B only** — one local branch; Branch A staged separately later.
5. **Env vars:** `AGENTCAT_API_URL` first, `MCPCAT_API_URL` fallback kept (the **only** legacy surface, matching TS). `MCPCAT_DEBUG_MODE` → `AGENTCAT_DEBUG_MODE` with **no fallback**.
6. **Log path `~/agentcat.log` only** — no `~/mcpcat.log` fallback (matches TS deviation from D4).
7. **Brand images:** copy `og-image.png` + `architecture.png` from the TS repo's rebrand branch; delete `docs/static/logo-{light,dark}.svg`.
8. **CI workflows flip fully:** `agentcat-api==1.0.0` installs, AgentCat brand strings, Resend sender `no-reply@agentcat.com` (agentcat.com is Resend-verified from Phase 1; these are internal alerts).

## Design — commit sequence

Every commit passes `uv run pytest` (full suite). Behavior-adjacent changes land failing-test-first. CI enforces only pytest; ruff/mypy baselines are dirty and are not a gate.

### 1. API client swap
- `pyproject.toml`: `mcpcat-api==0.1.9` → `agentcat-api==1.0.0`; `uv lock`.
- Imports `mcpcat_api` → `agentcat_api` (src ×2 + any test references).
- `EventType.MCPCAT_IDENTIFY` → `EventType.AGENTCAT_IDENTIFY = "agentcat:identify"` (`types.py:112`) + all uses (`identify.py`, tests). Backend canonicalizes `agentcat:identify` → stored `mcpcat:identify` at ingest (D3, PR #324).
- Wire/version field `mcpcat_version` → `agentcat_version` end-to-end: `types.py:45`, `session.py:23,151` (`get_mcpcat_version` → `get_agentcat_version`), `otlp.py:72,156,160`, tests. Wire emits `agentcat_version`.

### 2. Endpoint + env vars
- `constants.py`: `MCPCAT_API_URL` const → `AGENTCAT_API_URL = "https://api.agentcat.com"`.
- `__init__.py:158`: resolution `options.api_base_url` → `os.environ["AGENTCAT_API_URL"]` → `os.environ["MCPCAT_API_URL"]` (precedence test first).
- `logging.py:11`: `MCPCAT_DEBUG_MODE` → `AGENTCAT_DEBUG_MODE` (no fallback).

### 3. Logging + diagnostics
- `logging.py:47-48` + `constants.py` `LOG_PATH`: `~/mcpcat.log` → `~/agentcat.log`, no fallback.
- `diagnostics.py`: attribute keys `mcpcat.project_id|install_id|sdk.language|sdk.version|mcp_sdk.version` → `agentcat.*`; scope name → `agentcat-diagnostics`. Unchanged: `DISABLE_DIAGNOSTICS` / `DIAGNOSTICS_*` env names (brand-free), endpoint (already `otel.agentcat.com`), token. Collector-side dashboard flip is already tracked in REBRAND.md §3.3 note — applies to Python identically.
- **DOA-critical:** the three distribution-name lookups `version("mcpcat")` → `version("agentcat")`: `__init__.py:9`, `session.py:28`, `diagnostics.py:55`. Miss any and `import agentcat` raises `PackageNotFoundError` (§3.4 M1).

### 4. Exporter brand strings (accepted customer-side split, D3 §2C)
- OTLP: scope/resource `"name": "mcpcat"` → `"agentcat"` (`otlp.py:71`), any `source` attr → `agentcat`.
- Sentry: `contexts["mcpcat"]` → `contexts["agentcat"]` (`sentry.py:401`), tag namespaces `mcpcat.*` → `agentcat.*`, client string if branded.
- Datadog: `ddsource`/`source`/tag namespaces → `agentcat`.
- These write into customers' OWN observability and are NOT backend-canonicalized — one-time split, deliberate; do not "fix" at cutover.

### 5. Public types + brand copy
- `MCPCatOptions` → `AgentCatOptions`, `MCPCatData` → `AgentCatData` (+ any other `MCPCat*` names in `types.py`), re-exports in `__init__.py`, all uses in src + tests. No aliases.
- Brand copy in docstrings/comments/log messages across `src/` → AgentCat.

### 6. Package identity (the big mechanical commit)
- `git mv src/mcpcat src/agentcat`; rewrite every `from mcpcat...`/`import mcpcat` across src + ~90 test files + conftest.
- `pyproject.toml`: `name = "agentcat"`, `version = "1.0.0b1"`, author `AgentCat, Inc.` / `support@agentcat.com`, description reworded to match TS ("Analytics tool for MCP servers and AI agents…"), classifier → `4 - Beta`, URLs → `github.com/agentcathq/agentcat-python-sdk`, `[tool.hatch.build.targets.wheel] packages = ["src/agentcat"]`, mypy/ruff paths.
- `LICENSE` copyright holder → `AgentCat, Inc.` (§2I normalization).
- `[project.optional-dependencies] community` unchanged in name — installs become `pip install "agentcat[community]"`.

### 7. Docs, CI, assets
- `README.md`: hero → `docs/static/og-image.png` (copied from TS repo), delete `logo-{light,dark}.svg`; architecture diagram → `docs/static/architecture.png` (copied from TS repo, replaces the user-attachments URL); `pip install agentcat`, badges → `pypi.org/project/agentcat` + `agentcathq` repo paths, links `mcpcat.io|docs.|meet.` → `agentcat.com` equivalents, TS-SDK cross-link → `agentcathq/agentcat-typescript-sdk`.
- `CONTRIBUTING.md`, `AGENTS.md` (living docs): paths `src/mcpcat` → `src/agentcat`, brand, repo URLs.
- `.github/workflows/mcp-compatibility.yml` + `mcp-prerelease-compatibility.yml`: `mcpcat-api==0.1.4` → `agentcat-api==1.0.0`, brand strings, sender → `no-reply@agentcat.com`, install snippets → `agentcat`.

### 8. Verification (final gate)
- Full suite green; `uv run hatch build` produces `agentcat-1.0.0b1` sdist+wheel containing only the `agentcat` package.
- Clean-venv install of the built wheel: `import agentcat; agentcat.__version__` resolves (kills the §3.4 M1 DOA scenario); smoke `track()` against a stub.
- `grep -ri mcpcat` residue audit — remaining hits must be: dated specs/history, the `MCPCAT_API_URL` env fallback, and intentional legacy-compat mentions only.

## External dependencies (must hold before publish at cutover)

1. Backend accepts `agentcat_version` — confirmed 2026-07-02.
2. Backend canonicalizes `agentcat:identify` — done (PR #324).
3. Diagnostics collector dashboards flip `mcpcat.*` → `agentcat.*` — tracked in REBRAND.md §3.3, shared with TS.
4. GitHub org rename to `agentcathq` (Phase 5) precedes publish, or pyproject URLs 404.
5. `agentcat.com` verified in Resend before CI failure-alert emails send from `no-reply@agentcat.com` (Phase 1 item).

## Deviations from REBRAND.md §3.4 (approved by Naseem, 2026-07-03)

1. **No `import mcpcat` deprecation shim** (wheel-collision hazard + TS no-aliases precedent).
2. **No `MCPCat*` type aliases.**
3. **No `MCPCAT_DEBUG_MODE` fallback** — `AGENTCAT_API_URL`→`MCPCAT_API_URL` is the only legacy env surface.
4. **No `~/mcpcat.log` fallback** (matches TS deviation from D4).
5. **Diagnostics renames added** — §3.4 predates the Python diagnostics port.
6. **Both README image assets resolved now** (og-image + architecture.png from the TS branch) rather than left as a design punch-list item.
