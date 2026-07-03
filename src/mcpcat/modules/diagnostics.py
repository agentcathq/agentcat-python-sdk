"""Privacy-first internal SDK diagnostics.

Mirrors every internal MCPCat log line to MCPCat's own monitoring as an
OTLP/HTTP log record, so we can detect when a developer's SDK fails to set up.

**Only operational metadata is ever sent — never event payloads or user data.**
Records carry environment/identity metadata (the attempted ``project_id``, or an
anonymous install-id hash when none) plus a metadata-only message body. The local
``~/mcpcat.log`` is unaffected.

On by default; opt out via ``MCPCatOptions(disable_diagnostics=True)`` or the
``DISABLE_DIAGNOSTICS`` env var. Auto-disabled in test environments
(``PYTEST_CURRENT_TEST`` / ``PYTEST_VERSION`` set) unless explicitly force-enabled
with ``DISABLE_DIAGNOSTICS=false``. Nothing here ever throws into the host, and the
fire-and-forget HTTP export runs on a daemon thread so it never blocks process exit.

Override the collector with ``DIAGNOSTICS_ENDPOINT`` / ``DIAGNOSTICS_TOKEN``.
"""

import atexit
import hashlib
import os
import platform
import re
import socket
import threading
import time
from importlib.metadata import version
from typing import Any

import requests

from .constants import (
    DEFAULT_DIAGNOSTICS_ENDPOINT,
    DEFAULT_DIAGNOSTICS_TOKEN,
    DIAGNOSTICS_SCOPE_NAME,
)
from .logging import set_diagnostics_sink

# --- Module-level state (guarded by _lock for buffer/timer) -------------------

_enabled = False
_initialized = False
_static_attributes: list[dict[str, Any]] = []
_buffer: list[dict[str, Any]] = []
_flush_timer: threading.Timer | None = None
_lock = threading.Lock()

MAX_BUFFER = 1000
BATCH_FLUSH_MS = 2000


def _sdk_version() -> str:
    try:
        return version("mcpcat")
    except Exception:
        return "unknown"


# --- Resolution helpers -------------------------------------------------------


def _resolve_endpoint() -> str:
    base = DEFAULT_DIAGNOSTICS_ENDPOINT
    try:
        base = os.environ.get("DIAGNOSTICS_ENDPOINT") or base
    except Exception:
        pass
    trimmed = base.rstrip("/")
    return trimmed if trimmed.endswith("/v1/logs") else f"{trimmed}/v1/logs"


def _resolve_token() -> str:
    try:
        return os.environ.get("DIAGNOSTICS_TOKEN") or DEFAULT_DIAGNOSTICS_TOKEN
    except Exception:
        return DEFAULT_DIAGNOSTICS_TOKEN


def _is_test_environment() -> bool:
    """True when running under pytest (``PYTEST_CURRENT_TEST`` / ``PYTEST_VERSION``).

    Diagnostics auto-disable here so no test suite — ours or a consumer's — ever
    ships OTLP metadata to the live collector. Never throws.
    """
    try:
        return bool(
            os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST_VERSION")
        )
    except Exception:
        return False


def _env_diagnostics_flag() -> str:
    """Interpret ``DISABLE_DIAGNOSTICS`` by value, not mere presence.

    Returns one of:
    - ``"unset"``: env var unset, empty, or whitespace-only (default behavior).
    - ``"force-enabled"``: ``false`` / ``0`` / ``no`` / ``off`` — a deliberate
      opt-in that overrides the test-environment auto-disable.
    - ``"disabled"``: anything else disables diagnostics.

    Mirrors the ``AGENTCAT_DEBUG_MODE`` idiom in logging.py.
    """
    try:
        raw = os.environ.get("DISABLE_DIAGNOSTICS")
        if not raw or not raw.strip():
            return "unset"
        if raw.strip().lower() in ("false", "0", "no", "off"):
            return "force-enabled"
        return "disabled"
    except Exception:
        return "unset"


# --- Record construction ------------------------------------------------------


def _infer_severity(entry: str) -> tuple[int, str]:
    # Order matters: fail/error first so "Warning: Failed to..." is ERROR (a
    # setup failure), not WARN. "Warning:" is a case-sensitive literal match.
    if re.search(r"fail|error", entry, re.I):
        return 17, "ERROR"
    if "Warning:" in entry:
        return 13, "WARN"
    return 9, "INFO"


def _build_record(entry: str) -> dict[str, Any]:
    number, text = _infer_severity(entry)
    return {
        "timeUnixNano": str(time.time_ns()),
        "severityNumber": number,
        "severityText": text,
        "body": {"stringValue": entry},
        "attributes": [],
    }


def _attr(key: str, value: Any) -> list[dict[str, Any]]:
    return [{"key": key, "value": {"stringValue": str(value)}}] if value else []


