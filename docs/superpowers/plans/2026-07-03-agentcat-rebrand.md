# AgentCat Rebrand (Branch B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Per Naseem: every dispatched subagent runs on **Fable 5**.

**Goal:** Stage the `agentcat` 1.0.0b1 rename of the Python SDK on the local-only `rebrand/agentcat` branch — everything renamed MCPCat → AgentCat, depending on `agentcat-api==1.0.0`, never pushed or published.

**Architecture:** Mirror the TS SDK's commit sequence: API client swap → endpoint/env → logging → diagnostics → exporters → public types → package identity → docs/CI/assets → verification. Each commit passes the full test suite. The physical `src/mcpcat/` → `src/agentcat/` rename and the three `version("mcpcat")` distribution lookups flip together in one commit (they resolve the *installed dist name*, which only changes when pyproject's `name` does).

**Tech Stack:** Python 3.10+, uv, hatchling, pytest, pydantic v2, generated OpenAPI client `agentcat_api`.

**Spec:** `docs/superpowers/specs/2026-07-03-agentcat-rebrand-design.md` — read it before starting.

## Global Constraints

- **LOCAL ONLY:** never `git push`, never open a PR, never `uv publish` / upload. A public trace leaks the rebrand.
- Run tests with `uv run pytest` (never bare pytest / .venv paths). CI enforces only pytest; ruff/mypy baselines are dirty — do not treat their output as your regression.
- `mcp:*` event-type literals are protocol-owned — NEVER rename them.
- Dated history under `docs/superpowers/specs/` and `docs/superpowers/plans/` keeps MCPCat references (except this plan/spec pair, which is already AgentCat-aware). `dist/`, `.codex/`, `DIAGNOSTICS_PORT_PLAN.md`, `uv.lock` history untouched except where a task says otherwise.
- The ONLY legacy compat surface: `MCPCAT_API_URL` env fallback. No `import mcpcat` shim, no `MCPCat*` type aliases, no `MCPCAT_DEBUG_MODE` fallback, no `~/mcpcat.log` fallback.
- Wire literals after this branch: event type `agentcat:identify`; wire field `agentcat_version`; exporter source `agentcat`. Backend canonicalization for these is live (mcpcat-server PR #324; `agentcat_version` accepted, confirmed 2026-07-02).
- Brand strings: `AgentCat` (display), `agentcat` (machine), `agentcathq` (GitHub org), `AgentCat, Inc.` (legal), `agentcat.com` (domain).
- Commit messages: conventional-commit style, no Co-Authored-By lines.

---

### Task 0: Preflight

**Files:** none (branch + baseline check)

- [ ] **Step 1: Confirm branch and baseline**

```bash
git rev-parse --abbrev-ref HEAD   # expect: rebrand/agentcat
git status --porcelain            # expect: only untracked .codex/, DIAGNOSTICS_PORT_PLAN.md
uv run pytest -q 2>&1 | tail -3   # expect: all pass (record the count, e.g. "N passed")
```

Record the passing test count — every later task must end at that same count or higher.

---

### Task 1: API client swap (`agentcat-api==1.0.0`) + wire literals

**Files:**
- Modify: `pyproject.toml:23` (dependency), `uv.lock` (regenerated)
- Modify: `src/mcpcat/types.py:8,45,50-51,112`
- Modify: `src/mcpcat/modules/event_queue.py:16`
- Modify: `src/mcpcat/modules/session.py:23,151` (function name + field only — NOT the `version("mcpcat")` literal)
- Modify: `src/mcpcat/modules/identify.py:10,33`
- Modify: `src/mcpcat/modules/exporters/otlp.py:72,156,160`
- Modify: all test files referencing `mcpcat_api`, `mcpcat:identify`, `MCPCAT_IDENTIFY`, `mcpcat_version` (~22 sites; find with grep)

**Interfaces:**
- Produces: `EventType.AGENTCAT_IDENTIFY = "agentcat:identify"`; `SessionInfo.agentcat_version` field; `get_agentcat_version()` in session.py. Later tasks use these exact names.

- [ ] **Step 1: Swap the dependency**

In `pyproject.toml` dependencies, change `"mcpcat-api==0.1.9"` → `"agentcat-api==1.0.0"`, then:

```bash
uv lock && uv sync
```

- [ ] **Step 2: Run suite to see the expected breakage (RED)**

```bash
uv run pytest -q 2>&1 | tail -3
```
Expected: collection errors — `ModuleNotFoundError: No module named 'mcpcat_api'`.

- [ ] **Step 3: Rename imports and wire literals**

`src/mcpcat/types.py:8`: `from mcpcat_api import PublishEventRequest` → `from agentcat_api import PublishEventRequest`
`src/mcpcat/modules/event_queue.py:16`: `from mcpcat_api import ApiClient, Configuration, EventsApi` → `from agentcat_api import ...`

`src/mcpcat/types.py:112` (enum member — the new client's validator REJECTS the old literal):
```python
    AGENTCAT_IDENTIFY = "agentcat:identify"
```
Update the one src usage `src/mcpcat/modules/identify.py:33` (`EventType.MCPCAT_IDENTIFY.value` → `EventType.AGENTCAT_IDENTIFY.value`) and its docstring at line 10 (`mcpcat:identify` → `agentcat:identify`).

`src/mcpcat/types.py:45`: `mcpcat_version: Optional[str] = None` → `agentcat_version: Optional[str] = None` (SessionInfo merges into PublishEventRequest by dict spread at `event_queue.py:315` — field names must match the new client). Fix the comments on lines 50–51 (`mcpcat:identify` → `agentcat:identify`).

`src/mcpcat/modules/session.py`: rename `get_mcpcat_version()` → `get_agentcat_version()` (line 23) and the `mcpcat_version=get_mcpcat_version()` kwarg (line 151) → `agentcat_version=get_agentcat_version()`. **Leave `version("mcpcat")` on line 28 unchanged** — the installed dist is still named `mcpcat` until Task 7.

`src/mcpcat/modules/exporters/otlp.py:72,156,160`: `event.mcpcat_version` → `event.agentcat_version` (and the literal attr key `"mcpcat.version"`-style key at 158 if present — check the block; the key becomes `agentcat.version` only if it was `mcpcat.version`).

- [ ] **Step 4: Sweep the tests**

```bash
grep -rln 'mcpcat_api\|MCPCAT_IDENTIFY\|mcpcat:identify\|mcpcat_version' tests/
```
In each hit: `mcpcat_api` → `agentcat_api`, `EventType.MCPCAT_IDENTIFY` → `EventType.AGENTCAT_IDENTIFY`, literal `"mcpcat:identify"` → `"agentcat:identify"`, `mcpcat_version` → `agentcat_version`. Do NOT touch `mcp:` literals.

- [ ] **Step 5: Verify suite passes (GREEN)**

```bash
uv run pytest -q 2>&1 | tail -3
grep -rn 'mcpcat_api\|mcpcat_version\|mcpcat:identify' src/ tests/   # expect: no hits
```
Expected: same pass count as Task 0.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat!: replace mcpcat-api with agentcat-api@1.0.0, emit agentcat:identify + agentcat_version"
```

---

### Task 2: Default endpoint `api.agentcat.com` + env vars

**Files:**
- Modify: `src/mcpcat/modules/constants.py:5`
- Modify: `src/mcpcat/modules/event_queue.py:17,41`
- Modify: `src/mcpcat/__init__.py:157-158`
- Modify: `src/mcpcat/modules/logging.py:10-13`
- Test: `tests/test_api_base_url.py`, `tests/test_logging.py`

**Interfaces:**
- Produces: constant `AGENTCAT_API_URL = "https://api.agentcat.com"` in constants.py. Env resolution order: `options.api_base_url` → `AGENTCAT_API_URL` env → `MCPCAT_API_URL` env → default.

- [ ] **Step 1: Write failing precedence tests**

Add to `TestTrackApiBaseUrl` in `tests/test_api_base_url.py` (reuses the existing `_call_track_with_patches` helper):

```python
    def test_agentcat_env_var_overrides_default(self):
        """AGENTCAT_API_URL env var should trigger configure() when no option set."""
        opts = MCPCatOptions()
        mock_eq = self._call_track_with_patches(
            opts, env_vars={"AGENTCAT_API_URL": "https://new.example.com"}
        )
        mock_eq.configure.assert_called_once_with("https://new.example.com")

    def test_agentcat_env_var_takes_precedence_over_mcpcat(self):
        """AGENTCAT_API_URL wins over the legacy MCPCAT_API_URL fallback."""
        opts = MCPCatOptions()
        mock_eq = self._call_track_with_patches(
            opts,
            env_vars={
                "AGENTCAT_API_URL": "https://new.example.com",
                "MCPCAT_API_URL": "https://legacy.example.com",
            },
        )
        mock_eq.configure.assert_called_once_with("https://new.example.com")
```

Also update `test_default_url_used_when_not_configured` to import/assert `AGENTCAT_API_URL` instead of `MCPCAT_API_URL`, and keep `test_env_var_overrides_default` (legacy fallback still works — that's the point).

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_api_base_url.py -v
```
Expected: new tests FAIL (configure called with legacy value / ImportError on `AGENTCAT_API_URL`).

- [ ] **Step 3: Implement**

`src/mcpcat/modules/constants.py:5`:
```python
AGENTCAT_API_URL = "https://api.agentcat.com"  # Default API URL for AgentCat events
```

`src/mcpcat/modules/event_queue.py`: import and use `AGENTCAT_API_URL` (lines 17, 41).

`src/mcpcat/__init__.py:157-158`:
```python
        # Resolve API base URL: option > new env var > legacy env var > default
        api_base_url = (
            options.api_base_url
            or os.environ.get("AGENTCAT_API_URL")
            or os.environ.get("MCPCAT_API_URL")
        )
```

`src/mcpcat/modules/logging.py:11`: `os.getenv("MCPCAT_DEBUG_MODE")` → `os.getenv("AGENTCAT_DEBUG_MODE")` (no fallback — locked decision). Update any test referencing `MCPCAT_DEBUG_MODE`:

```bash
grep -rn 'MCPCAT_DEBUG_MODE' tests/ src/
```

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/test_api_base_url.py tests/test_logging.py -v && uv run pytest -q 2>&1 | tail -3
```
Expected: PASS, full-suite count ≥ Task 0 count (+2 new tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat!: default endpoint api.agentcat.com; AGENTCAT_API_URL env (MCPCAT_API_URL fallback kept); AGENTCAT_DEBUG_MODE"
```

---

### Task 3: Log path `~/agentcat.log`

**Files:**
- Modify: `src/mcpcat/modules/logging.py:47-48`, `src/mcpcat/modules/constants.py:2`
- Test: `tests/test_logging.py:20`

- [ ] **Step 1: Flip the test expectation first**

`tests/test_logging.py:20`: `os.path.expanduser("~/mcpcat.log")` → `os.path.expanduser("~/agentcat.log")`. Check for other `mcpcat.log` assertions:

```bash
grep -rn 'mcpcat\.log' tests/
```

- [ ] **Step 2: Verify failure**

```bash
uv run pytest tests/test_logging.py -v
```
Expected: FAIL (log written to old path).

- [ ] **Step 3: Implement**

`src/mcpcat/modules/logging.py:47-48`:
```python
    # Always use ~/agentcat.log
    log_path = os.path.expanduser("~/agentcat.log")
```
`src/mcpcat/modules/constants.py:2`: `LOG_PATH = "agentcat.log"  # Default log file path`

- [ ] **Step 4: Verify + commit**

```bash
uv run pytest tests/test_logging.py -v && uv run pytest -q 2>&1 | tail -3
git add -A && git commit -m "feat!: log to ~/agentcat.log (no old-path fallback)"
```

---

### Task 4: Diagnostics attribute keys + scope name

**Files:**
- Modify: `src/mcpcat/modules/diagnostics.py:157,159,162-163,167` (+ scope-name emission site — grep `DIAGNOSTICS_SCOPE_NAME` usage)
- Modify: `src/mcpcat/modules/constants.py:16`
- Test: `tests/test_diagnostics_attributes.py`, `tests/test_diagnostics_no_payload.py`

**Interfaces:**
- Produces: OTLP attribute keys `agentcat.project_id`, `agentcat.install_id`, `agentcat.sdk.language`, `agentcat.sdk.version`, `agentcat.mcp_sdk.version`; scope `agentcat-diagnostics`. (Collector-side dashboards flip is tracked in REBRAND.md — shared with TS.)

- [ ] **Step 1: Flip test expectations first**

In `tests/test_diagnostics_attributes.py` (lines 27-53) and `tests/test_diagnostics_no_payload.py`: every `mcpcat.` attribute key → `agentcat.`, `mcpcat-diagnostics` → `agentcat-diagnostics`.

```bash
grep -rn 'mcpcat\.\|mcpcat-diagnostics' tests/test_diagnostics_attributes.py tests/test_diagnostics_no_payload.py
```

- [ ] **Step 2: Verify failure**

```bash
uv run pytest tests/test_diagnostics_attributes.py tests/test_diagnostics_no_payload.py -v
```
Expected: FAIL on key names.

- [ ] **Step 3: Implement**

`src/mcpcat/modules/diagnostics.py` `_build_static_attributes`: `mcpcat.project_id|install_id|sdk.language|sdk.version|mcp_sdk.version` → `agentcat.*` (5 keys; `process.*`/`os.*`/`host.*` stay). Leave `version("mcpcat")` at line 55 unchanged (Task 7).
`src/mcpcat/modules/constants.py:16`: `DIAGNOSTICS_SCOPE_NAME = "agentcat-diagnostics"`.
Update module docstrings mentioning `~/mcpcat.log` → `~/agentcat.log`, MCPCat → AgentCat (`diagnostics.py` head, `types.py:180-193` comment block).

- [ ] **Step 4: Verify + commit**

```bash
uv run pytest tests/test_diagnostics_attributes.py tests/test_diagnostics_no_payload.py -v && uv run pytest -q 2>&1 | tail -3
git add -A && git commit -m "feat!: agentcat.* diagnostics attributes + agentcat-diagnostics scope"
```

---

### Task 5: Exporter brand strings (accepted customer-side split, D3)

**Files:**
- Modify: `src/mcpcat/modules/constants.py:6`
- Modify: `src/mcpcat/modules/exporters/otlp.py:71,153,177,245,252`
- Modify: `src/mcpcat/modules/exporters/datadog.py:10,144,150,163`
- Modify: `src/mcpcat/modules/exporters/sentry.py:10,45,370,401`
- Test: exporter tests — find with `grep -rln 'MCPCAT_SOURCE\|ddsource\|mcpcat' tests/ | grep -i 'otlp\|datadog\|sentry\|telemetry\|export'`

- [ ] **Step 1: Flip test expectations first (RED), covering:**
  - OTLP scope `"name": "mcpcat"` → `"agentcat"`; `telemetry.sdk.name` `mcpcat-python` → `agentcat-python`; `source` attr → `agentcat`; tag keys `mcpcat.tag.*` → `agentcat.tag.*`; `mcpcat.properties` → `agentcat.properties`.
  - Datadog `source:` tag + `ddsource` → `agentcat`; customer-tag namespace `mcpcat.<key>` → `agentcat.<key>`.
  - Sentry `source` → `agentcat`; `contexts["mcpcat"]` → `contexts["agentcat"]`; `sentry_client=mcpcat/1.0.0` → `sentry_client=agentcat/1.0.0`.

```bash
uv run pytest tests/ -k 'otlp or datadog or sentry or telemetry or export' -v 2>&1 | tail -5
```
Expected: FAIL on renamed literals.

- [ ] **Step 2: Implement**

`src/mcpcat/modules/constants.py:6`:
```python
AGENTCAT_SOURCE = "agentcat"  # Source attribution for telemetry exporters
```
Update the three exporter imports (`from ...modules.constants import MCPCAT_SOURCE` → `AGENTCAT_SOURCE`) and all use sites listed above. These write into customers' OWN observability tools and are NOT backend-canonicalized — the split is deliberate (D3, 2026-06-29); do not add compatibility shims.

- [ ] **Step 3: Verify + commit**

```bash
uv run pytest -q 2>&1 | tail -3
grep -rn 'MCPCAT_SOURCE\|"mcpcat"' src/mcpcat/modules/exporters/  # expect: no hits
git add -A && git commit -m "feat!: exporter source/brand strings agentcat (accepted customer-side split)"
```

---

### Task 6: Public types `AgentCatOptions` / `AgentCatData` + brand copy

**Files:**
- Modify: `src/mcpcat/types.py:167,206` + every `MCPCatOptions`/`MCPCatData` reference (37 files across src/ + tests/ — grep-driven)
- Modify: brand copy in docstrings/log messages across `src/` (`__init__.py` docstrings, `logging.py:1`, `types.py:1`, etc.)

**Interfaces:**
- Produces: `AgentCatOptions`, `AgentCatData` — exact names all later tasks and tests use. NO `MCPCat*` aliases.

- [ ] **Step 1: Mechanical rename**

```bash
grep -rl 'MCPCatOptions\|MCPCatData' src/ tests/ | xargs sed -i '' 's/MCPCatOptions/AgentCatOptions/g; s/MCPCatData/AgentCatData/g'
```

- [ ] **Step 2: Brand copy sweep in src/**

```bash
grep -rn 'MCPCat\|MCPcat\|mcpcat' src/ --include='*.py' | grep -v 'import mcpcat\|from mcpcat\|version("mcpcat")'
```
For each hit in docstrings/comments/log strings: MCPCat/MCPcat → AgentCat; `docs.mcpcat.io` → `docs.agentcat.com`; `mcpcat.track(` examples → `agentcat.track(`; `import os, mcpcat` doctest → `import os, agentcat`. Leave module import paths (`from mcpcat...`) and `version("mcpcat")` for Task 7. Log-message strings like `"MCPCat setup started"` → `"AgentCat setup started"` — the diagnostics severity regex (`fail|error`, `Warning:`) is brand-free, so message renames are safe.

- [ ] **Step 3: Verify + commit**

```bash
uv run pytest -q 2>&1 | tail -3
grep -rn 'MCPCatOptions\|MCPCatData' src/ tests/  # expect: no hits
git add -A && git commit -m "feat!: rename public types to AgentCat* (no aliases) + brand copy sweep"
```

---

### Task 7: Package identity — `import agentcat`, dist `agentcat` 1.0.0b1

The big mechanical commit. Everything here lands together because the three `version("mcpcat")` lookups only resolve once the installed dist name changes.

**Files:**
- Rename: `src/mcpcat/` → `src/agentcat/` (git mv)
- Modify: every `from mcpcat`/`import mcpcat` in src/ + tests/ (~90 test files)
- Modify: `pyproject.toml` (name, version, authors, description, classifiers, URLs, hatch packages, mypy/ruff paths if present)
- Modify: `src/agentcat/__init__.py:9`, `src/agentcat/modules/session.py:28`, `src/agentcat/modules/diagnostics.py:55` — `version("mcpcat")` → `version("agentcat")`
- Modify: `LICENSE` (copyright holder)

- [ ] **Step 1: Move the package and rewrite imports**

```bash
git mv src/mcpcat src/agentcat
grep -rl 'from mcpcat\|import mcpcat' src/ tests/ | xargs sed -i '' 's/from mcpcat\./from agentcat./g; s/from mcpcat import/from agentcat import/g; s/^import mcpcat$/import agentcat/g; s/import mcpcat\./import agentcat./g'
grep -rn 'mcpcat' src/ tests/ --include='*.py' | grep -v agentcat   # audit stragglers: patch() strings, monkeypatch targets, doctest text
```
Watch for `patch("mcpcat.` strings in tests (e.g. `test_api_base_url.py` TRACK_PATCHES) — sed above misses quoted module paths; fix them to `patch("agentcat.`.

- [ ] **Step 2: Flip the three distribution-name lookups**

`src/agentcat/__init__.py:9`: `__version__ = version("agentcat")`
`src/agentcat/modules/session.py:28`: `return importlib.metadata.version("agentcat")`
`src/agentcat/modules/diagnostics.py:55`: `return version("agentcat")`
(Missing any of these = `PackageNotFoundError` on import after publish — the §3.4 M1 DOA scenario.)

- [ ] **Step 3: Package metadata**

`pyproject.toml`:
```toml
[project]
name = "agentcat"
version = "1.0.0b1"
description = "Analytics tool for MCP (Model Context Protocol) servers and AI agents - tracks tool usage patterns and provides insights"
authors = [
    { name = "AgentCat, Inc.", email = "support@agentcat.com" },
]
```
Classifier `Development Status :: 2 - Pre-Alpha` → `Development Status :: 4 - Beta`. URLs → `https://github.com/agentcathq/agentcat-python-sdk` (+ `/issues`). `[tool.hatch.build.targets.wheel] packages = ["src/agentcat"]`.

`LICENSE`: copyright holder → `AgentCat, Inc.` (keep the year).

```bash
uv lock && uv sync
```

- [ ] **Step 4: Verify**

```bash
uv run pytest -q 2>&1 | tail -3          # full suite, same count
uv run python -c "import agentcat; print(agentcat.__version__)"   # expect: 1.0.0b1
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat!: package identity agentcat@1.0.0b1, import agentcat, AgentCat, Inc. metadata"
```

---

### Task 8: Docs, CI workflows, brand assets

**Files:**
- Modify: `README.md`, `CONTRIBUTING.md`, `AGENTS.md`
- Create: `docs/static/og-image.png`, `docs/static/architecture.png` (copied from TS repo)
- Delete: `docs/static/logo-light.svg`, `docs/static/logo-dark.svg`
- Modify: `.github/workflows/mcp-compatibility.yml`, `.github/workflows/mcp-prerelease-compatibility.yml`

- [ ] **Step 1: Copy final brand assets from the TS rebrand branch**

```bash
cd /Users/naseemalnaji/Projects/mcpcat/mcpcat-typescript-sdk
git show rebrand/agentcat:docs/static/og-image.png > /Users/naseemalnaji/Projects/mcpcat/mcpcat-python-sdk/docs/static/og-image.png
git show rebrand/agentcat:docs/static/architecture.png > /Users/naseemalnaji/Projects/mcpcat/mcpcat-python-sdk/docs/static/architecture.png
cd /Users/naseemalnaji/Projects/mcpcat/mcpcat-python-sdk
git rm docs/static/logo-light.svg docs/static/logo-dark.svg
```

- [ ] **Step 2: README rewrite (mirror the TS branch's README structure)**

- Hero `<picture>` block (lines 2-5) → single `<img alt="AgentCat — see exactly how agents experience your product" src="docs/static/og-image.png" width="80%">`.
- Architecture diagram (line 57, user-attachments URL) → `<img alt="AgentCat architecture — the AgentCat SDK inside your MCP server sends analytics to your observability vendors and session replay to the AgentCat dashboard" src="docs/static/architecture.png" />`.
- Badges → `badge.fury.io/py/agentcat`, `pypi.org/project/agentcat`, `github.com/agentcathq/agentcat-python-sdk` paths.
- `pip install mcpcat` → `pip install agentcat`; `pip install "mcpcat[community]"` → `pip install "agentcat[community]"`.
- Links: `mcpcat.io` → `agentcat.com`, `docs.mcpcat.io` → `docs.agentcat.com`, `meet.mcpcat.io/meet` → `meet.agentcat.com/meet`; TS-SDK cross-link → `github.com/agentcathq/agentcat-typescript-sdk`.
- All code examples: `import mcpcat` → `import agentcat`, `mcpcat.track` → `agentcat.track`, `MCPCatOptions` → `AgentCatOptions`, brand prose → AgentCat. `docs/cats/` images stay.

- [ ] **Step 3: CONTRIBUTING.md + AGENTS.md**

`CONTRIBUTING.md`: clone URLs/dir names → `agentcat-python-sdk`, `src/mcpcat` paths → `src/agentcat`, issue links → `agentcathq`.
`AGENTS.md`: `src/mcpcat/` → `src/agentcat/`, `mcpcat/modules` → `agentcat/modules`, `mypy src/mcpcat` → `mypy src/agentcat`.

- [ ] **Step 4: CI workflows**

Both workflow files: `mcpcat-api==0.1.4` → `agentcat-api==1.0.0`; brand strings MCPCat → AgentCat in subjects/HTML/echo blocks; `'from': 'no-reply@mcpcat.io'` → `'no-reply@agentcat.com'`; `pip install mcpcat` snippets → `agentcat`. Note in each commit body that the sender flip is gated on `agentcat.com` being Resend-verified (Phase 1 item) before this branch merges.

- [ ] **Step 5: Verify + commit**

```bash
grep -rn 'mcpcat' README.md CONTRIBUTING.md AGENTS.md .github/  # expect: no hits
uv run pytest -q 2>&1 | tail -3
git add -A && git commit -m "docs!: AgentCat README/CONTRIBUTING/AGENTS + brand assets from Figma; CI installs agentcat-api, alerts from agentcat.com"
```

---

### Task 9: Final verification + REBRAND.md tracker update

**Files:**
- Modify: `/Users/naseemalnaji/Projects/mcpcat/REBRAND.md` (§3.4 + change log; outside this repo — plain file edit, no commit here)

- [ ] **Step 1: Build + clean-venv DOA check**

```bash
uv run hatch build 2>/dev/null || uvx hatch build
ls dist/ | tail -2          # expect agentcat-1.0.0b1.tar.gz + agentcat-1.0.0b1-py3-none-any.whl
python3 -m venv /tmp/agentcat-venv && /tmp/agentcat-venv/bin/pip install -q dist/agentcat-1.0.0b1-py3-none-any.whl
/tmp/agentcat-venv/bin/python -c "import agentcat; print(agentcat.__version__); from agentcat import AgentCatOptions; print('OK')"
```
Expected: `1.0.0b1` then `OK` (kills the §3.4 M1 `PackageNotFoundError` scenario). Also confirm the wheel contains ONLY `agentcat/` (no `mcpcat/`): `unzip -l dist/agentcat-1.0.0b1-py3-none-any.whl | head`.

- [ ] **Step 2: Residue audit**

```bash
grep -rin 'mcpcat' src/ tests/ pyproject.toml README.md CONTRIBUTING.md AGENTS.md LICENSE .github/ | grep -v 'MCPCAT_API_URL'
```
Every remaining hit must be intentional: the `MCPCAT_API_URL` env fallback (code + its tests/comments) and nothing else. Dated `docs/superpowers/` history is exempt (not in the grep set).

- [ ] **Step 3: Full suite one last time**

```bash
uv run pytest -q 2>&1 | tail -3
```
Expected: ≥ Task 0 count, zero failures.

- [ ] **Step 4: Update REBRAND.md (authorized by Naseem 2026-07-03)**

In §3.4:
- Mark items `[~]` staged with a `— staged on local rebrand/agentcat branch (2026-07-03)` note: agentcat-api dep (now UNBLOCKED: `agentcat-api==1.0.0` live on PyPI), dist-name lookups, endpoint/env, event-type strings, types rename, v1.0.0b1 staging, CI email/README.
- Rewrite the shim clause: `(+ deprecation shim \`import mcpcat\`)` → **dropped** (wheel-collision hazard; TS no-aliases precedent) — decided 2026-07-03.
- Rewrite `(+ aliases)` → **no aliases** (mirrors TS 2026-07-02 decision).
- Note `MCPCAT_DEBUG_MODE` → `AGENTCAT_DEBUG_MODE` **without** fallback and log path without fallback (deviations from D4, matching TS).
- Add diagnostics-rename line (attribute keys + scope, shared collector-side flip with TS §3.3).
- Note version staged as **1.0.0b1** (PEP 440 twin of TS beta).
- Add a §6 change-log entry dated 2026-07-03 summarizing Branch B staging + the four deviations, referencing this repo's spec path.

- [ ] **Step 5: Report**

Summarize: commits on `rebrand/agentcat`, final test count, wheel verification output, REBRAND.md diff summary. NO push, NO publish.
