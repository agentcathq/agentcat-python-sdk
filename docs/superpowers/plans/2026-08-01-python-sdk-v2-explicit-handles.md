# AgentCat Python SDK v2 — Explicit Handles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

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

**Goal:** Upgrade `agentcat` to `2.0.0b1`: replace session machinery with stateless per-call `task_id`/`agent_id` handles (MCP 2026-07-28), supporting official MCP SDK 1.x + 2.x and community FastMCP 3.x + 4.x through one `track()` call.

**Architecture:** Shared pure engine (`handles.py`, `injection.py`, `callpath.py`, `client_identity.py`, `detection.py`) + three thin adapters (`lowlevel_v1`, `lowlevel_v2`, `community`) replacing `modules/overrides/`. Spec: `docs/superpowers/specs/2026-08-01-python-sdk-v2-explicit-handles-design.md`. Behavior contract: `2026-07-28-cross-sdk-changelog.md` (Appendices A/B byte-authoritative). North star: `/Users/naseemalnaji/Projects/mcpcat/agentcat-typescript-sdk` @ `feat/explicit-handles-v2`.

**Tech Stack:** Python ≥3.10, pydantic 2, `mcp` 1.x/2.x, `fastmcp` 3.x/4.x, uv (conflict groups), pytest + pytest-asyncio, ruff, mypy --strict, hatch.

## Global Constraints

- Agent-facing copy MUST be byte-identical to TS `src/modules/constants.ts` (em dashes, quotes, spacing included). Never reword.
- Golden derivation vectors are frozen: `("customer-abc","proj_1")→ses_2cOHEO0LYGADMzRvWTXXVbbgxgm`, `("customer-abc",None)→ses_2cZY3tvyI25O2AmL2CGVo2B1IIj`, `(" x ","p")→ses_2c3yR5mYKQdLaXsJNgZH6erbfQK`, `("x","p")→ses_2bw285VY9apdgUgTPXKFnT6P4G0`.
- `task_id` is stored in `Event.session_id` with the existing `ses_` prefix. No backend/wire changes.
- v2 publishes ONLY `mcp:tools/call` (+ `agentcat:custom`). No `mcp:initialize`, `mcp:tools/list`, `agentcat:identify` events anywhere.
- `track()` never raises. Failures log via `write_to_log` + return the server untracked.
- Never mutate customer objects: deep-copy schemas, clone requests before stripping, copy results before decorating. The published event carries the RAW request and the UNDECORATED response.
- No module-scope imports of version-specific `mcp`/`fastmcp` symbols in engine/detection modules (must import cleanly under both majors). Adapters may import inside functions.
- Private attributes of the SDKs are fair game (CI change detection covers drift).
- Deps: `mcp>=1.2.0,<3`, `pydantic>=2.0.0,<3`, community extra `fastmcp>=3.0.0,<5`, `requires-python>=3.10`. FastMCP 2.x is unsupported (log + return untracked).
- Style: ruff (88 cols), mypy `--strict` on `src/agentcat`, snake_case, `TypedDict` for shared shapes, Conventional Commits.
- Run tests with `uv run pytest -q` (default env = mcp-modern once Task 1 lands; use `uv sync --group mcp-legacy` env for legacy-only suites). Every task ends green in the env(s) it touches.

---

### Task 1: Packaging & dual-generation dev environments

**Files:**
- Modify: `pyproject.toml`
- Modify: `CONTRIBUTING.md` (dev-env section)

**Interfaces:**
- Produces: uv dependency-groups `mcp-legacy` (mcp 1.x) and `mcp-modern` (mcp 2.x + fastmcp 4), mutually exclusive; version `2.0.0b1`.

- [ ] **Step 1: Edit `pyproject.toml`**

```toml
[project]
version = "2.0.0b1"
dependencies = [
    "mcp>=1.2.0,<3",
    "agentcat-api==1.0.0",
    "pydantic>=2.0.0,<3",
    "requests>=2.31.0",
]

[project.optional-dependencies]
community = [
    "fastmcp>=3.0.0,<5",
]
# dev extra unchanged

[dependency-groups]
dev = [
    "freezegun>=1.5.2",
    "pytest-asyncio>=1.0.0",
    "pytest-cov>=6.1.1",
]
mcp-legacy = ["mcp>=1.2.0,<2", "fastmcp>=3.0.0,<4"]
mcp-modern = ["mcp>=2.0.0,<3", "fastmcp>=4.0.0b1,<5"]

[tool.uv]
conflicts = [[{ group = "mcp-legacy" }, { group = "mcp-modern" }]]
default-groups = ["dev", "mcp-modern"]
prerelease = "if-necessary-or-explicit"
```

- [ ] **Step 2: Verify both environments resolve**

Run: `uv sync --extra community && uv pip show mcp fastmcp | grep -E "Name|Version"`
Expected: mcp 2.0.x, fastmcp 4.0.0b1.
Run: `uv sync --extra community --no-group mcp-modern --group mcp-legacy && uv pip show mcp fastmcp | grep -E "Name|Version"`
Expected: mcp 1.x (≥1.24), fastmcp 3.x.

- [ ] **Step 3: Verify current suite still passes in the LEGACY env**

