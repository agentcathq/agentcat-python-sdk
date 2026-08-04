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

Three entry points, because the SDK runs hooks from three places:

* `run_hook` — the request path's containment boundary. The hook CALL runs on
  an anyio worker thread (a blocking sync hook stalls only its own request,
  never the server's event loop — the same offload both FastMCP generations
  give customers' sync tool bodies), awaitable results are awaited inline,
  and the whole execution sits under a hard timeout. Every failure mode —
  raise, wrong thread-behavior, timeout, even SystemExit or a spontaneous
  CancelledError — surfaces as `HookExecutionError`, a plain Exception, so
  each call site's own `except Exception` degradation rule applies unchanged.
  Genuine task cancellation (client disconnect) still propagates.
* `await_hook_result` — resolves a hook's RETURN VALUE on a path with a
  running loop. `run_hook` uses it internally; adapters no longer call it
  directly for customer hooks.
* `drive_hook_result` — the publish worker (`event_queue.EventQueue`),
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
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

import anyio
import anyio.to_thread

from agentcat.modules.logging import write_to_log

T = TypeVar("T")

# Hard cap on one hook execution. Generous for a lookup, small enough that a
# wedged hook costs one request its metadata instead of stalling the call.
HOOK_TIMEOUT_SECONDS = 5.0

# An async hook may legitimately return an awaitable of an awaitable (a hook
# returning an asyncio.Task of a cached lookup); unwrap a few hops, not forever.
_MAX_AWAIT_HOPS = 3


class HookExecutionError(Exception):
    """A customer hook failed, timed out, or raised something that must not
    ride the request path. Deliberately a plain Exception: every call site's
    existing `except Exception` degradation rule catches it unchanged."""

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


async def run_hook(
    hook: Callable[..., Any],
    hook_name: str,
    request: Any,
    extra: Any,
    timeout: float = HOOK_TIMEOUT_SECONDS,
) -> Any:
    """Execute a customer hook with full containment. The request-path entry.

    Threading: the CALL runs via `anyio.to_thread.run_sync`, so a sync hook
    that blocks (a DB lookup, an HTTP call) suspends only this request — the
    loop keeps serving every other call. `abandon_on_cancel=True` lets the
    timeout fire while the hook still blocks; the abandoned hook keeps running
    on its daemon worker thread with its result discarded, and can never hold
    the process open. An async hook's coroutine is awaited inline on the loop
    (same as before), under the same timeout.

    Exceptions-as-values across the thread boundary make cancellation
    unambiguous: a `CancelledError`/`SystemExit` the hook itself raises comes
    back as data and becomes `HookExecutionError`, so any `CancelledError`
    surfacing from the awaits here is, by construction, the enclosing task
    being cancelled — and is re-raised. (On the async-hook path, Python 3.11+
    can additionally distinguish a spontaneous CancelledError via
    `Task.cancelling()`; on 3.10 it is conservatively re-raised.)

    Behavior notes, both documented in MIGRATION.md: a sync hook that calls
    asyncio APIs must become `async def` (worker threads have no running
    loop), and a hook slower than `timeout` degrades that call exactly like a
    hook that raised.
    """

    def _call_contained() -> tuple[str, Any]:
        try:
            return ("ok", hook(request, extra))
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001 — the containment boundary
            return ("raised", e)

    try:
        with anyio.fail_after(timeout):
            status, payload = await anyio.to_thread.run_sync(
                _call_contained, abandon_on_cancel=True
            )
            if status == "raised":
                raise HookExecutionError(
                    f"{hook_name} hook raised {type(payload).__name__}: {payload}"
                ) from payload
            value = payload
            hops = 0
            while inspect.isawaitable(value) and hops < _MAX_AWAIT_HOPS:
                value = await await_hook_result(value, hook_name)
                hops += 1
            return value
    except HookExecutionError:
        raise
    except TimeoutError:
        write_to_log(
            f"Warning: {hook_name} hook timed out after {timeout}s; "
            "proceeding without its result"
        )
        raise HookExecutionError(f"{hook_name} hook timed out") from None
    except asyncio.CancelledError:
        task = asyncio.current_task()
        cancelling = getattr(task, "cancelling", None)
        if callable(cancelling) and cancelling() == 0:
            # 3.11+: no external cancel is pending, so the async hook raised
            # CancelledError spontaneously — contained, not propagated.
            raise HookExecutionError(
                f"{hook_name} hook raised CancelledError outside a cancellation"
            ) from None
        raise  # genuine task cancellation (or 3.10, which cannot probe)
    except (Exception, SystemExit) as e:
        raise HookExecutionError(
            f"{hook_name} hook raised {type(e).__name__}: {e}"
        ) from e


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
    except (SystemExit, asyncio.CancelledError) as e:
        # On a worker thread there is no enclosing task, so either of these
        # from the awaitable is definitionally the hook's own doing. Convert
        # to a plain Exception so the caller's drop-the-event rule applies
        # instead of the worker thread dying.
        write_to_log(
            f"Warning: {hook_name} returned an awaitable that raised "
            f"{type(e).__name__}; treating as hook failure"
        )
        raise HookExecutionError(
            f"{hook_name} hook raised {type(e).__name__}"
        ) from e


async def _resolved(awaitable: Any) -> Any:
    """`asyncio.run` takes a coroutine, and an arbitrary awaitable is not one."""
    return await awaitable
