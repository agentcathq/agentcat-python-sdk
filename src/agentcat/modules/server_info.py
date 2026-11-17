"""The server info heartbeat.

About once an hour AgentCat is told what an agent sees on a tracked server:
its name, version, title and instructions, and its full tool, resource,
resource template and prompt lists.

The work has three parts, and each one runs in a different place:

* `check` runs on the request path. It reads and writes times in memory and
  does nothing else: no I/O, no log line, no customer code.
* The build runs as a task on the server's event loop. A timer of that loop
  starts it `START_DELAY_SECONDS` after the check, so no list handler runs
  while the result of the request is still being written. The list handlers
  are async and belong to that loop, so nothing else can call them.
* Redaction and the POST run on the event queue's worker thread, because both
  can block.

Nothing here raises into the server, writes to standard output or standard
error, or touches the disk. The only state is `_memory`, which holds times.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agentcat.modules.constants import GET_MORE_TOOLS_NAME
from agentcat.modules.logging import write_to_log
from agentcat.modules.redaction import redact_server_info
from agentcat.types import AgentCatData

# The kinds of request that run a check.
LIST_REQUEST = "tools/list"
FEEDBACK_NOTE = "get_more_tools"
TOOL_CALL = "tools/call"

# One attempt per (project, server name) in this many seconds.
ATTEMPT_INTERVAL_SECONDS = 3600.0
# A memory younger than this may be one that does not outlive its request, so
# an ordinary tool call does not start a heartbeat in it.
SETTLED_MEMORY_SECONDS = 60.0
MAX_GUARD_KEYS = 1000

# How long after the check the build starts. The handler of the request has
# returned by then, but its result is still on its way out: the MCP library
# and an HTTP transport need several turns of the loop to write it.
START_DELAY_SECONDS = 0.5

MAX_PAGES = 100
LIST_TIMEOUT_SECONDS = 5.0
BUILD_TIMEOUT_SECONDS = 30.0
SEND_TIMEOUT_SECONDS = 10

_PAYLOAD_TOO_LARGE = 413
_ACCEPTED = 202

# One page of a list, and the cursor of the next page (None on the last one).
Page = tuple[list[Any], Any]
PageFetcher = Callable[[Any], Awaitable[Page]]


@dataclass(frozen=True)
class ServerInfoSource:
    """What an adapter knows how to read from one server.

    `lists` maps a key of `features` to the fetcher of that list. A list the
    server has no handler for has no entry.
    """

    title: str
    instructions: str
    lists: dict[str, PageFetcher]


class _ListFailed(Exception):
    """A list that cannot be read to its end."""


class _Memory:
    """When the first check ran, and when each key was last attempted."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.first_check_at: float | None = None
        self.attempts: dict[tuple[str, str], float] = {}

    def clear(self) -> None:
        with self.lock:
            self.first_check_at = None
            self.attempts.clear()


_memory = _Memory()
_now: Callable[[], float] = time.monotonic

# Running builds. asyncio keeps only a weak reference to a task, so a build
# that nothing else holds could be collected before it ends.
_tasks: set[asyncio.Task[None]] = set()

# Builds that wait for their start. The loop holds each timer; this set only
# lets a reader see them, and a timer that its loop dropped leaves it.
_starts: weakref.WeakSet[asyncio.TimerHandle] = weakref.WeakSet()

# Set inside a build. A list handler can pass through the SDK's own tools/list
# hook again, and that pass must not start a second build.
_building: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "agentcat_server_info_building", default=False
)


