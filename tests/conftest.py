import os
from importlib.metadata import PackageNotFoundError, version

# Belt-and-suspenders: force diagnostics off before any test imports/runs, so no
# test ordering or future change to the auto-disable detection can ever ship OTLP
# diagnostics to the live collector from our own suite. Diagnostics-specific tests
# opt back in explicitly with DISABLE_DIAGNOSTICS=false plus mocked HTTP.
os.environ["DISABLE_DIAGNOSTICS"] = "true"


# ── Collection gating ────────────────────────────────────────────────────────
# Three independent reasons a module cannot even be IMPORTED in a given
# environment. They are accumulated below rather than chosen between: the
# compatibility matrix runs legs that trip more than one at once (mcp 2.x with
# fastmcp deliberately uninstalled trips two).
#
# This has to happen at collection, not as a `skipif`. Every case here is a
# module-scope import of a symbol that does not exist, which raises while
# pytest is importing the module — before any mark on any test inside it runs.
#
# Prerelease segments are dropped rather than parsed: `2.0.0b1` yields (2, 0),
# which orders correctly against every bound used here.
MCP_VERSION = tuple(int(p) for p in version("mcp").split(".")[:3] if p.isdigit())
MCP_MAJOR = MCP_VERSION[0]

try:
    version("fastmcp")
    HAS_FASTMCP = True
except PackageNotFoundError:  # the `test-without-fastmcp` matrix legs
    HAS_FASTMCP = False

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
    "test_community_v4_nested_calls.py",
)

# Needs community FastMCP importable at all, independent of era. The
# `test-without-fastmcp` matrix legs uninstall it and then verify it is gone,
# and these trees reach for `fastmcp` while pytest imports them.
_NEEDS_FASTMCP = (
    "e2e/community_v3",
    "e2e/community_v4",
    "community",
)

# Needs a Streamable-HTTP transport AND an HTTP request object to inspect.
# `mcp.client.streamable_http`, `FastMCP.streamable_http_app()` and the
# `stateless_http` setting all arrive in mcp 1.8.0; `RequestContext.request` —
# everything `extra.requestInfo` reads — arrives in 1.9.2. Below that there is
# no transport to exercise and no request to read, so unlike the rest of the
# old-version work there is nothing to reach into: the capability is absent
# upstream, not merely spelled differently.
_NEEDS_REQUEST_CONTEXT = ("e2e/official",)


def _ignore_globs(paths: tuple[str, ...]) -> list[str]:
    """Ignore each path itself plus everything beneath it.

    Patterns are fnmatched against absolute paths, so a bare trailing `*` on a
    directory name would also swallow siblings that merely share the prefix
    (`e2e/official` vs `e2e/official_modern`). Anchoring the recursive form on
    `/` keeps each entry to exactly its own subtree.
    """
    return [pattern for path in paths for pattern in (path, f"{path}/*")]


collect_ignore_glob = _ignore_globs(_MODERN_ONLY if MCP_MAJOR < 2 else _LEGACY_ONLY)
if not HAS_FASTMCP:
    collect_ignore_glob += _ignore_globs(_NEEDS_FASTMCP)
if MCP_VERSION < (1, 9, 2):
    collect_ignore_glob += _ignore_globs(_NEEDS_REQUEST_CONTEXT)
