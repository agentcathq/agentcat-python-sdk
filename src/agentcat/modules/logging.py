"""Logging functionality for AgentCat."""

import os
from collections.abc import Callable
from datetime import datetime, timezone

from agentcat.types import AgentCatOptions


# Initialize debug_mode from environment variable at module load time
_env_debug = os.getenv("AGENTCAT_DEBUG_MODE")
if _env_debug is not None:
    debug_mode = _env_debug.lower() in ("true", "1", "yes", "on")
else:
    debug_mode = False


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


def write_to_log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    log_entry = f"[{timestamp}] {message}"

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
