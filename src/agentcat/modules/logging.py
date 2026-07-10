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


def safe_error_string(error: object) -> str:
    """Best-effort str(error) that never raises.

    Exceptions surfaced from customer code (identify hooks, tools, callbacks)
    may have a broken __str__/__format__. Log formatting on the request path
    must never raise, or the log line itself would break the customer's
    request handling.
    """
    try:
        return str(error)
    except Exception:
        try:
            return f"<unprintable {type(error).__name__} instance>"
        except Exception:
            return "<unprintable error>"


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