def identity_text(server: Any, attribute: str) -> str:
    """A text attribute of the server, or an empty string when it has none."""
    try:
        value = getattr(server, attribute, None)
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def check(
    data: AgentCatData, kind: str, source: Callable[[], ServerInfoSource]
) -> None:
    """Schedule a heartbeat when one is due. Safe to call on the request path.

    Nothing of the heartbeat runs here: a timer of the running loop starts the
    build later, and the build is what calls `source`. The timer carries a copy
    of the current context, so the list handlers see the request that started
    the check. A loop that closes first drops the timer, and no heartbeat is
    sent. Without a running asyncio loop there is nowhere to run the build, so
    none is sent either.
    """
    try:
        if _building.get() or not _due(data, kind):
            return
        loop = asyncio.get_running_loop()
        _starts.add(
            loop.call_later(
                START_DELAY_SECONDS,
                _start,
                loop,
                data,
                source,
                context=contextvars.copy_context(),
            )
        )
    except Exception:
        return


def _start(
    loop: asyncio.AbstractEventLoop,
    data: AgentCatData,
    source: Callable[[], ServerInfoSource],
) -> None:
    """The timer callback: put the build on the loop. Never raises."""
    try:
        build = _run(data, source)
        try:
            task = loop.create_task(build, name="agentcat-server-info")
        except BaseException:
            build.close()
            raise
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception:
        return


def _due(data: AgentCatData, kind: str) -> bool:
    """Whether to start a heartbeat now. Records the attempt when it says yes."""
    now = _now()
    with _memory.lock:
        if _memory.first_check_at is None:
            _memory.first_check_at = now
        if kind == TOOL_CALL and now - _memory.first_check_at < SETTLED_MEMORY_SECONDS:
            return False
        if not data.project_id or not data.options.enable_tracing:
            return False
        key = (data.project_id, _guard_name(data.server_name))
        last = _memory.attempts.get(key)
        if last is not None and now - last < ATTEMPT_INTERVAL_SECONDS:
            return False
        if key not in _memory.attempts and len(_memory.attempts) >= MAX_GUARD_KEYS:
            _memory.attempts.clear()
        _memory.attempts[key] = now
        return True


def _guard_name(server_name: Any) -> str:
    return server_name if isinstance(server_name, str) else ""


async def _run(data: AgentCatData, source: Callable[[], ServerInfoSource]) -> None:
    """The build task. Nothing but a cancellation leaves it."""
    _building.set(True)
    deadline = time.monotonic() + BUILD_TIMEOUT_SECONDS
    try:
        body = await asyncio.wait_for(_build(data, source), BUILD_TIMEOUT_SECONDS)
        if body is None:
            return
        redact = data.options.redact_sensitive_information

        def deliver() -> None:
            _deliver(body, redact, deadline)

        from agentcat.modules import event_queue as queue_module

        if not queue_module.event_queue.add_job(deliver):
            write_to_log(
                "Server info heartbeat skipped: the event queue did not take it"
            )
    except asyncio.TimeoutError:
        write_to_log(
            "Server info heartbeat skipped: the lists took more than "
            f"{BUILD_TIMEOUT_SECONDS:g} seconds"
        )
    except (Exception, SystemExit) as error:
        write_to_log(f"Server info heartbeat failed: {type(error).__name__}")


async def _build(
    data: AgentCatData, source: Callable[[], ServerInfoSource]
) -> dict[str, Any] | None:
    """The request body, or None when the tool list cannot be read."""
    info = source()
    features: dict[str, list[Any]] = {}
    for key, fetch in info.lists.items():
        try:
            features[key] = await _collect(fetch)
        except (Exception, SystemExit) as error:
            write_to_log(
                f"Server info heartbeat: the {key} list failed "
                f"({_reason(error)}), so it is left out"
            )
    if "tools" not in features:
        write_to_log("Server info heartbeat skipped: no tool list")
        return None
    features["tools"] = _one_get_more_tools(features["tools"])
    return {
        "project_id": data.project_id,
        "server_name": data.server_name,
        "server_version": data.server_version,
        "server_title": info.title,
        "instructions": info.instructions,
        "features": features,
    }


def _reason(error: BaseException) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return f"more than {LIST_TIMEOUT_SECONDS:g} seconds"
    if isinstance(error, _ListFailed):
        return str(error)
    return type(error).__name__


