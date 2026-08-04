"""Logging functionality for AgentCat."""

import functools
import os
import platform
from collections.abc import Callable
from datetime import datetime, timezone


def _env_debug_mode() -> bool:
    """True when AGENTCAT_DEBUG_MODE holds a truthy value."""
    raw = os.getenv("AGENTCAT_DEBUG_MODE")
    return raw is not None and raw.lower() in ("true", "1", "yes", "on")


# Seed from the environment at import time. track() only overrides this when
# AgentCatOptions.debug_mode is explicitly set — None leaves the seed alone.
debug_mode = _env_debug_mode()


# Optional sink that receives every (clean, newline-free) log entry. Used by the
# diagnostics module to mirror internal logs to AgentCat's monitoring. Fires
# independent of debug_mode and must never break logging.
_diagnostics_sink: Callable[[str], None] | None = None


def set_debug_mode(value: bool) -> None:
    """Set the global debug_mode value."""
    global debug_mode
    debug_mode = value


def set_diagnostics_sink(fn: Callable[[str], None] | None) -> None:
    """Register (or clear with None) the diagnostics sink for log entries."""
    global _diagnostics_sink
    _diagnostics_sink = fn


@functools.lru_cache(maxsize=1)
def _version_suffix() -> str:
    """Environment stamp appended to every log entry, computed once per process.

    A shared log excerpt must never leave the reader guessing which AgentCat
    SDK, Python, or MCP SDK produced it; `absent` for an uninstalled MCP
    distribution is itself a diagnostic.
    """
    from agentcat.utils import get_agentcat_version, get_dist_version

    return (
        f"agentcat={get_agentcat_version() or 'unknown'} "
        f"python={platform.python_version()} "
        f"mcp={get_dist_version('mcp') or 'absent'} "
        f"fastmcp={get_dist_version('fastmcp') or 'absent'}"
    )


def write_to_log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    log_entry = f"[{timestamp}] {message} | {_version_suffix()}"

    # Tee to diagnostics FIRST — independent of debug_mode. Must never break logging.
    if _diagnostics_sink is not None:
        try:
            _diagnostics_sink(log_entry)
        except Exception:
            pass

    # Always use ~/agentcat.log
    log_path = os.path.expanduser("~/agentcat.log")

    try:
        if debug_mode:
            # Write to log file (no need to ensure directory exists for home directory)
            with open(log_path, "a") as f:
                f.write(log_entry + "\n")
    except Exception:
        # Silently fail - we don't want logging errors to break the server
        pass
