"""The customer's process is theirs: signals, exit, threads, imports.

Regression suite for the v2.0.0 never-halt audit. Every test here pins a
process-level guarantee the SDK broke at least once:

- importing the SDK must not install signal handlers, call os._exit, or
  register event-draining atexit hooks (audit finding 1);
- `import agentcat` must survive an install with no distribution metadata
  (finding 2);
- importing and publishing from a NON-MAIN thread must work and must not leak
  threads (finding 3);
- interpreter exit must be prompt even with a wedged customer hook in flight
  (findings 5/6).

Everything runs in subprocesses: these are properties of a whole process
lifecycle, and asserting them in-suite would let pytest's own state mask a
regression.
"""

import subprocess
import sys
import tempfile
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX signal semantics"
)


def _run(code: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            "PATH": "/usr/bin:/bin",
            "DISABLE_DIAGNOSTICS": "true",
            # Isolated so the subprocess's ~/agentcat.log never touches the
            # developer's real one.
            "HOME": tempfile.mkdtemp(prefix="agentcat-test-home-"),
        },
    )


def test_import_leaves_signal_handlers_alone():
    """Finding 1: the SDK must never replace SIGINT/SIGTERM handlers."""
    result = _run(
        """
import signal
import agentcat
import agentcat.modules.event_queue

assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, (
    "SIGINT handler replaced: " + repr(signal.getsignal(signal.SIGINT))
)
assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, (
    "SIGTERM handler replaced: " + repr(signal.getsignal(signal.SIGTERM))
)
print("CLEAN")
"""
    )
    assert result.returncode == 0, result.stderr
    assert "CLEAN" in result.stdout


def test_customer_signal_handler_and_cleanup_still_run():
    """Finding 1: on SIGTERM the CUSTOMER's handler, finally blocks, and
    atexit hooks all run, and the exit code is the customer's — no os._exit."""
    result = _run(
        """
import atexit
import os
import signal
import sys
import time

import agentcat
import agentcat.modules.event_queue  # the module that used to hijack signals

ran = []
atexit.register(lambda: print("ATEXIT-RAN", flush=True))

def customer_handler(signum, frame):
    ran.append(signum)
    print("CUSTOMER-HANDLER-RAN", flush=True)
    sys.exit(7)  # the CUSTOMER chooses the exit path and code

signal.signal(signal.SIGTERM, customer_handler)

try:
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(5)  # never reached; the handler exits
finally:
    print("FINALLY-RAN", flush=True)
"""
    )
    assert result.returncode == 7, (result.returncode, result.stderr)
    assert "CUSTOMER-HANDLER-RAN" in result.stdout
    assert "FINALLY-RAN" in result.stdout
    assert "ATEXIT-RAN" in result.stdout


def test_exit_is_prompt_with_a_wedged_redaction_hook():
    """Findings 5/6: a blocking customer hook on a worker thread must not
    hold interpreter exit — workers are daemon and nothing drains at exit."""
    start = time.monotonic()
    result = _run(
        """
import time
from datetime import datetime, timezone

from agentcat.modules.event_queue import event_queue
from agentcat.types import UnredactedEvent

event_queue.add(
    UnredactedEvent(
        id="wedge",
        event_type="mcp:tools/call",
        project_id="proj_test",
        session_id="ses_x",
        timestamp=datetime.now(timezone.utc),
        redaction_fn=lambda s: time.sleep(60) or s,
    )
)
time.sleep(0.3)  # let a worker pick it up and enter the hook
print("EXITING", flush=True)
""",
        timeout=15.0,
    )
    elapsed = time.monotonic() - start
    assert result.returncode == 0, result.stderr
    assert "EXITING" in result.stdout
    assert elapsed < 5.0, f"exit took {elapsed:.1f}s with a wedged hook"


def test_import_and_publish_from_non_main_thread():
    """Finding 3: importing and using the queue off the main thread works,
    starts exactly `concurrency` workers once, and leaks nothing per call."""
    result = _run(
        """
import sys
import threading
from datetime import datetime, timezone

def use_from_worker():
    from agentcat.modules.event_queue import event_queue
    from agentcat.types import UnredactedEvent

    def make():
        return UnredactedEvent(
            id="bg",
            event_type="mcp:tools/call",
            project_id=None,  # no project: nothing leaves the process
            session_id="ses_x",
            timestamp=datetime.now(timezone.utc),
        )

    event_queue.add(make())
    event_queue.add(make())

t = threading.Thread(target=use_from_worker)
t.start()
t.join()

assert "agentcat.modules.event_queue" in sys.modules, "module evicted on import"
from agentcat.modules.event_queue import event_queue
assert len(event_queue._workers) == event_queue.concurrency, (
    f"expected {event_queue.concurrency} workers, found {len(event_queue._workers)}"
)
assert all(w.daemon for w in event_queue._workers)

# A second thread's adds must not grow the pool.
t2 = threading.Thread(target=use_from_worker)
t2.start()
t2.join()
assert len(event_queue._workers) == event_queue.concurrency
print("NO-LEAK")
"""
    )
    assert result.returncode == 0, result.stderr
    assert "NO-LEAK" in result.stdout


def test_worker_stop_hook_registers_on_first_publish_not_at_import():
    """Importing the queue module must not register exit hooks; the bounded
    worker-stop hook appears only once a publish has started the worker."""
    result = _run(
        """
import atexit
from datetime import datetime, timezone

# Diagnostics registers its own hook at its import; measure after it.
import agentcat.modules.event_queue  # noqa: F401

baseline = atexit._ncallbacks()

from agentcat.modules.event_queue import event_queue
from agentcat.types import UnredactedEvent

assert atexit._ncallbacks() == baseline, "import registered an exit hook"

event_queue.add(
    UnredactedEvent(
        id="first",
        event_type="mcp:tools/call",
        project_id=None,
        session_id="ses_x",
        timestamp=datetime.now(timezone.utc),
    )
)
assert atexit._ncallbacks() == baseline + 1, "first publish must register the stop hook"

event_queue.add(
    UnredactedEvent(
        id="second",
        event_type="mcp:tools/call",
        project_id=None,
        session_id="ses_x",
        timestamp=datetime.now(timezone.utc),
    )
)
assert atexit._ncallbacks() == baseline + 1, "hook must register exactly once"
print("LAZY-HOOK")
"""
    )
    assert result.returncode == 0, result.stderr
    assert "LAZY-HOOK" in result.stdout


def test_import_survives_missing_distribution_metadata():
    """Finding 2: PackageNotFoundError at import time degrades the version
    string instead of crashing the customer's server at startup."""
    result = _run(
        """
import importlib.metadata
from importlib.metadata import PackageNotFoundError

_real = importlib.metadata.version

def fake(name, _real=_real):
    if name == "agentcat":
        raise PackageNotFoundError(name)
    return _real(name)

importlib.metadata.version = fake

import agentcat

assert agentcat.__version__ == "0.0.0", agentcat.__version__
print("SURVIVED", agentcat.__version__)
"""
    )
    assert result.returncode == 0, result.stderr
    assert "SURVIVED 0.0.0" in result.stdout


def test_version_matches_metadata_when_present():
    from importlib.metadata import version

    import agentcat

    assert agentcat.__version__ == version("agentcat")