Run (still in legacy env): `uv run pytest -q -m "not e2e"`
Expected: PASS (code untouched; legacy env matches today's supported range). The modern env is NOT expected to work until Task 8+ — do not run pytest there yet.

- [ ] **Step 4: Document in CONTRIBUTING.md**

Add a "Dev environments" section: default sync = modern (`uv sync --extra community`); legacy = `uv sync --extra community --no-group mcp-modern --group mcp-legacy`; explain the conflict groups and that each suite auto-skips by installed major (Task 8 conftest).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml CONTRIBUTING.md
git commit -m "chore: v2.0.0b1 packaging, dual mcp-generation dev envs"
```

---

### Task 2: Canonical copy constants + byte-parity tests

**Files:**
- Modify: `src/agentcat/modules/constants.py`
- Test: `tests/test_constants_copy.py`

**Interfaces:**
- Produces (all in `agentcat.modules.constants`): `TASK_ID_PARAM = "task_id"`, `AGENT_ID_PARAM = "agent_id"`, `CONTEXT_PARAM = "context"`, `GET_MORE_TOOLS_NAME = "get_more_tools"`, `AGENT_ID_PREFIX = "agt"`, `MCP_INSTRUCTIONS_KEY = "_mcp_instructions"`, `META_CLIENT_INFO_KEY`, `META_PROTOCOL_VERSION_KEY`, `AGENTCAT_TAG_TASK_SOURCE`, `AGENTCAT_TAG_AGENT_ID`, `AGENTCAT_TAG_AGENT_SOURCE`, `AGENTCAT_TAG_PROTOCOL_VERSION`, `AGENTCAT_TAG_MRTR`, `AGENTCAT_CUSTOM_EVENT_TYPE = "agentcat:custom"`, `TASK_ID_PARAM_DESCRIPTION`, `AGENT_ID_PARAM_DESCRIPTION`, `AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE`, `MINT_BACK_HEADER_TASK`, `MINT_BACK_CLOSER`, `MCP_INSTRUCTIONS_FIELD_DESCRIPTION`, `MCP_INSTRUCTIONS_TASK_ID_DESCRIPTION`, `MCP_INSTRUCTIONS_AGENT_ID_DESCRIPTION`, `mint_back_task_line(task_id: str) -> str`, `mint_back_confirmed(names: list[str]) -> str`.

- [ ] **Step 1: Write the failing test** — `tests/test_constants_copy.py` asserts every string equals the TS bytes. Copy each expected literal EXACTLY from `agentcat-typescript-sdk/src/modules/constants.ts:20-62` (open the file; do not retype from this plan). Skeleton with the short ones inline:

```python
"""Byte-parity guard for agent-facing copy (TS constants.ts is the SoT)."""
from agentcat.modules import constants as c


def test_param_names_and_keys():
    assert c.TASK_ID_PARAM == "task_id"
    assert c.AGENT_ID_PARAM == "agent_id"
    assert c.MCP_INSTRUCTIONS_KEY == "_mcp_instructions"
    assert c.META_CLIENT_INFO_KEY == "io.modelcontextprotocol/clientInfo"
    assert c.META_PROTOCOL_VERSION_KEY == "io.modelcontextprotocol/protocolVersion"
    assert c.AGENTCAT_TAG_TASK_SOURCE == "agentcat_task_id_source"
    assert c.AGENTCAT_TAG_AGENT_ID == "agentcat_agent_id"
    assert c.AGENTCAT_TAG_AGENT_SOURCE == "agentcat_agent_id_source"
    assert c.AGENTCAT_TAG_PROTOCOL_VERSION == "agentcat_protocol_version"
    assert c.AGENTCAT_TAG_MRTR == "agentcat_mrtr"
    assert c.AGENT_ID_PREFIX == "agt"


def test_mint_back_assembly():
    assert c.MINT_BACK_HEADER_TASK == "[MCP INSTRUCTIONS]: task_id issued."
    assert (
        c.MINT_BACK_CLOSER
        == "Without task_id, this server does not function as intended."
    )
    assert (
        c.mint_back_task_line("ses_X")
        == "  task_id=ses_X — required on every subsequent tool call"
    )
    assert c.mint_back_confirmed(["task_id"]) == (
        "[MCP INSTRUCTIONS]: task_id confirmed. "
        "Keep sending this exact value on every call."
    )
    assert c.mint_back_confirmed(["task_id", "agent_id"]) == (
        "[MCP INSTRUCTIONS]: task_id and agent_id confirmed. "
        "Keep sending these exact values on every call."
    )


def test_param_descriptions_match_ts():
    # Paste full literals from TS constants.ts lines 20-27; spot anchors:
    assert c.TASK_ID_PARAM_DESCRIPTION.startswith("REQUIRED on every call after your first.")
    assert c.TASK_ID_PARAM_DESCRIPTION.endswith("Without task_id, this server does not function as intended.")
    assert "task_id and agent_id" not in c.TASK_ID_PARAM_DESCRIPTION
    assert "working this task" in c.AGENT_ID_PARAM_DESCRIPTION
    assert "working this task" not in c.AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE
    assert c.AGENT_ID_PARAM_DESCRIPTION.replace(" working this task", "") == c.AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE
    # ...plus full == comparisons against the pasted literals.
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_constants_copy.py -q` → FAIL (AttributeError).

- [ ] **Step 3: Implement** — append to `constants.py` (copy long strings from TS source, not from here):

```python
# ── Explicit handles: injected parameter names & wire keys ───────────────────
TASK_ID_PARAM = "task_id"
AGENT_ID_PARAM = "agent_id"
CONTEXT_PARAM = "context"
GET_MORE_TOOLS_NAME = "get_more_tools"
AGENT_ID_PREFIX = "agt"  # reserved; server-side agent minting was removed
MCP_INSTRUCTIONS_KEY = "_mcp_instructions"
META_CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
META_PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
AGENTCAT_TAG_TASK_SOURCE = "agentcat_task_id_source"
AGENTCAT_TAG_AGENT_ID = "agentcat_agent_id"
AGENTCAT_TAG_AGENT_SOURCE = "agentcat_agent_id_source"
AGENTCAT_TAG_PROTOCOL_VERSION = "agentcat_protocol_version"
AGENTCAT_TAG_MRTR = "agentcat_mrtr"
AGENTCAT_CUSTOM_EVENT_TYPE = "agentcat:custom"

# ── Explicit handles: agent-facing copy (byte-identical to TS constants.ts) ──
TASK_ID_PARAM_DESCRIPTION = "REQUIRED on every call after your first. ..."  # full TS string
AGENT_ID_PARAM_DESCRIPTION = "REQUIRED on every call, including your first. ..."  # full TS string
AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE = "..."  # full TS string (no "working this task")
MINT_BACK_HEADER_TASK = "[MCP INSTRUCTIONS]: task_id issued."
MINT_BACK_CLOSER = "Without task_id, this server does not function as intended."
MCP_INSTRUCTIONS_FIELD_DESCRIPTION = "Your handles for this task, confirmed by this MCP server on every response, and the instructions for echoing them on later calls. Read and follow."
MCP_INSTRUCTIONS_TASK_ID_DESCRIPTION = "Echo this exact value as the task_id argument on every subsequent tool call."
MCP_INSTRUCTIONS_AGENT_ID_DESCRIPTION = "Your agent_id as this server received it. Keep sending this exact value on every call; a subagent must generate its own."


def mint_back_task_line(task_id: str) -> str:
    return f"  task_id={task_id} — required on every subsequent tool call"


def mint_back_confirmed(names: list[str]) -> str:
    tail = "these exact values" if len(names) > 1 else "this exact value"
    return (
        f"[MCP INSTRUCTIONS]: {' and '.join(names)} confirmed. "
        f"Keep sending {tail} on every call."
    )
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_constants_copy.py -q` → PASS. Also `uv run ruff check src tests` → clean.

- [ ] **Step 5: Commit** — `git commit -m "feat(handles): canonical agent-facing copy constants (TS byte-parity)"`

---

### Task 3: Options & type additions (additive only)

**Files:**
- Modify: `src/agentcat/types.py`
- Test: `tests/test_options_v2.py`

**Interfaces:**
- Produces: `ResolveTaskIdFunction = Callable[[Any, Any], str | None | Awaitable[str | None]]`; `AgentCatOptions.enable_agent_tracking: bool = False`; `AgentCatOptions.resolve_task_id: ResolveTaskIdFunction | None = None`; `class CustomEventData(TypedDict, total=False)` with keys `task_id, resource_name, parameters, response, message, duration, is_error, error, tags, properties`; `AgentCatData` gains `injected_params_registry: dict[str, set[str]] | None = None`, `output_injection_registry: set[str] | None = None`, `original_list_source: Any = None`, `server_name: str | None = None`, `server_version: str | None = None`. Do NOT remove `stateless`/session fields yet (Task 11).
- Docstrings for the two options adapted from TS `src/types.ts:6-26` (types.ts wording, not the stale index.ts `@param`).

- [ ] **Step 1: Failing test**

```python
from agentcat.types import AgentCatOptions, CustomEventData


def test_v2_option_defaults():
    o = AgentCatOptions()
    assert o.enable_agent_tracking is False
    assert o.resolve_task_id is None


def test_custom_event_data_keys():
    d: CustomEventData = {"task_id": "ses_x", "is_error": False, "tags": {"a": "b"}}
    assert d["task_id"] == "ses_x"
```

- [ ] **Step 2: Run → FAIL.**  `uv run pytest tests/test_options_v2.py -q`
- [ ] **Step 3: Implement in `types.py`** (fields + TypedDict + registry fields on `AgentCatData`).
- [ ] **Step 4: Run → PASS; `uv run mypy src/agentcat --strict` clean for the touched file.**
- [ ] **Step 5: Commit** — `git commit -m "feat(types): v2 options (agent tracking, resolve_task_id) and CustomEventData"`

---

### Task 4: Handle primitives (`modules/handles.py`)

**Files:**
- Create: `src/agentcat/modules/handles.py`
- Test: `tests/test_handles.py`

**Interfaces:**
- Produces:
  - `@dataclass HandleResolution(task_id: str, task_source: Literal["supplied","minted","hook"], agent_id: str | None = None, agent_source: Literal["supplied"] | None = None, hook_mode: bool = False)`
  - `new_task_id() -> str`
  - `derive_task_id(id: str, project_id: str | None = None) -> str`
  - `extract_handle(arguments: Any, name: str) -> str | None`
  - `async resolve_handles(arguments: Any, options: AgentCatOptions, project_id: str | None, request: Any, extra: Any) -> HandleResolution`
  - `build_mint_back_text(res: HandleResolution) -> str | None` (non-None only when `task_source=="minted"` and not `hook_mode`)
  - `build_structured_mint_back(res: HandleResolution) -> dict[str, Any] | None`
  - `mirror_into_structured_content(sc: Any, mint: dict[str, Any]) -> Any | None` (returns the NEW dict, or `None` = leave result untouched)
  - `build_handle_tags(res: HandleResolution, protocol_version: str | None = None, mrtr: str | None = None) -> dict[str, str]`

- [ ] **Step 1: Failing tests** (port of TS `handles.test.ts`):

```python
import asyncio
import pytest
from agentcat.modules.handles import (
    HandleResolution, build_handle_tags, build_mint_back_text,
    build_structured_mint_back, derive_task_id, extract_handle,
    mirror_into_structured_content, new_task_id, resolve_handles,
)
from agentcat.types import AgentCatOptions


def test_mint_shape():
    a, b = new_task_id(), new_task_id()
    assert a.startswith("ses_") and len(a) == 4 + 27 and a != b


def test_golden_vectors():
    assert derive_task_id("customer-abc", "proj_1") == "ses_2cOHEO0LYGADMzRvWTXXVbbgxgm"
    assert derive_task_id("customer-abc") == "ses_2cZY3tvyI25O2AmL2CGVo2B1IIj"
    assert derive_task_id(" x ", "p") == "ses_2c3yR5mYKQdLaXsJNgZH6erbfQK"  # no trim
    assert derive_task_id("x", "p") == "ses_2bw285VY9apdgUgTPXKFnT6P4G0"


def test_extract_handle_trust_model():
    assert extract_handle({"task_id": " ses_x "}, "task_id") == "ses_x"
    assert extract_handle({"task_id": "my-own-correlation-id"}, "task_id") == "my-own-correlation-id"
    for bad in ({"task_id": ""}, {"task_id": "   "}, {"task_id": 42}, None, "str"):
        assert extract_handle(bad, "task_id") is None


async def test_prompted_supplied_vs_minted():
    o = AgentCatOptions()
    r1 = await resolve_handles({"task_id": "ses_supplied"}, o, "proj", None, None)
    assert (r1.task_id, r1.task_source, r1.hook_mode) == ("ses_supplied", "supplied", False)
    r2 = await resolve_handles({}, o, "proj", None, None)
    assert r2.task_source == "minted" and r2.task_id.startswith("ses_")


async def test_hook_mode_derives_and_falls_back():
    o = AgentCatOptions(resolve_task_id=lambda req, extra: " customer-abc ")
    r = await resolve_handles({"task_id": "ignored"}, o, "proj_1", None, None)
    assert r.task_id == derive_task_id("customer-abc", "proj_1")  # trimmed by caller
    assert r.task_source == "hook" and r.hook_mode is True

    async def async_hook(req, extra):
        return "customer-abc"
    r = await resolve_handles({}, AgentCatOptions(resolve_task_id=async_hook), "proj_1", None, None)
    assert r.task_source == "hook"

    def boom(req, extra):
        raise RuntimeError("hook broke")
    r = await resolve_handles({}, AgentCatOptions(resolve_task_id=boom), "proj_1", None, None)
    assert r.task_source == "minted" and r.hook_mode is True
    r = await resolve_handles({}, AgentCatOptions(resolve_task_id=lambda q, e: None), None, None, None)
    assert r.task_source == "minted" and r.hook_mode is True


async def test_agent_extraction_gated_on_option():
    on = AgentCatOptions(enable_agent_tracking=True)
    r = await resolve_handles({"agent_id": " a|b|c "}, on, None, None, None)
    assert (r.agent_id, r.agent_source) == ("a|b|c", "supplied")
    r = await resolve_handles({"agent_id": "a|b|c"}, AgentCatOptions(), None, None, None)
    assert r.agent_id is None


def test_mint_back_text_rules():
    minted = HandleResolution(task_id="ses_T", task_source="minted")
    assert build_mint_back_text(minted) == (
        "[MCP INSTRUCTIONS]: task_id issued.\n"
        "  task_id=ses_T — required on every subsequent tool call\n"
        "Without task_id, this server does not function as intended."
    )
    assert build_mint_back_text(HandleResolution("ses_T", "supplied")) is None
    assert build_mint_back_text(HandleResolution("ses_T", "minted", hook_mode=True)) is None


def test_structured_mint_back_omission_rules():
    both = HandleResolution("ses_T", "supplied", agent_id="A", agent_source="supplied")
    m = build_structured_mint_back(both)
    assert m == {
        "task_id": "ses_T", "agent_id": "A",
        "instructions": "[MCP INSTRUCTIONS]: task_id and agent_id confirmed. Keep sending these exact values on every call.",
    }
    hook_agent = HandleResolution("ses_T", "hook", agent_id="A", agent_source="supplied", hook_mode=True)
    m = build_structured_mint_back(hook_agent)
    assert "task_id" not in m and m["agent_id"] == "A" and "agent_id confirmed" in m["instructions"]
    assert build_structured_mint_back(HandleResolution("ses_T", "hook", hook_mode=True)) is None
    minted = HandleResolution("ses_T", "minted")
    assert build_structured_mint_back(minted)["instructions"].startswith("[MCP INSTRUCTIONS]: task_id issued.")


def test_mirror_rules():
    mint = {"task_id": "ses_T", "instructions": "i"}
    assert mirror_into_structured_content(None, mint) is None
    assert mirror_into_structured_content([1], mint) is None
    assert mirror_into_structured_content({"_mcp_instructions": "customer"}, mint) is None
    out = mirror_into_structured_content({"a": 1}, mint)
    assert out == {"a": 1, "_mcp_instructions": mint}


def test_tag_clamp():
    res = HandleResolution("ses_T", "supplied", agent_id="a\r\nb" + "x" * 300, agent_source="supplied")
    tags = build_handle_tags(res, protocol_version="2026-07-28", mrtr="continuation")
    assert tags["agentcat_task_id_source"] == "supplied"
    assert tags["agentcat_agent_id"].startswith("a  b") and len(tags["agentcat_agent_id"]) == 200
    assert tags["agentcat_agent_id_source"] == "supplied"
    assert tags["agentcat_protocol_version"] == "2026-07-28"
    assert tags["agentcat_mrtr"] == "continuation"
    assert build_handle_tags(HandleResolution("s", "minted")) == {"agentcat_task_id_source": "minted"}
```

- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement `handles.py`:**

```python
"""Explicit-handle primitives: minting, derivation, extraction, mint-back.

Cross-SDK contract: 2026-07-28-cross-sdk-changelog.md §3-§4; TS reference
src/modules/handles.ts. Derivation golden vectors are frozen — changing them
splits customer tasks across an upgrade.
"""
import hashlib
import inspect
from dataclasses import dataclass
from typing import Any, Literal, Optional

from agentcat.modules.constants import (
    AGENT_ID_PARAM, AGENTCAT_TAG_AGENT_ID, AGENTCAT_TAG_AGENT_SOURCE,
    AGENTCAT_TAG_MRTR, AGENTCAT_TAG_PROTOCOL_VERSION, AGENTCAT_TAG_TASK_SOURCE,
    MCP_INSTRUCTIONS_KEY, MINT_BACK_CLOSER, MINT_BACK_HEADER_TASK,
    SESSION_ID_PREFIX, TASK_ID_PARAM, mint_back_confirmed, mint_back_task_line,
)
from agentcat.modules.logging import write_to_log
from agentcat.thirdparty.ksuid import Ksuid
from agentcat.utils import generate_prefixed_ksuid

_KSUID_EPOCH_MS = 1_400_000_000_000
_DERIVE_EPOCH_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
_YEAR_MS = 365 * 24 * 60 * 60 * 1000


@dataclass
class HandleResolution:
    task_id: str
    task_source: Literal["supplied", "minted", "hook"]
    agent_id: Optional[str] = None
    agent_source: Optional[Literal["supplied"]] = None
    hook_mode: bool = False


def new_task_id() -> str:
    return generate_prefixed_ksuid(SESSION_ID_PREFIX)


def derive_task_id(id: str, project_id: str | None = None) -> str:
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


async def resolve_handles(arguments, options, project_id, request, extra) -> HandleResolution:
    hook = options.resolve_task_id
    agent_id = (
        extract_handle(arguments, AGENT_ID_PARAM)
        if options.enable_agent_tracking else None
    )
    agent_source: Literal["supplied"] | None = "supplied" if agent_id else None

    if callable(hook):
        try:
            value = hook(request, extra)
            if inspect.isawaitable(value):
                value = await value
        except Exception as e:  # hook errors mint silently
            write_to_log(f"Warning: resolve_task_id hook raised: {e}")
            value = None
        if isinstance(value, str) and value.strip():
            return HandleResolution(
                derive_task_id(value.strip(), project_id), "hook",
                agent_id, agent_source, hook_mode=True,
            )
        return HandleResolution(new_task_id(), "minted", agent_id, agent_source, hook_mode=True)

    supplied = extract_handle(arguments, TASK_ID_PARAM)
    if supplied:
        return HandleResolution(supplied, "supplied", agent_id, agent_source)
    return HandleResolution(new_task_id(), "minted", agent_id, agent_source)


def build_mint_back_text(res: HandleResolution) -> str | None:
    if res.task_source != "minted" or res.hook_mode:
        return None
    return "\n".join(
        [MINT_BACK_HEADER_TASK, mint_back_task_line(res.task_id), MINT_BACK_CLOSER]
    )


def build_structured_mint_back(res: HandleResolution) -> dict[str, Any] | None:
    names: list[str] = []
    if not res.hook_mode:
        names.append(TASK_ID_PARAM)
    if res.agent_id:
        names.append(AGENT_ID_PARAM)
    if not names:
        return None
    mint: dict[str, Any] = {}
    if not res.hook_mode:
        mint[TASK_ID_PARAM] = res.task_id
    if res.agent_id:
        mint[AGENT_ID_PARAM] = res.agent_id
    mint["instructions"] = build_mint_back_text(res) or mint_back_confirmed(names)
    return mint


def mirror_into_structured_content(sc: Any, mint: dict[str, Any]) -> Any | None:
    if not isinstance(sc, dict) or MCP_INSTRUCTIONS_KEY in sc:
        return None
    return {**sc, MCP_INSTRUCTIONS_KEY: mint}


def build_handle_tags(res, protocol_version=None, mrtr=None) -> dict[str, str]:
    tags = {AGENTCAT_TAG_TASK_SOURCE: res.task_source}
    if res.agent_id and res.agent_source:
        clamped = res.agent_id.replace("\r", " ").replace("\n", " ")[:200]
        tags[AGENTCAT_TAG_AGENT_ID] = clamped
        tags[AGENTCAT_TAG_AGENT_SOURCE] = res.agent_source
    if protocol_version:
        tags[AGENTCAT_TAG_PROTOCOL_VERSION] = protocol_version
    if mrtr:
        tags[AGENTCAT_TAG_MRTR] = mrtr
    return tags
```

If the golden-vector test fails: diff against TS `src/modules/handles.ts:33-47` and `src/thirdparty/ksuid/index.js:44-50` (`Math.floor((ms-14e11)/1e3)` uint32-BE + `hash[4:20]`). Do NOT adjust the vectors.

- [ ] **Step 4: Run → PASS**; `uv run mypy src/agentcat/modules/handles.py --strict` clean.
- [ ] **Step 5: Commit** — `git commit -m "feat(handles): mint/derive/extract/mint-back primitives with TS golden parity"`

---

### Task 5: Pure injection pipeline (`modules/injection.py`)

**Files:**
- Create: `src/agentcat/modules/injection.py`
- Test: `tests/test_injection.py`

**Interfaces:**
- Produces:
  - `@dataclass ToolSpec(name: str, input_schema: dict[str, Any], output_schema: dict[str, Any] | None = None)` — schemas are the adapter's DEEP COPIES; the pipeline mutates them in place.
  - `@dataclass InjectionResult(injected_params: dict[str, set[str]], output_injected: set[str])`
  - `build_injected_schemas(tools: list[ToolSpec], options: AgentCatOptions) -> InjectionResult`
  - `strip_injected_arguments(tool_name: str, arguments: dict[str, Any], registry: dict[str, set[str]] | None) -> dict[str, Any]` — always returns a new dict; `registry=None` → heuristic (strip `task_id`/`agent_id`/`context`, except `get_more_tools` keeps `context`).
  - `mcp_instructions_schema_property() -> dict[str, Any]` — the `_mcp_instructions` outputSchema fragment.
- Consumes: Task 2 constants, Task 3 options.

- [ ] **Step 1: Failing tests** (key cases; write all of these):

```python
import copy
from agentcat.modules.injection import (
    InjectionResult, ToolSpec, build_injected_schemas,
    mcp_instructions_schema_property, strip_injected_arguments,
)
from agentcat.modules import constants as c
from agentcat.types import AgentCatOptions


def spec(name="t", props=None, extra=None, out=None):
    schema = {"type": "object", "properties": dict(props or {"q": {"type": "string"}})}
    schema.update(extra or {})
    return ToolSpec(name=name, input_schema=schema, output_schema=out)


def test_param_order_and_descriptions():
    s = spec()
    r = build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    keys = list(s.input_schema["properties"])
    assert keys == ["q", "task_id", "agent_id", "context"]
    assert s.input_schema["properties"]["task_id"]["description"] == c.TASK_ID_PARAM_DESCRIPTION
    assert s.input_schema["properties"]["agent_id"]["description"] == c.AGENT_ID_PARAM_DESCRIPTION
    assert "task_id" not in s.input_schema.get("required", [])
    assert "agent_id" in s.input_schema["required"]
    assert r.injected_params["t"] == {"task_id", "agent_id", "context"}


def test_hook_mode_omits_task_id_and_switches_agent_copy():
    s = spec()
    build_injected_schemas([s], AgentCatOptions(
        enable_agent_tracking=True, resolve_task_id=lambda q, e: "x"))
    props = s.input_schema["properties"]
    assert "task_id" not in props
    assert props["agent_id"]["description"] == c.AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE


def test_tracing_disabled_skips_handles_but_not_context():
    s = spec()
    r = build_injected_schemas([s], AgentCatOptions(enable_tracing=False))
    assert set(s.input_schema["properties"]) == {"q", "context"}
    assert r.injected_params["t"] == {"context"}


def test_additional_properties_false_removed():
    s = spec(extra={"additionalProperties": False})
    build_injected_schemas([s], AgentCatOptions())
    assert "additionalProperties" not in s.input_schema


def test_composed_schema_skipped_with_empty_registry_entry():
    s = ToolSpec(name="t", input_schema={"oneOf": [{"type": "object"}]})
    r = build_injected_schemas([s], AgentCatOptions())
    assert s.input_schema == {"oneOf": [{"type": "object"}]}
    assert r.injected_params["t"] == set()


def test_collision_skips_that_param_only():
    s = spec(props={"task_id": {"type": "string", "description": "customer"}})
    r = build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    assert s.input_schema["properties"]["task_id"]["description"] == "customer"
    assert r.injected_params["t"] == {"agent_id", "context"}


def test_get_more_tools_gets_handles_but_not_context():
    s = spec(name=c.GET_MORE_TOOLS_NAME, props={"context": {"type": "string"}})
    r = build_injected_schemas([s], AgentCatOptions())
    assert r.injected_params[c.GET_MORE_TOOLS_NAME] == {"task_id"}


def test_output_schema_extension_and_registry():
    s = spec(out={"type": "object", "properties": {"answer": {"type": "string"}}})
    r = build_injected_schemas([s], AgentCatOptions())
    prop = s.output_schema["properties"][c.MCP_INSTRUCTIONS_KEY]
    assert prop["description"] == c.MCP_INSTRUCTIONS_FIELD_DESCRIPTION
    assert prop["properties"]["task_id"]["description"] == c.MCP_INSTRUCTIONS_TASK_ID_DESCRIPTION
    assert c.MCP_INSTRUCTIONS_KEY not in s.output_schema.get("required", [])
    assert r.output_injected == {"t"}
    s2 = spec(out={"oneOf": []})
    r2 = build_injected_schemas([s2], AgentCatOptions())
    assert r2.output_injected == set()


def test_determinism():
    def make():
        return [spec(), spec(name="u", extra={"additionalProperties": False})]
    a, b = make(), make()
    ra = build_injected_schemas(a, AgentCatOptions(enable_agent_tracking=True))
    rb = build_injected_schemas(b, AgentCatOptions(enable_agent_tracking=True))
    assert ra == rb and [t.input_schema for t in a] == [t.input_schema for t in b]


def test_strip_registry_driven_and_heuristic():
    args = {"q": 1, "task_id": "s", "agent_id": "a", "context": "c"}
    reg = {"t": {"task_id", "context"}}
    out = strip_injected_arguments("t", args, reg)
    assert out == {"q": 1, "agent_id": "a"} and args["task_id"] == "s"  # clone
    # Registry present but tool not in it: it was never advertised through the
    # pipeline, so nothing was injected for it — strip nothing.
    assert strip_injected_arguments("unknown", args, reg) == args
    assert strip_injected_arguments("t", args, None) == {"q": 1}
    gmt = {"context": "real", "task_id": "s"}
    assert strip_injected_arguments(c.GET_MORE_TOOLS_NAME, gmt, None) == {"context": "real"}
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Core injector:

```python
_COMPOSED_KEYS = ("oneOf", "allOf", "anyOf")


def _inject_param(schema: dict, name: str, description: str, required: bool, registry_entry: set[str]) -> None:
    props = schema.setdefault("properties", {})
    if name in props:
        write_to_log(f"Warning: tool already defines '{name}'; skipping injection")
        return
    props[name] = {"type": "string", "description": description}
    if required:
        schema.setdefault("required", [])
        if name not in schema["required"]:
            schema["required"].append(name)
    registry_entry.add(name)


def build_injected_schemas(tools, options):
    hook_mode = callable(options.resolve_task_id)
    result = InjectionResult(injected_params={}, output_injected=set())
    for tool in tools:
        entry: set[str] = set()
        result.injected_params[tool.name] = entry
        schema = tool.input_schema
        if any(k in schema for k in _COMPOSED_KEYS):
            write_to_log(f"Warning: composed input schema on '{tool.name}'; skipping injection")
            continue
        if schema.get("additionalProperties") is False:
            del schema["additionalProperties"]
        if options.enable_tracing and not hook_mode:
            _inject_param(schema, TASK_ID_PARAM, TASK_ID_PARAM_DESCRIPTION, False, entry)
        if options.enable_tracing and options.enable_agent_tracking:
            desc = AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE if hook_mode else AGENT_ID_PARAM_DESCRIPTION
            _inject_param(schema, AGENT_ID_PARAM, desc, True, entry)
        if options.enable_tool_call_context and tool.name != GET_MORE_TOOLS_NAME:
            _inject_param(schema, CONTEXT_PARAM, options.custom_context_description, False, entry)
        out = tool.output_schema
        if isinstance(out, dict):
            if any(k in out for k in _COMPOSED_KEYS):
                write_to_log(f"Warning: composed output schema on '{tool.name}'; content-only mint-back")
            elif isinstance(out.get("properties"), dict):
                if MCP_INSTRUCTIONS_KEY not in out["properties"]:
                    out["properties"][MCP_INSTRUCTIONS_KEY] = mcp_instructions_schema_property()
                    result.output_injected.add(tool.name)
    return result
```

`mcp_instructions_schema_property()` returns `{"type": "object", "description": MCP_INSTRUCTIONS_FIELD_DESCRIPTION, "properties": {"task_id": {"type": "string", "description": MCP_INSTRUCTIONS_TASK_ID_DESCRIPTION}, "agent_id": {"type": "string", "description": MCP_INSTRUCTIONS_AGENT_ID_DESCRIPTION}, "instructions": {"type": "string"}}}`. `strip_injected_arguments` per the test contract (registry present + tool listed → strip exactly the entry; registry present + tool unknown → strip nothing; registry `None` → heuristic with the `get_more_tools` context exception).

- [ ] **Step 4: Run → PASS; ruff + mypy clean.**
- [ ] **Step 5: Commit** — `git commit -m "feat(injection): pure schema-injection pipeline with registries"`

---

### Task 6: Detection & client identity

**Files:**
- Create: `src/agentcat/modules/detection.py`, `src/agentcat/modules/client_identity.py`
- Test: `tests/test_detection.py`, `tests/test_client_identity.py`

**Interfaces:**
- Produces (`detection.py`):
  - `class ServerFlavor(str, Enum)`: `LOWLEVEL_V1, LOWLEVEL_V2, OFFICIAL_FASTMCP_V1, MCPSERVER_V2, COMMUNITY_V3, COMMUNITY_V4, COMMUNITY_V2_UNSUPPORTED, UNKNOWN`
  - `@dataclass Detection(flavor: ServerFlavor, lowlevel: Any | None, fingerprint: dict[str, bool])` — `lowlevel` = the object adapters wrap (`_mcp_server` / `_lowlevel_server` / the server itself; `None` for community + unknown).
  - `detect_server(server: Any) -> Detection`
- Produces (`client_identity.py`):
  - `@dataclass ClientIdentity(name: str | None = None, version: str | None = None)`
  - `client_identity_from_meta(meta: Any) -> ClientIdentity | None` — reads `META_CLIENT_INFO_KEY`, per-field `isinstance(str)` narrowing.
  - `resolve_client_identity(meta_sources: list[Any], legacy: Callable[[], Any | None]) -> ClientIdentity` — first meta hit wins, then legacy object (accepts objects with `.name`/`.version` attrs or dicts), else empty.
  - `resolve_protocol_version(meta_sources: list[Any], fallback: str | None = None) -> str | None`
- Probes (fingerprint keys, spec §8.1): `is_fastmcp_class`, `has_local_provider`, `has_add_middleware`, `has_middleware`, `has_tool_manager`, `has_mcp_server_attr`, `has_lowlevel_server_attr`, `has_extensions`, `has_request_state_security`, `has_request_handlers`, `has_private_request_handlers`, `has_add_request_handler`, `has_request_context`.

- [ ] **Step 1: Failing tests.** Build minimal doubles — module/class names matter:

```python
from agentcat.modules.detection import Detection, ServerFlavor, detect_server


def make(name, module, **attrs):
    cls = type(name, (), {})
    cls.__module__ = module
    obj = cls()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def test_community_v4_vs_v3():
    base = dict(_local_provider=object(), add_middleware=lambda m: None, middleware=[])
    v3 = make("FastMCP", "fastmcp.server.server", **base)
    v4 = make("FastMCP", "fastmcp.server.server", **base, _extensions={}, add_extension=lambda e: None)
    assert detect_server(v3).flavor is ServerFlavor.COMMUNITY_V3
    assert detect_server(v4).flavor is ServerFlavor.COMMUNITY_V4


def test_community_v2_unsupported():
    v2 = make("FastMCP", "fastmcp.server", _mcp_server=object(), _tool_manager=object())
    assert detect_server(v2).flavor is ServerFlavor.COMMUNITY_V2_UNSUPPORTED


def test_official_flavors():
    ll1 = make("Server", "mcp.server.lowlevel.server", request_handlers={}, request_context=None)
    ll2 = make("Server", "mcp.server.lowlevel.server", _request_handlers={}, add_request_handler=lambda *a: None)
    fm1 = make("FastMCP", "mcp.server.fastmcp.server", _mcp_server=ll1, _tool_manager=object())
    ms2 = make("MCPServer", "mcp.server.mcpserver.server", _lowlevel_server=ll2, _tool_manager=object())
    assert detect_server(ll1).flavor is ServerFlavor.LOWLEVEL_V1
    d = detect_server(ll2); assert d.flavor is ServerFlavor.LOWLEVEL_V2 and d.lowlevel is ll2
    d = detect_server(fm1); assert d.flavor is ServerFlavor.OFFICIAL_FASTMCP_V1 and d.lowlevel is ll1
    d = detect_server(ms2); assert d.flavor is ServerFlavor.MCPSERVER_V2 and d.lowlevel is ll2


def test_unknown_shape_has_fingerprint():
    d = detect_server(object())
    assert d.flavor is ServerFlavor.UNKNOWN and isinstance(d.fingerprint, dict)
```

`tests/test_client_identity.py`:

```python
from agentcat.modules.client_identity import (
    ClientIdentity, client_identity_from_meta, resolve_client_identity, resolve_protocol_version,
)

KEY = "io.modelcontextprotocol/clientInfo"
PV = "io.modelcontextprotocol/protocolVersion"


def test_meta_narrowing():
    assert client_identity_from_meta({KEY: {"name": "cursor", "version": "2.1"}}) == ClientIdentity("cursor", "2.1")
    assert client_identity_from_meta({KEY: {"name": "cursor", "version": 7}}) == ClientIdentity("cursor", None)
    assert client_identity_from_meta({KEY: "junk"}) is None
    assert client_identity_from_meta(None) is None


def test_ladder_order_and_legacy():
    envelope = {KEY: {"name": "envelope", "version": "1"}}
    passthrough = {KEY: {"name": "meta", "version": "2"}}
    class Legacy:  # duck-typed clientInfo object
        name, version = "legacy", "3"
    assert resolve_client_identity([envelope, passthrough], lambda: Legacy()).name == "envelope"
    assert resolve_client_identity([None, passthrough], lambda: Legacy()).name == "meta"
    assert resolve_client_identity([None, None], lambda: Legacy()).name == "legacy"
    assert resolve_client_identity([None], lambda: (_ for _ in ()).throw(RuntimeError())) == ClientIdentity()


def test_protocol_version():
    assert resolve_protocol_version([{PV: "2026-07-28"}]) == "2026-07-28"
    assert resolve_protocol_version([None], fallback="2026-07-28") == "2026-07-28"
    assert resolve_protocol_version([{PV: 9}]) is None
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Detection order: community v2 check BEFORE official-fastmcp-v1 (both have `_mcp_server`+`_tool_manager`; discriminate on module prefix `fastmcp` vs `mcp.server.fastmcp`). `meta` access supports both Mapping (`meta.get(KEY)`) and pydantic `Meta` objects (try `.model_extra.get(KEY)` then `getattr`). Legacy accepts objects (`.name`/`.version`) or dicts. Everything wrapped so no probe can raise.
- [ ] **Step 4: Run → PASS.** Extra check: `uv run python -c "import agentcat.modules.detection, agentcat.modules.client_identity"` in BOTH envs (legacy + modern) — must import cleanly (no version-specific imports).
- [ ] **Step 5: Commit** — `git commit -m "feat(detection): per-object flavor classifier and per-request client identity ladder"`

---

### Task 7: Call-path orchestration (`modules/callpath.py`)

**Files:**
- Create: `src/agentcat/modules/callpath.py`
- Test: `tests/test_callpath.py`

**Interfaces:**
- Produces:
  - `@dataclass ResolvedCall(resolution: HandleResolution, actor: UserIdentity | None, client: ClientIdentity, protocol_version: str | None, intent: str | None)`
  - `async resolve_call(data: AgentCatData, tool_name: str, raw_arguments: dict, request: Any, extra: Any, meta_sources: list[Any], legacy_client: Callable[[], Any | None], protocol_fallback: str | None = None) -> ResolvedCall` — runs `resolve_handles`, the customer `identify` hook (never raises), the client ladder; captures `intent = raw_arguments.get("context")` when a str.
  - `async get_stripped_arguments(data: AgentCatData, options, tool_name: str, raw_arguments: dict, rebuild: Callable[[], Awaitable[list[ToolSpec]]] | None) -> dict` — registry from `data.injected_params_registry`; on `None` awaits `rebuild()` → `build_injected_schemas` → stores registries on `data`; on failure logs + heuristic strip (and sets `data.output_injection_registry = None` so the mirror gate is bypassed).
  - `detect_mrtr(result_type: str | None, has_input_responses: bool) -> str | None` → `"input_required" | "continuation" | None`.
  - `decorate_content(content: list[Any] | None, res: HandleResolution, make_text_block: Callable[[str], Any]) -> list[Any] | None` — returns a NEW list with the trailing block appended, or `None` when no decoration applies (not minted / hook mode / content not a list).
  - `structured_mirror(sc: Any, res: HandleResolution, tool_name: str, output_registry: set[str] | None) -> Any | None` — gate: mirror when `output_registry is None` (rebuild failed) or `tool_name in output_registry`; delegates to `mirror_into_structured_content`.
  - `async publish_tool_call_event(server_key: Any, data: AgentCatData, rc: ResolvedCall, tool_name: str, raw_arguments: dict, response: Any, is_error: bool, error_msg: str | None, duration_ms: int | None, mrtr: str | None, extra_params: dict | None) -> None` — builds `UnredactedEvent(event_type="mcp:tools/call", session_id=rc.resolution.task_id, resource_name=tool_name, user_intent=rc.intent, parameters={"arguments": raw_arguments, **(extra_params or {})}, response=response, is_error=is_error, error=..., client_name=rc.client.name, client_version=rc.client.version, identify_actor_given_id=..., ...)`, resolves customer tags/properties via `attach_event_metadata`, then merges `build_handle_tags(rc.resolution, rc.protocol_version, mrtr)` OVER them (SDK wins; exempt from the 50-tag cap — merge after `validate_tags`), and `event_queue.publish_event(server_key, event)`.
- Consumes: Tasks 4–6 symbols exactly as defined there.

- [ ] **Step 1: Failing tests** — pure-python fakes, no MCP imports. Cover: identify hook error → `actor is None`; `intent` captured; strip with registry / rebuild-success (fake rebuild returns `ToolSpec`s and registries land on `data`) / rebuild-raise → heuristic + `output_injection_registry is None`; `detect_mrtr` matrix; `decorate_content` minted vs supplied vs hook vs non-list content (append also happens on `is_error` results — `decorate_content` never looks at error state); `structured_mirror` gate matrix (in registry / not in registry / registry None); `publish_tool_call_event` → capture `event_queue.publish_event` with monkeypatch and assert `session_id`, raw args, SDK tag merge collision (customer tag `agentcat_task_id_source:"fake"` is overridden), 50-cap exemption (50 customer tags + SDK tags all present). Anchor style:

```python
from agentcat.modules.callpath import decorate_content, detect_mrtr, get_stripped_arguments
from agentcat.modules.handles import HandleResolution
from agentcat.modules.injection import ToolSpec
from agentcat.types import AgentCatData, AgentCatOptions


def test_detect_mrtr_matrix():
    assert detect_mrtr("input_required", False) == "input_required"
    assert detect_mrtr("input_required", True) == "input_required"  # intermediate wins
    assert detect_mrtr(None, True) == "continuation"
    assert detect_mrtr("complete", False) is None


def test_decorate_content_only_when_minted_prompted():
    minted = HandleResolution("ses_T", "minted")
    out = decorate_content([{"type": "text", "text": "x"}], minted, lambda t: {"type": "text", "text": t})
    assert out is not None and "[MCP INSTRUCTIONS]: task_id issued." in out[-1]["text"]
    assert decorate_content([{"t": 1}], HandleResolution("ses_T", "supplied"), dict) is None
    assert decorate_content([{"t": 1}], HandleResolution("ses_T", "minted", hook_mode=True), dict) is None
    assert decorate_content("not-a-list", minted, dict) is None


async def test_rebuild_failure_falls_back_to_heuristic(caplog):
    data = AgentCatData(project_id="p", options=AgentCatOptions())
    async def broken_rebuild():
        raise RuntimeError("list source gone")
    out = await get_stripped_arguments(data, data.options, "t",
                                       {"q": 1, "task_id": "s", "context": "c"}, broken_rebuild)
    assert out == {"q": 1}
    assert data.output_injection_registry is None  # mirror gate bypassed
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** exactly per the Interfaces block. Reuse `identify` logic from `modules/identify.py` but WITHOUT publishing an event (new helper `resolve_identity(data, request, extra) -> UserIdentity | None` added to `identify.py`; old `identify_session` stays until Task 11).
- [ ] **Step 4: Run → PASS; ruff + mypy clean.**
- [ ] **Step 5: Commit** — `git commit -m "feat(callpath): shared per-call orchestration (resolve/strip/decorate/publish)"`

### Task 8: Lowlevel-v1 adapter + `track()` cutover (official SDK 1.x flavors)

**Files:**
- Create: `src/agentcat/modules/adapters/__init__.py`, `src/agentcat/modules/adapters/lowlevel_v1.py`
- Modify: `src/agentcat/__init__.py` (full `track()` rewrite), `src/agentcat/modules/request_extra.py` (expose `extra_from_request_context(request_context) -> dict`), `src/agentcat/modules/identify.py` (add `resolve_identity`), `tests/conftest.py` (env gating)
- Delete: `src/agentcat/modules/overrides/official/` (whole dir)
- Test: `tests/test_lowlevel_v1_handles.py` (new); rewrite `tests/test_tool_context.py`, `tests/test_report_missing.py`, `tests/test_event_capture_completeness.py`, `tests/test_mcp_version_compatibility.py`, `tests/test_dynamic_tracking.py`, `tests/test_multiple_servers.py`, `tests/e2e/official/*`

**Interfaces:**
- Produces: `install_lowlevel_v1(server: Any, data: AgentCatData) -> None` (wraps type-keyed `request_handlers`); `track()` v2: detection-dispatched, never raises; `agentcat` imports cleanly under BOTH mcp majors (all mcp/fastmcp imports lazy).
- Consumes: `detect_server` (Task 6), `resolve_call`/`get_stripped_arguments`/`decorate_content`/`structured_mirror`/`publish_tool_call_event`/`detect_mrtr` (Task 7), `ToolSpec`/`build_injected_schemas` (Task 5).

- [ ] **Step 1: conftest env gating.** In `tests/conftest.py`:

```python
from importlib.metadata import version

MCP_MAJOR = int(version("mcp").split(".")[0])
_LEGACY_ONLY = [
    "e2e/official", "e2e/community_v2", "e2e/community_v3", "community",
    "test_tool_context.py", "test_report_missing.py", "test_dynamic_tracking.py",
    "test_multiple_servers.py", "test_event_capture_completeness.py",
    "test_mcp_version_compatibility.py", "test_session.py", "test_stateless.py",
    "test_request_extra.py", "test_lowlevel_v1_handles.py",
]
_MODERN_ONLY = ["e2e/official_modern", "e2e/community_v4", "test_lowlevel_v2_handles.py"]
collect_ignore_glob = [
    f"{p}*" for p in (_MODERN_ONLY if MCP_MAJOR < 2 else _LEGACY_ONLY)
]
```

- [ ] **Step 2: Failing integration test** `tests/test_lowlevel_v1_handles.py` (legacy env), using `tests/test_utils/todo_server.py` + `client.py`:

```python
import json
import pytest
from agentcat import AgentCatOptions, track
from tests.test_utils.client import create_test_client
from tests.test_utils.todo_server import create_todo_server


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    events = []
    from agentcat.modules import event_queue
    monkeypatch.setattr(event_queue.event_queue, "add_event", events.append)
    return events


async def test_prompted_mode_end_to_end(capture):
    server = create_todo_server()
    track(server, "proj_test", AgentCatOptions())
    async with create_test_client(server) as client:
        listed = await client.list_tools()
        add = next(t for t in listed.tools if t.name == "add_todo")
        assert list(add.inputSchema["properties"])[-2:] == ["task_id", "context"]
        assert "task_id" not in add.inputSchema.get("required", [])
        assert any(t.name == "get_more_tools" for t in listed.tools)

        r1 = await client.call_tool("add_todo", {"text": "hi", "context": "Adding a todo item for the user's task list to track work"})
        text = "".join(c.text for c in r1.content if hasattr(c, "text"))
        assert "[MCP INSTRUCTIONS]: task_id issued." in text
        minted = text.split("task_id=")[1].split(" ")[0]
        assert minted.startswith("ses_")

        r2 = await client.call_tool("add_todo", {"text": "again", "task_id": minted})
        text2 = "".join(c.text for c in r2.content if hasattr(c, "text"))
        assert "[MCP INSTRUCTIONS]: task_id issued." not in text2

    call_events = [e for e in capture if e.event_type == "mcp:tools/call"]
    assert {e.event_type for e in capture} <= {"mcp:tools/call"}  # no initialize/list/identify
    assert call_events[0].session_id == minted == call_events[1].session_id
    assert call_events[0].parameters["arguments"]["context"]  # raw request keeps injected params
    assert "[MCP INSTRUCTIONS]" not in json.dumps(call_events[0].response)  # undecorated


async def test_handler_sees_stripped_args_and_customer_result_untouched(capture):
    server = create_todo_server()
    seen = {}
    # register a probe tool BEFORE track (todo_server helper or direct decorator)
    track(server, "proj_test")
    # call with task_id/context; assert the tool body received neither (todo_server
    # tools raise on unexpected kwargs, so a successful call is the assertion).
```

Also: `test_track_never_raises()` — `track(object(), None)` returns the object, no exception; `test_agent_tracking_injection()` — with `enable_agent_tracking=True`, `agent_id` in required + tags `agentcat_agent_id` on events when supplied; `test_hook_mode()` — `resolve_task_id=lambda q, e: "cust-1"` → no `task_id` param in schemas, event session = `derive_task_id("cust-1", "proj_test")`, no `[MCP INSTRUCTIONS]` text ever.

- [ ] **Step 3: Run → FAIL.**

- [ ] **Step 4: Implement `adapters/lowlevel_v1.py`:**

```python
"""Adapter for official MCP SDK 1.x lowlevel Server (also serves
mcp.server.fastmcp.FastMCP via its _mcp_server). All mcp imports are lazy so
the package imports under either SDK major."""


def install_lowlevel_v1(server, data):
    from mcp.types import (CallToolRequest, CallToolResult, ListToolsRequest,
                           ListToolsResult, ServerResult, TextContent, Tool)
    from agentcat.modules.callpath import (decorate_content, detect_mrtr,
        get_stripped_arguments, publish_tool_call_event, resolve_call, structured_mirror)
    from agentcat.modules.injection import ToolSpec, build_injected_schemas
    from agentcat.modules.tools import GET_MORE_TOOLS_SCHEMA, handle_report_missing

    handlers = server.request_handlers
    orig_list = handlers.get(ListToolsRequest)
    orig_call = handlers.get(CallToolRequest)

    def make_gmt() -> Tool:
        # Reuse the existing description string and GET_MORE_TOOLS_SCHEMA from
        # modules/tools.py exactly as-is (the bytes already match the TS SDK).
        return Tool(
            name="get_more_tools",
            description=GET_MORE_TOOLS_DESCRIPTION,
            inputSchema=copy.deepcopy(GET_MORE_TOOLS_SCHEMA),
            annotations={"readOnlyHint": True},
        )

    async def list_specs() -> tuple[list[Tool], list[ToolSpec]]:
        tools: list[Tool] = []
        if orig_list is not None:
            result = await orig_list(ListToolsRequest(method="tools/list"))
            tools = [t.model_copy(deep=True) for t in result.root.tools]
        if data.options.enable_report_missing and all(t.name != "get_more_tools" for t in tools):
            tools.append(make_gmt())
        specs = [ToolSpec(t.name, t.inputSchema, getattr(t, "outputSchema", None)) for t in tools]
        return tools, specs

    async def rebuild() -> list[ToolSpec]:
        _, specs = await list_specs()
        return specs

    async def wrapped_list(req):
        tools, specs = await list_specs()
        result = build_injected_schemas(specs, data.options)
        data.injected_params_registry = result.injected_params
        data.output_injection_registry = result.output_injected
        for tool, s in zip(tools, specs):
            tool.inputSchema = s.input_schema
            if s.output_schema is not None:
                tool.outputSchema = s.output_schema
        return ServerResult(ListToolsResult(tools=tools))

    async def wrapped_call(req):
        name = req.params.name
        raw_args = dict(req.params.arguments or {})
        ctx = _safe_request_context(server)
        rc = await resolve_call(
            data, name, raw_args, req, ctx,
            meta_sources=[_meta_extras(req)],
            legacy_client=lambda: _legacy_client_info(ctx),
        )
        stripped = await get_stripped_arguments(data, data.options, name, raw_args, rebuild)
        start = _now_ms()
        if name == "get_more_tools" and data.options.enable_report_missing:
            inner = CallToolResult(content=await handle_report_missing(stripped.get("context", "")))
        else:
            new_params = req.params.model_copy(update={"arguments": stripped})
            inner = (await orig_call(req.model_copy(update={"params": new_params}))).root
        mrtr = detect_mrtr(getattr(inner, "resultType", None), False)
        is_err = bool(getattr(inner, "isError", False))
        await publish_tool_call_event(
            server, data, rc, name, raw_args,
            response=inner.model_dump(mode="json"), is_error=is_err,
            error_msg=_first_text(inner) if is_err else None,
            duration_ms=_now_ms() - start, mrtr=mrtr,
            extra_params=extra_from_request_context(ctx),
        )
        if mrtr == "input_required":
            return ServerResult(inner)
        update: dict = {}
        new_content = decorate_content(
            list(inner.content) if isinstance(inner.content, list) else None,
            rc.resolution, lambda t: TextContent(type="text", text=t))
        if new_content is not None:
            update["content"] = new_content
        mirrored = structured_mirror(getattr(inner, "structuredContent", None),
                                     rc.resolution, name, data.output_injection_registry)
        if mirrored is not None:
            update["structuredContent"] = mirrored
        return ServerResult(inner.model_copy(update=update) if update else inner)

    handlers[ListToolsRequest] = wrapped_list
    handlers[CallToolRequest] = wrapped_call
```

(`_meta_extras(req)` = `req.params.meta.model_extra` when present; `_legacy_client_info(ctx)` = `ctx.session.client_params.clientInfo` guarded; `decorate_content` internally applies only when `build_mint_back_text` is non-None — keep the single source of truth in callpath, drop the local `text` variable if redundant.)

- [ ] **Step 5: Rewrite `track()` in `__init__.py`:** keep diagnostics/exporters/api-url blocks; replace compatibility checks + `_apply_server_tracking` with:

```python
detection = detect_server(server)
if detection.flavor in (ServerFlavor.LOWLEVEL_V1, ServerFlavor.OFFICIAL_FASTMCP_V1):
    lowlevel = detection.lowlevel
    data = AgentCatData(
        project_id=project_id, options=options,
        server_name=getattr(lowlevel, "name", None),
        server_version=getattr(lowlevel, "version", None),
    )
    set_server_tracking_data(lowlevel, data)
    from agentcat.modules.adapters.lowlevel_v1 import install_lowlevel_v1
    install_lowlevel_v1(lowlevel, data)
elif detection.flavor is ServerFlavor.COMMUNITY_V3:
    data = AgentCatData(project_id=project_id, options=options,
                        server_name=getattr(server, "name", None), server_version=None)
    set_server_tracking_data(server, data)
    from agentcat.modules.overrides.community_v3.integration import apply_community_v3_integration
    apply_community_v3_integration(server, data)   # legacy path; replaced by Task 9
elif detection.flavor is ServerFlavor.COMMUNITY_V2_UNSUPPORTED:
    write_to_log("FastMCP 2.x is not supported by agentcat>=2 — pin agentcat<2 or upgrade FastMCP")
elif detection.flavor in (ServerFlavor.LOWLEVEL_V2, ServerFlavor.MCPSERVER_V2, ServerFlavor.COMMUNITY_V4):
    write_to_log(f"{detection.flavor} support arrives in Tasks 12/13; server not tracked yet")
else:
    # Unrecognized shape: log fingerprint AND emit a diagnostics beacon
    # (fleet-level drift detection, changelog §6.7).
    write_to_log(f"Unrecognized server shape; not tracked | fingerprint={detection.fingerprint}")
```

The whole body sits in one `try/except Exception` that logs and returns `server` — including the former `ValueError`/`TypeError` paths (breaking change: never raises).

- [ ] **Step 6: Make imports lazy.** `rg -n "^from mcp|^import mcp|^from fastmcp|^import fastmcp" src/agentcat` — move every hit inside functions or `TYPE_CHECKING`. Then verify in the MODERN env: `uv run python -c "import agentcat; print(agentcat.__version__)"` → prints `2.0.0b1`.

- [ ] **Step 7: Update the legacy-env test files listed above.** Delete assertions about `mcp:initialize` / `mcp:tools/list` / `agentcat:identify` events and session rollover; identify tests now assert actor fields ON the tools/call event; e2e/official `test_session_http.py` → `test_task_http.py` asserting mint-back + echo across HTTP calls. Run: `uv run pytest -q` (legacy env) → PASS.

- [ ] **Step 8: Commit** — `git commit -m "feat(adapters)!: lowlevel-v1 engine cutover; official FastMCP unified; track() never raises"`

---

### Task 9: Community adapter (FastMCP 3.x era) + retire overrides/

**Files:**
- Create: `src/agentcat/modules/adapters/community.py`
- Modify: `src/agentcat/__init__.py` (route COMMUNITY_V3/V4 → new adapter), `src/agentcat/modules/tools.py` (readOnlyHint on the FastMCP registration)
- Delete: `src/agentcat/modules/overrides/` (entire tree: `mcp_server.py`, `community/`, `community_v3/`, `__init__.py`)
- Test: `tests/community/test_community_v3_handles.py` (new); rewrite `tests/community/test_community_v3_inject_context.py`, `test_community_v3_event_serialization.py`, `test_community_v3_openapi.py`, `test_community_v3_runtime_state_tools.py`, `tests/e2e/community_v3/*`; delete `tests/e2e/community_v2/`, `tests/community/test_community_fastmcp.py`, `test_community_event_capture.py`, `test_community_tool_context.py`, `test_community_report_missing.py`, `test_community_dynamic_tracking.py`, `test_community_tracking_timing.py` (fastmcp-2-era suites; port any still-relevant scenario onto the v3 harness first)

**Interfaces:**
- Produces: `install_community(server: Any, data: AgentCatData, era: int) -> None` — constructs `AgentCatMiddleware(data, server, era)` and `server.middleware.insert(0, mw)`; registers `get_more_tools` via `server.add_tool(Tool.from_function(...))` with `annotations={"readOnlyHint": True}`.
- Consumes: Tasks 4–7 exactly as defined.

- [ ] **Step 1: Failing tests** (legacy env = fastmcp 3.x). Mirror Task 8's integration shape using the community harness (`tests/test_utils/community_todo_server.py` + `community_client.py`): schema injection order on `tools/list`; mint-back on first call; echo on second; raw-vs-stripped; hook mode; agent tracking; **only tools/call events**; OpenAPI tool coverage (port from the old inject-context test: injected `context` on OpenAPI-generated tools without breaking pickling).

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement `AgentCatMiddleware`** (single class, era flag):

```python
class AgentCatMiddleware(Middleware):        # fastmcp import inside install_community
    def __init__(self, data, server, era: int):
        self._data, self._server, self._era = data, server, era
        self._handshake_client: dict | None = None

    async def on_initialize(self, context, call_next):
        params = getattr(context.message, "params", None) or context.message
        info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        if info is not None:
            self._handshake_client = {"name": getattr(info, "name", None),
                                      "version": getattr(info, "version", None)}
        return await call_next(context)      # NO event

    async def on_list_tools(self, context, call_next):
        tools = list(await call_next(context))
        copies = [t.model_copy(deep=True) for t in tools]
        specs = [ToolSpec(t.name, t.parameters, getattr(t, "output_schema", None)) for t in copies]
        result = build_injected_schemas(specs, self._data.options)
        self._data.injected_params_registry = result.injected_params
        self._data.output_injection_registry = result.output_injected
        return [t.model_copy(update={"parameters": s.input_schema,
                                     **({"output_schema": s.output_schema} if s.output_schema is not None else {})})
                for t, s in zip(copies, specs)]

    async def on_call_tool(self, context, call_next):
        msg = context.message
        name, raw_args = msg.name, dict(msg.arguments or {})
        meta = self._request_meta(context)   # fastmcp_context.request_context.meta, guarded
        rc = await resolve_call(self._data, name, raw_args, msg, context.fastmcp_context,
                                meta_sources=[meta],
                                legacy_client=lambda: self._handshake_client)
        async def rebuild():
            listed = await self._server.list_tools(run_middleware=False)
            return [ToolSpec(t.name, copy.deepcopy(t.parameters),
                             copy.deepcopy(getattr(t, "output_schema", None))) for t in listed]
        stripped = await get_stripped_arguments(self._data, self._data.options, name, raw_args, rebuild)
        new_msg = msg.model_copy(update={"arguments": stripped})   # preserves input_responses/request_state/_meta
        start = _now_ms()
        result = await call_next(context.copy(message=new_msg))
        continuation = getattr(msg, "input_responses", None) is not None
        intermediate = type(result).__name__ == "InputRequiredToolResult" or \
            getattr(result, "result_type", None) == "input_required"
        mrtr = "input_required" if intermediate else ("continuation" if continuation else None)
        await publish_tool_call_event(self._server, self._data, rc, name, raw_args,
                                      response=_tool_result_payload(result), is_error=bool(getattr(result, "is_error", False)),
                                      error_msg=None, duration_ms=_now_ms() - start, mrtr=mrtr,
                                      extra_params=_extra_from_fastmcp(context.fastmcp_context))
        if intermediate:
            return result
        update = {}
        new_content = decorate_content(list(result.content) if isinstance(getattr(result, "content", None), list) else None,
                                       rc.resolution, _make_text_content)
        if new_content is not None:
            update["content"] = new_content
        mirrored = structured_mirror(getattr(result, "structured_content", None),
                                     rc.resolution, name, self._data.output_injection_registry)
        if mirrored is not None:
            update["structured_content"] = mirrored
        return result.model_copy(update=update) if update else result
```

`get_more_tools` keeps flowing through the provider (`server.add_tool`), so its calls traverse this middleware and publish like any tool.

- [ ] **Step 4: Route in `track()`** (`COMMUNITY_V3` → `install_community(server, data, era=3)`; leave `COMMUNITY_V4` on the placeholder branch until Task 13), delete `modules/overrides/`, fix stragglers (`rg -n "overrides" src tests`).
- [ ] **Step 5: Run legacy env full suite → PASS.** Commit — `git commit -m "feat(adapters)!: community FastMCP middleware on the shared engine; drop FastMCP 2.x"`

---

### Task 10: Session/identify teardown + event pipeline restamp + migration docs

**Files:**
- Delete: `src/agentcat/modules/session.py`, `src/agentcat/modules/compatibility.py`, `src/agentcat/modules/version_detection.py`, `tests/test_session.py`, `tests/test_stateless.py`
- Modify: `src/agentcat/types.py` (remove `AgentCatOptions.stateless`, `SessionInfo` deletion, `AgentCatData` slim per spec §3.3, `EventType` → `MCP_TOOLS_CALL` + `AGENTCAT_CUSTOM` only), `src/agentcat/__init__.py` (drop `_detect_stateless`, session imports), `src/agentcat/modules/event_queue.py` (`publish_event` stamps `sdk_language="python"`, `agentcat_version=__version__`, `server_name`/`server_version` from `AgentCatData`; no `get_session_info` merge; no `set_last_activity`), `src/agentcat/modules/identify.py` (delete `identify_session`; keep `resolve_identity`), `src/agentcat/modules/tools.py` (drop version_detection gates), `MIGRATION.md`, `README.md`
- Test: `tests/test_event_queue.py` (update stamping assertions), `tests/test_request_extra.py` (transport `mcp-session-id` still lands in `parameters.extra.sessionId`; AgentCat `session_id` unaffected)

- [ ] **Step 1: Update tests first** (stamping + request-extra semantics), run → FAIL.
- [ ] **Step 2: Implement removals/restamp.** `rg -n "session_info|get_session_info|last_activity|INACTIVITY|is_stateless|identify_session|SessionInfo" src tests` must return zero hits in `src/` afterward.
- [ ] **Step 3: MIGRATION.md — add section "agentcat 1.x → 2.0":** sessions→tasks table (`Event.sessionId` now carries the task handle, same `ses_` prefix); removed events (`mcp:initialize`, `mcp:tools/list`, `agentcat:identify` — actor fields now ride every tools/call event); removed `AgentCatOptions.stateless`; `identify` runs per call (keep hooks cheap); `track()` no longer raises; FastMCP 2.x unsupported (pin `agentcat<2`); new: `task_id`/`agent_id` injection, `enable_agent_tracking`, `resolve_task_id`, `publish_custom_event`; supported matrix (mcp 1.x/2.x, FastMCP 3.x/4.x).
- [ ] **Step 4: Run both envs' importable checks + legacy full suite → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat!: remove session machinery and identify/initialize/list events"`

---

### Task 11: `publish_custom_event`

**Files:**
- Modify: `src/agentcat/__init__.py`, `src/agentcat/types.py` (only if `CustomEventData` needs export wiring)
- Test: `tests/test_publish_custom_event.py`

**Interfaces:**
- Produces: `publish_custom_event(server_or_task_id: Any, project_id: str, event_data: CustomEventData | None = None) -> None`, exported in `__all__`.

- [ ] **Step 1: Failing tests:** string form uses the string verbatim as `session_id` (never derived); `event_data["task_id"]` takes precedence over the string; tracked-server form without `task_id` → `session_id` empty/None; event_type == `"agentcat:custom"`; `resource_name`/`tags`/`properties`/`is_error` pass through; bad input never raises.
- [ ] **Step 2: Run → FAIL. Step 3: Implement** (build `UnredactedEvent(event_type=AGENTCAT_CUSTOM_EVENT_TYPE, session_id=..., ...)`; tracked-server form resolves `AgentCatData` via `get_server_tracking_data` for project fallback; wrap in try/except + log). **Step 4: Run → PASS. Step 5: Commit** — `git commit -m "feat(events): publish_custom_event with verbatim task attribution"`

---

### Task 12: Lowlevel-v2 adapter (official SDK 2.x + MCPServer)

**Files:**
- Create: `src/agentcat/modules/adapters/lowlevel_v2.py`
- Modify: `src/agentcat/__init__.py` (route `LOWLEVEL_V2` / `MCPSERVER_V2`), `src/agentcat/modules/request_extra.py` (add `extra_from_server_context(ctx) -> dict`: headers from `ctx.request` when HTTP; keep `sessionId` only when transport provides one)
- Test: `tests/test_lowlevel_v2_handles.py`, `tests/e2e/official_modern/{conftest.py,test_task_http.py,test_mcpserver_http.py}` (modern env only)

**Interfaces:**
- Produces: `install_lowlevel_v2(server: Any, data: AgentCatData) -> None` — swaps `_request_handlers["tools/list"|"tools/call"]` entries and re-arms `add_request_handler`.
- Consumes: Tasks 4–7 symbols; era specifics: snake_case fields, `ctx.meta`, `ctx.protocol_version`, MRTR types.

- [ ] **Step 1: Failing tests (modern env).** Unit: real `mcp.server.lowlevel.Server` with a `tools/call` + `tools/list` handler registered via v2 API; assert injection order, mint-back, echo, strip, raw event, `agentcat_protocol_version` tag, MRTR: a handler returning `InputRequiredResult` → event tagged `input_required`, result NOT decorated; a continuation call (`params.input_responses={...}`) → tagged `continuation`; re-registration after `track()` still wrapped; `MCPServer` end-to-end via `_lowlevel_server`. E2E: streamable HTTP modern path (mirror `tests/e2e/official/conftest.py` boot pattern with the v2 app API).
- [ ] **Step 2: Run (modern env) → FAIL.**
- [ ] **Step 3: Implement:**

```python
def install_lowlevel_v2(server, data):
    # No mcp imports needed: HandlerEntry is cloned via type(entry)(...).
    from agentcat.modules.callpath import ...
    from agentcat.modules.injection import ToolSpec, build_injected_schemas

    def swap(method: str, wrap):
        entry = server.get_request_handler(method)
        if entry is None:
            return None
        original = entry.handler
        server._request_handlers[method] = type(entry)(entry.params_type, wrap(original))
        return original

    def wrap_list(original):
        async def wrapped(ctx, params):
            result = await original(ctx, params)
            tools = [t.model_copy(deep=True) for t in result.tools]
            if data.options.enable_report_missing and all(t.name != "get_more_tools" for t in tools):
                tools.append(_make_gmt_v2())          # mcp.types.Tool imported lazily here
            specs = [ToolSpec(t.name, t.input_schema, t.output_schema) for t in tools]
            r = build_injected_schemas(specs, data.options)
            data.injected_params_registry = r.injected_params
            data.output_injection_registry = r.output_injected
            for t, s in zip(tools, specs):
                t.input_schema = s.input_schema
                if s.output_schema is not None:
                    t.output_schema = s.output_schema
            return result.model_copy(update={"tools": tools})
        return wrapped

    def wrap_call(original):
        async def wrapped(ctx, params):
            name, raw_args = params.name, dict(params.arguments or {})
            rc = await resolve_call(
                data, name, raw_args, params, ctx,
                meta_sources=[ctx.meta],
                legacy_client=lambda: _v2_legacy_client(ctx),   # ctx.session.client_params.client_info
                protocol_fallback=getattr(ctx, "protocol_version", None),
            )
            async def rebuild():
                entry = ...  # captured original list handler; call with (ctx, None)
                listed = await original_list(ctx, None)
                return [ToolSpec(t.name, t.input_schema.copy() if isinstance(t.input_schema, dict) else {}, ...)]
            stripped = await get_stripped_arguments(data, data.options, name, raw_args, rebuild)
            call_params = params.model_copy(update={"arguments": stripped})  # keeps input_responses/request_state/_meta
            start = _now_ms()
            if name == "get_more_tools" and data.options.enable_report_missing:
                result = _gmt_result_v2(stripped)
            else:
                result = await original(ctx, call_params)
            mrtr = detect_mrtr(getattr(result, "result_type", None),
                               getattr(params, "input_responses", None) is not None)
            is_err = bool(getattr(result, "is_error", False))
            await publish_tool_call_event(server, data, rc, name, raw_args,
                response=result.model_dump(mode="json"), is_error=is_err,
                error_msg=None, duration_ms=_now_ms() - start, mrtr=mrtr,
                extra_params=extra_from_server_context(ctx))
            if mrtr == "input_required":
                return result
            update = {}
            new_content = decorate_content(list(result.content) if isinstance(result.content, list) else None,
                                           rc.resolution, _make_text_content_v2)
            if new_content is not None:
                update["content"] = new_content
            mirrored = structured_mirror(result.structured_content if isinstance(result.structured_content, dict) else None,
                                         rc.resolution, name, data.output_injection_registry)
            if mirrored is not None:
                update["structured_content"] = mirrored
            return result.model_copy(update=update) if update else result
        return wrapped

    original_list = swap("tools/list", wrap_list)
    swap("tools/call", wrap_call)

    original_add = server.add_request_handler
    def rearming_add(method, params_type, handler):
        original_add(method, params_type, handler)
        if method in ("tools/list", "tools/call"):
            swap(method, wrap_list if method == "tools/list" else wrap_call)
    server.add_request_handler = rearming_add   # instance attribute shadows the method
```

- [ ] **Step 4: Route in `track()`** (replace the placeholder branch); run modern-env suite → PASS; run legacy env → still PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(adapters): official MCP SDK v2 support (lowlevel + MCPServer)"`

---

### Task 13: Community FastMCP 4 era

**Files:**
- Modify: `src/agentcat/__init__.py` (route `COMMUNITY_V4` → `install_community(server, data, era=4)`), `src/agentcat/modules/adapters/community.py` (only if a v4-specific fix surfaces — the era paths were built in Task 9)
- Test: `tests/e2e/community_v4/{conftest.py,test_task_http.py}`, `tests/test_community_v4_handles.py` (modern env)

- [ ] **Step 1: Failing tests (modern env, fastmcp 4.0.0b1):** injection/mint-back/echo end-to-end over `http_app`; **strip preserves MRTR fields** (build a `CallToolRequestParams` with `input_responses={"r1": ...}` + `request_state="s"`, assert the params object reaching the tool retains both after stripping); `InputRequiredToolResult` passes undecorated with `agentcat_mrtr=input_required`; ordering vs `ResponseCachingMiddleware` (register caching, call list twice, injected schema present both times); OpenAPI-generated tools still injectable.
- [ ] **Step 2: Run → FAIL** (routing placeholder). **Step 3:** enable routing; fix any v4 snake/camel drift the tests surface. **Step 4: Run modern env → PASS; legacy env → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(adapters): community FastMCP 4 era support"`

---

### Task 14: Cross-flavor regressions (concurrency, removed events, rebuild)

**Files:**
- Test: `tests/test_concurrency_handles.py`, `tests/test_removed_events.py`, `tests/test_rebuild_on_demand.py`

- [ ] **Step 1: Concurrency** — per flavor available in the active env: `asyncio.gather` 25 simultaneous calls, each supplying a distinct `task_id`; assert each captured event's `session_id` matches its own call's handle (no cross-attribution) and each response's mint-back/mirror names only its own handle.
- [ ] **Step 2: Removed events** — full lifecycle (initialize where applicable, tools/list, tools/call ×2) per flavor; assert captured event types `== {"mcp:tools/call"}`.
- [ ] **Step 3: Rebuild-on-demand** — fresh tracked server, call `tools/call` FIRST (no prior list): registry gets rebuilt, customer handler receives stripped args, mirror gating uses the rebuilt output registry. Also the failure path: monkeypatch the list source to raise → heuristic strip still protects `get_more_tools.context`, mirror applies anyway.
- [ ] **Step 4: Run in BOTH envs → PASS. Commit** — `git commit -m "test: concurrency, removed-event, and rebuild regressions across flavors"`

---

### Task 15: CI matrices, docs polish, final validation

**Files:**
- Modify: `.github/workflows/mcp-compatibility.yml`, `.github/workflows/mcp-prerelease-compatibility.yml`, `README.md`, `MIGRATION.md` (final read-through)

- [ ] **Step 1: `mcp-compatibility.yml`:** mcp discovery filter accepts `>=1.2,<3` (drop the implicit `<2`); fastmcp discovery accepts `>=3.0,<5`, remove the `2.9.*` carve-out; the fastmcp job's `sed` pins now target the community extra + groups from Task 1; add matrix legs that run `uv sync --group mcp-legacy` and default (modern) with the full suite (conftest gating selects the right subsets).
- [ ] **Step 2: `mcp-prerelease-compatibility.yml`:** discovery uses `pip index versions --pre` for `mcp` 2.x and `fastmcp` 4.x prerelease channels.
- [ ] **Step 3: README:** update quickstart for v2 (task handles paragraph, `enable_agent_tracking`, `resolve_task_id`, `publish_custom_event`, supported matrix, `track()`-inside-factory pattern note).
- [ ] **Step 4: Final validation (both envs):**

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src/agentcat --strict
uv run pytest -q                                   # modern env
uv sync --extra community --no-group mcp-modern --group mcp-legacy && uv run pytest -q   # legacy env
uv run hatch build && ls dist/
```

Expected: all green; wheel + sdist for `agentcat-2.0.0b1`.

- [ ] **Step 5: Commit** — `git commit -m "ci: dual-generation compatibility matrices for mcp 2.x and fastmcp 4"`

---

## Execution notes

- Tasks 1–11 are fully testable in the legacy env (plus import smoke in modern); Tasks 12–14 need the modern env; Task 15 exercises both.
- If any golden vector or byte-parity test fails, the reference implementation wins: diff against the TS SDK before touching the expected values (they are frozen cross-SDK contracts).
- The changelog's §8 rollout order maps to Tasks 4→7 (primitives/resolution), 5 (injection), 8–9 (wiring + removals), 5/7 (structured mint-back), 3/4 (agent_id), 12–14 (2026-era topology).

