"""What "may be sync or async" means, defined once for every customer hook.

`AgentCatOptions` takes five customer-supplied callables — `identify`,
`event_tags`, `event_properties`, `resolve_session_id` and
`redact_sensitive_information` — and all five are documented to accept a sync
or an async function. Before this module each site answered that question for
itself, in four different spellings across four files, and `identify` did not
answer it at all: it called the hook and used the return value verbatim, so an
`async def identify` built a coroutine, ran none of its body, failed the
`isinstance(result, UserIdentity)` check and published the call anonymously.
The customer saw no error — only an event with no actor.

Two entry points, because the SDK runs hooks from two places:

* `await_hook_result` — the request path. Every adapter reaches the hooks
  through `await resolve_call(...)` and `await publish_tool_call_event(...)`,
  on every mcp and fastmcp version in the compatibility matrix, so there is a
  running loop and awaiting is all it takes.
* `drive_hook_result` — the publish worker (`event_queue.EventQueue._worker`),
  a daemon THREAD with no event loop of its own. Redaction runs there and
  cannot await, so an awaitable has to be driven to completion with a loop of
  its own.

Both narrow on `inspect.isawaitable`, never `inspect.iscoroutine`. The
difference is not academic: `iscoroutine` matches only native coroutines, so a
hook returning an `asyncio.Task` or `Future` — a cached in-flight lookup, say —
or any object implementing `__await__` would be assigned into the event
verbatim. That is the `<coroutine object ...>`-shaped failure again, and it
looks like the hook worked.

Both also narrow on the RESULT rather than the callable. `iscoroutinefunction`
would be wrong here: it answers False for a `functools.partial` of an async
function, for a bound method of one on some versions, and for any decorator
that returns a non-async wrapper around async work. Calling the hook and asking
what came back needs none of those special cases.
"""

import asyncio
import inspect
from collections.abc import Awaitable
from typing import Any, TypeVar, cast

from agentcat.modules.logging import write_to_log

T = TypeVar("T")

# Both signatures below spell `T | Awaitable[Any]` inline rather than sharing an
# alias. A generic alias needs `Union[T, ...]` — mypy rejects a TypeVar as the
# target of a PEP-604 alias — and ruff's UP007 then rewrites that `Union` back
# into the form mypy rejected. Inline, both tools agree.
#
# `Any` rather than `T` on the awaitable half: a customer's hook is untyped at
# runtime and its option alias is itself a union, so the concrete awaitable is
# whatever they returned.
#
# Only the awaited branch casts. `inspect.isawaitable` is a TypeGuard, so its
# NEGATIVE arm already narrows the union to `T` on its own — a cast there is
# redundant, and mypy says so. Awaiting an `Awaitable[Any]` yields `Any`, which
# `warn_return_any` rejects, so that arm does need one.


def _report(hook_name: str, error: Exception) -> None:
    """Name the await as the cause, since the caller's log cannot.

    Every call site already logs "this hook failed, degrading" from its own
    `except`. That line is true but unhelpful when the hook itself was fine and
    only the awaiting went wrong — a customer reading it goes looking for a bug
    in code that ran correctly. This one says which half broke; the caller's
    still says which hook degraded, and both are wanted.
    """
    write_to_log(
        f"Warning: {hook_name} returned an awaitable that could not be awaited: {error}"
    )


async def await_hook_result(value: T | Awaitable[Any], hook_name: str) -> T:
    """Resolve a hook's return value on a path that has a running loop.

    Re-raises rather than swallowing: each call site owns its own degradation
    rule, and they differ. A failed `identify` yields an anonymous actor, a
    failed `resolve_session_id` mints a fresh handle, a failed redaction drops
    the event entirely. Deciding that here would flatten three deliberate
    behaviors into one.
    """
    if not inspect.isawaitable(value):
        return value
    try:
        return cast(T, await value)
    except Exception as e:
        _report(hook_name, e)
        raise


def drive_hook_result(value: T | Awaitable[Any], hook_name: str) -> T:
    """Resolve a hook's return value from a thread with no event loop.

    `asyncio.run` builds a loop, runs the awaitable to completion and tears the
    loop down. That is only valid because the publish worker is a plain thread
    that never had one — calling this from inside a running loop raises, which
    is why the request path uses `await_hook_result` instead.

    Not cheap, and knowingly so: the one caller is redaction, a security
    control, running off the request's hot path. A hook that cannot be driven
    raises here and the queue drops the event rather than publishing it
    unredacted.
    """
    if not inspect.isawaitable(value):
        return value
    try:
        return cast(T, asyncio.run(_resolved(value)))
    except Exception as e:
        _report(hook_name, e)
        raise


async def _resolved(awaitable: Any) -> Any:
    """`asyncio.run` takes a coroutine, and an arbitrary awaitable is not one."""
    return await awaitable