def _compute_install_id() -> str | None:
    try:
        seed = f"{socket.gethostname()}|{__file__}"
        return hashlib.sha256(seed.encode()).hexdigest()[:16]
    except Exception:
        return None


def _build_static_attributes(project_id: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        # Identity / traceability
        if project_id:
            out += _attr("mcpcat.project_id", project_id)
        else:
            out += _attr("mcpcat.install_id", _compute_install_id())

        # SDK
        out += _attr("mcpcat.sdk.language", "python")
        out += _attr("mcpcat.sdk.version", _sdk_version())

        # Best-effort: resolved MCP SDK version (distribution name may differ).
        try:
            out += _attr("mcpcat.mcp_sdk.version", version("mcp"))
        except Exception:
            pass

        # Runtime
        out += _attr("process.runtime.name", platform.python_implementation().lower())
        out += _attr("process.runtime.version", platform.python_version())
        out += _attr("process.pid", str(os.getpid()))

        # OS / host
        out += _attr("os.type", platform.system())
        out += _attr("os.version", platform.release())
        out += _attr("host.arch", platform.machine())
        cpu_count = os.cpu_count()
        out += _attr("host.cpu.count", str(cpu_count) if cpu_count else None)

        # Deploy/CI hints
        out += _attr(
            "deployment.environment",
            os.environ.get("APP_ENV") or os.environ.get("ENVIRONMENT"),
        )
    except Exception:
        # best-effort; partial attributes are fine
        pass
    return out


# --- Buffering + flush --------------------------------------------------------


def _schedule_flush() -> None:
    global _flush_timer
    with _lock:
        if _flush_timer is not None:
            return
        try:
            timer = threading.Timer(BATCH_FLUSH_MS / 1000.0, _timer_fired)
            timer.daemon = True  # never keep the interpreter alive for diagnostics
            _flush_timer = timer
            timer.start()
        except Exception:
            _flush_timer = None


def _timer_fired() -> None:
    global _flush_timer
    with _lock:
        _flush_timer = None
    flush_diagnostics()


def capture(entry: str) -> None:
    """Buffer one log entry (bounded, drop-oldest) and schedule a flush."""
    try:
        if not _enabled:
            return
        with _lock:
            if len(_buffer) >= MAX_BUFFER:
                _buffer.pop(0)
            _buffer.append(_build_record(entry))
        _schedule_flush()
    except Exception:
        # diagnostics capture must never throw
        pass


def flush_diagnostics() -> None:
    """Swap out the buffer and POST it fire-and-forget. Never raises."""
    try:
        if not _enabled:
            return
        with _lock:
            if not _buffer:
                return
            records = _buffer[:]
            _buffer.clear()

        payload = {
            "resourceLogs": [
                {
                    "resource": {"attributes": _static_attributes},
                    "scopeLogs": [
                        {
                            "scope": {
                                "name": DIAGNOSTICS_SCOPE_NAME,
                                "version": _sdk_version(),
                            },
                            "logRecords": records,
                        }
                    ],
                }
            ]
        }

        token = _resolve_token()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        requests.post(_resolve_endpoint(), json=payload, headers=headers, timeout=5)
    except Exception:
        # fire-and-forget: never propagate diagnostics network errors
        pass


# --- Lifecycle ----------------------------------------------------------------


def init_diagnostics(project_id: str | None, disabled: bool = False) -> None:
    """Initialize diagnostics. Idempotent; never throws into the host."""
    global _enabled, _initialized, _static_attributes
    try:
        if _initialized:
            return
        _initialized = True
        # Off when opted out (option or env), and off in test environments unless
        # explicitly force-enabled (DISABLE_DIAGNOSTICS=false) — so no test run,
        # ours or a consumer's, ever ships diagnostics to the live collector.
        flag = _env_diagnostics_flag()
        _enabled = (
            (not disabled)
            and flag != "disabled"
            and (flag == "force-enabled" or not _is_test_environment())
        )
        if not _enabled:
            return
        _static_attributes = _build_static_attributes(project_id)
        set_diagnostics_sink(capture)
    except Exception:
        # diagnostics init must never throw
        pass


def is_diagnostics_enabled() -> bool:
    return _enabled


# --- Test helpers (mirror the TS SDK) -----------------------------------------


def _reset_diagnostics_for_test() -> None:
    global _enabled, _initialized, _static_attributes, _buffer, _flush_timer
    with _lock:
        _enabled = False
        _initialized = False
        _static_attributes = []
        _buffer = []
        if _flush_timer is not None:
            _flush_timer.cancel()
            _flush_timer = None
    set_diagnostics_sink(None)


def _get_static_attributes_for_test() -> list[dict[str, Any]]:
    return _static_attributes


def _build_record_for_test(entry: str) -> dict[str, Any]:
    return _build_record(entry)


# Flush whatever is buffered on interpreter exit (covers non-destroy() paths).
atexit.register(flush_diagnostics)