async def _collect(fetch: PageFetcher) -> list[Any]:
    """Every item of one list, read page by page."""
    deadline = time.monotonic() + LIST_TIMEOUT_SECONDS
    items: list[Any] = []
    seen: set[str] = set()
    cursor: Any = None
    for _ in range(MAX_PAGES):
        # One turn of the loop for everything else before each page, so a
        # handler that never suspends cannot run the lists back to back.
        await asyncio.sleep(0)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        page, cursor = await asyncio.wait_for(fetch(cursor), remaining)
        items.extend(_wire_item(item) for item in page)
        if cursor is None or cursor == "":
            return items
        if not isinstance(cursor, str):
            raise _ListFailed("a cursor is not a string")
        if cursor in seen:
            raise _ListFailed("a cursor repeats")
        seen.add(cursor)
    raise _ListFailed(f"more than {MAX_PAGES} pages")


def _wire_item(item: Any) -> Any:
    """The JSON object the MCP library puts on the wire for a list item."""
    if isinstance(item, dict):
        return item
    return item.model_dump(by_alias=True, mode="json", exclude_none=True)


def _one_get_more_tools(tools: list[Any]) -> list[Any]:
    """`tools` with `get_more_tools` kept one time.

    A server that pages its tool list gets the SDK's tool on every page.
    """
    last = max(
        (index for index, tool in enumerate(tools) if _is_get_more_tools(tool)),
        default=None,
    )
    return [
        tool
        for index, tool in enumerate(tools)
        if index == last or not _is_get_more_tools(tool)
    ]


def _is_get_more_tools(tool: Any) -> bool:
    return isinstance(tool, dict) and tool.get("name") == GET_MORE_TOOLS_NAME


def without_schemas(body: dict[str, Any]) -> dict[str, Any]:
    """`body` with no input or output schema on any tool."""
    features = dict(body["features"])
    features["tools"] = [
        {
            key: value
            for key, value in tool.items()
            if key not in ("inputSchema", "outputSchema")
        }
        if isinstance(tool, dict)
        else tool
        for tool in features["tools"]
    ]
    return {**body, "features": features}


def _deliver(
    body: dict[str, Any], redact: Callable[[str], Any] | None, deadline: float
) -> None:
    """Redact and send one heartbeat. Runs on the worker thread; never raises."""
    try:
        if redact is not None:
            body = redact_server_info(body, _within(redact, deadline))
        if time.monotonic() > deadline:
            write_to_log(
                "Server info heartbeat skipped: it was not ready after "
                f"{BUILD_TIMEOUT_SECONDS:g} seconds"
            )
            return
        status = _send(body)
        if status == _PAYLOAD_TOO_LARGE:
            body = without_schemas(body)
            status = _send(body)
        if status == _ACCEPTED:
            write_to_log(
                f"Server info heartbeat sent | {len(body['features']['tools'])} "
                f"tools | {len(json.dumps(body).encode())} bytes"
            )
        else:
            write_to_log(f"Server info heartbeat failed: the API answered {status}")
    except BaseException as error:
        write_to_log(f"Server info heartbeat failed: {type(error).__name__}")


def _within(redact: Callable[[str], Any], deadline: float) -> Callable[[str], Any]:
    """`redact`, refusing to start once the build has run out of time."""

    def timed(value: str) -> Any:
        if time.monotonic() > deadline:
            raise TimeoutError("redaction passed the time limit of the build")
        return redact(value)

    return timed


def _post(body: dict[str, Any]) -> int:
    """One POST through the event queue's API client. Returns the status."""
    from agentcat.modules import event_queue as queue_module

    return queue_module.event_queue.post_server_info(body, SEND_TIMEOUT_SECONDS)


# What `_deliver` calls to send. The test setup replaces it with a recorder.
_send: Callable[[dict[str, Any]], int] = _post
