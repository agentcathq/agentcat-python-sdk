import os
from importlib.metadata import version

# Belt-and-suspenders: force diagnostics off before any test imports/runs, so no
# test ordering or future change to the auto-disable detection can ever ship OTLP
# diagnostics to the live collector from our own suite. Diagnostics-specific tests
# opt back in explicitly with DISABLE_DIAGNOSTICS=false plus mocked HTTP.
os.environ["DISABLE_DIAGNOSTICS"] = "true"


# ── Per-era collection gating ────────────────────────────────────────────────
# The suite runs under two mutually exclusive dependency sets (see pyproject's
# `mcp-legacy` / `mcp-modern` groups). Era-specific test modules import symbols
# that exist on only one side, so each era's collection skips the other's.
MCP_MAJOR = int(version("mcp").split(".")[0])

_LEGACY_ONLY = (
    "e2e/official",
    "e2e/community_v3",
    "community",
    "test_tool_context.py",
    "test_report_missing.py",
    "test_dynamic_tracking.py",
    "test_multiple_servers.py",
    "test_event_capture_completeness.py",
    "test_request_extra.py",
    "test_lowlevel_v1_handles.py",
    # Every test in this module drives a tracked mcp 1.x server end to end, so
    # there is no era-agnostic remainder to rescue: the whole file is gated.
    # `test_event_tags_properties.py` and `test_exceptions.py` used to be gated
    # here too, taking their era-agnostic unit tests with them; those two now
    # import under both majors and mark only their integration class
    # (`test_utils.LEGACY_ONLY`). The privacy guard this module encodes has no
    # 2.x equivalent yet.
    "test_diagnostics_no_payload.py",
)
_MODERN_ONLY = (
    "e2e/official_modern",
    "e2e/community_v4",
    "test_lowlevel_v2_handles.py",
    # FastMCP 4 only: the era ships a second dispatch pass, a default
    # dereferencing middleware and real multi-round-trip results, none of which
    # exist on the 3.x line this suite's community/ directory covers.
    "test_community_v4_handles.py",
)


def _ignore_globs(paths: tuple[str, ...]) -> list[str]:
    """Ignore each path itself plus everything beneath it.

    Patterns are fnmatched against absolute paths, so a bare trailing `*` on a
    directory name would also swallow siblings that merely share the prefix
    (`e2e/official` vs `e2e/official_modern`). Anchoring the recursive form on
    `/` keeps each entry to exactly its own subtree.
    """
    return [pattern for path in paths for pattern in (path, f"{path}/*")]


collect_ignore_glob = _ignore_globs(_MODERN_ONLY if MCP_MAJOR < 2 else _LEGACY_ONLY)
