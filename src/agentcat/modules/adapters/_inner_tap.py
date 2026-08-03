"""The inner tap: full Python exception detail on tool-error events.

v2 intercepts at the protocol boundary, and by the time an adapter sees a
failed ``tools/call`` most SDK generations have already caught the exception
and flattened it into an ``isError`` result. `mcp/server/lowlevel/server.py`
does it in the handler its ``@call_tool()`` decorator builds; `MCPServer`
does it in ``_handle_call_tool``. Either way the exception object dies inside
that ``except`` — nothing on the returned result retains its type, traceback
or ``__cause__``. Without a tap the only honest payload left is the three-key
fallback ``capture_call_tool_result_error`` produces.

This module is the tap. It is the Python answer to the TypeScript engine's
``src/engine/innerTap.ts``: something *below* the conversion records the live
exception, and the adapter's publish path picks it up for that same call.

**The contract** — the same on every era, however the tap is placed:

- `inner_tap()` opens a capture slot for exactly one tool call. Enter it
  before the customer's handler runs; the slot closes on every exit path,
  including the error path, because a context manager cannot skip ``__exit__``.
- `capture()` / `capture_in_flight()` record into whatever slot is open for
  the *calling* call, and are no-ops when none is.
- `InnerTap.error()` is the adapter's read: the tapped exception when there
  was one, the flattened result when there was not (a proxied upstream error
  and a tool that simply returned ``is_error`` have no local exception to
  find, and never will).

**Which exception, when a tool composes another tool.** A tool that calls a
tool re-enters a tapped seam, so one slot can see more than one failure — and
the event must describe the one the AGENT was told about, never a sub-call's
that the caller handled. Two rules settle it, and both are needed:

- **The last write wins.** A sub-call's failure is recorded, then the caller's
  own failure is recorded after it and replaces it. (The caller re-raising the
  same object, or a wrapper around it, is the same rule.) An earlier design
  kept the FIRST write and published a swallowed sub-call's exception against
  the caller's message.
- **A capture that never escaped is discarded.** A seam that returns *normally*
  drops any capture recorded inside its own dynamic extent: the exception was
  handled in there, so it did not produce this result. That is what covers the
  caller who suppresses a sub-call and then answers with an ``is_error`` result
  of its own rather than raising. A capture the seam re-raised is untouched,
  which is what keeps the community case — a middleware BELOW us converting a
  real tool failure into an ``is_error`` result — working.

  "Inside its own extent" is answered per SEAM, never per nesting level. Each
  entry into a seam takes a fresh marker and pushes it onto a per-task chain,
  and a capture remembers the chain it was recorded under; a normal return
  discards only a capture whose chain names that entry. A depth counter cannot
  answer the same question, because the counter has to live on the cell — and
  the cell is shared with every task that inherits it, so a sub-call the tool
  left running inflated the count and then erased a genuine capture on its own
  normal return. The chain is a ``ContextVar``, so a child task's pushes are
  invisible to its parent and to its siblings, which is exactly the isolation
  the question needs.

**Why a cell and not a "last error" variable.** The slot is an object created
inside the adapter's own frame; the `ContextVar` only carries a *reference* to
it downward, and the adapter reads its own local cell rather than the
variable. Two properties follow, and both matter:

1. *Cross-attribution is structurally impossible.* Two tool calls can only
   interleave if they are separate asyncio tasks, and a task runs in its own
   copy of the context — so `set()` in one call is invisible to the other, and
   each tap writes to the cell its own call installed. There is no shared slot
   to race for.
2. *A capture from a child task or worker thread still lands.* Contexts are
   copied downward, so a `ContextVar.set()` inside a child would be invisible
   to the parent that must read it — which is exactly what the retired
   ``store_captured_error`` did. Writing to the cell OBJECT is visible to
   whoever holds it, and `anyio.to_thread` (how both FastMCP generations run a
   sync tool body) copies the context into the worker thread.

   The same direction has a bounded cost worth naming: a background task the
   customer's tool spawns and does not await inherits a context that still
   references this call's cell, so a failure in it can be recorded after the
   slot closed — retaining that exception and its traceback for as long as the
   child lives. Nothing reads the cell after `__exit__`, so it cannot reach an
   event; it is a lifetime footnote, not a correctness one.

**Never alters what the customer's server does.** The wrapping form catches,
records and re-raises the same exception object with a bare ``raise``; the
probing form only reads `sys.exc_info()` and returns the original's own
result. A tap that fails is logged and degrades to the no-tap payload; it
never raises into the customer's server.

Nothing version-specific from ``mcp``/``fastmcp`` is imported here, so this
module loads under either SDK major. WHERE the tap is placed is era-specific
and belongs to each adapter; WHAT it records does not, and belongs here.
"""

from __future__ import annotations

import contextvars
import inspect
import sys
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any

from agentcat.modules.exceptions import capture_exception
from agentcat.modules.logging import write_to_log
from agentcat.types import ErrorData


class _Cell:
    """One tool call's exception slot.

    ``baseline`` is whatever exception was already being handled when the slot
    opened. `capture_in_flight` reads `sys.exc_info`, which answers for the
    whole stack rather than one frame, so a conversion site reached with no
    exception of its own would otherwise record an older frame's — the SDK
    calls ``_make_error_result`` for a bad return type too, outside any
    ``except``. Remembering the baseline is how that reads as "nothing to
    record" instead of as someone else's failure.

    ``exc_seams`` is the chain of seam entries that were open, in this task,
    when the stored exception was recorded. It is what answers "did this
    failure actually escape?" — see the module docstring. Nothing here counts:
    the cell is shared with every task that inherits it, so a count would be
    everyone's and an identity is only its own.
    """

    __slots__ = ("exc", "exc_seams", "baseline")

    def __init__(self, baseline: BaseException | None) -> None:
        self.exc: BaseException | None = None
        self.exc_seams: tuple[object, ...] = ()
        self.baseline = baseline

    def record(self, exc: BaseException) -> None:
        """Store ``exc`` as this call's failure so far. Last write wins."""
        self.exc = exc
        self.exc_seams = _open_seams.get()

    def discard_unescaped(self, seam: object) -> None:
        """Drop a capture that ``seam`` handled inside itself.

        Called when that seam entry returns NORMALLY: a capture whose chain
        names it was raised within it and did not come back out, so it did not
        produce the result the agent is about to be handed. A capture from a
        sibling — sequential or concurrent — does not name it, and survives to
        be settled by whichever seam actually encloses them both.
        """
        if self.exc is not None and seam in self.exc_seams:
            self.exc = None
            self.exc_seams = ()


# Holds a reference to the innermost open cell. Never the exception itself:
# see the module docstring for why that distinction is the whole design.
_open_cell: contextvars.ContextVar[_Cell | None] = contextvars.ContextVar(
    "agentcat_inner_tap", default=None
)

# The seam entries currently open in THIS task, outermost first. A ContextVar
# rather than a field on the cell, because the question it answers is about one
# task's call stack and the cell is shared with every task that inherits it.
_open_seams: contextvars.ContextVar[tuple[object, ...]] = contextvars.ContextVar(
    "agentcat_inner_tap_seams", default=()
)


class InnerTap:
    """The capture slot for one ``tools/call``. Use via `inner_tap`."""

    __slots__ = ("_cell", "_token")

    def __init__(self) -> None:
        self._cell = _Cell(_in_flight())
        self._token: contextvars.Token[_Cell | None] | None = None

    def __enter__(self) -> InnerTap:
        self._token = _open_cell.set(self._cell)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Every exit path, the raise included: a token reset restores exactly
        # the cell that was open before, so a nested call (a tool that calls
        # its own server) hands the slot back to its caller rather than
        # clearing it.
        if self._token is not None:
            _open_cell.reset(self._token)
            self._token = None

    @property
    def captured(self) -> BaseException | None:
        """The exception a tap recorded for this call, if one did."""
        return self._cell.exc

    def error(self, flattened: Any) -> ErrorData:
        """The event's ``error`` payload for a call that failed.

        The tapped exception when there is one — real type, formatted stack,
        stack frames and the ``__cause__`` chain the SDK wrapped it in.
        Otherwise ``flattened``, whatever the adapter could make of the result:
        an upstream error a proxy passed through, or a tool that returned
        ``is_error`` without anything having been raised at all, both of which
        have no local exception and never will.
        """
        # `is not None`, not truthiness: an exception class that defines
        # `__len__` or `__bool__` can be falsy, and losing its traceback to
        # that would be a very quiet bug.
        exc = self._cell.exc
        return capture_exception(exc if exc is not None else flattened)


def inner_tap() -> InnerTap:
    """Open a capture slot around one tool call.

    >>> with inner_tap() as tap:          # doctest: +SKIP
    ...     result = await run_the_customers_handler()
    ...     error = tap.error(result) if is_error(result) else None
    """
    return InnerTap()


def capture(exc: BaseException) -> None:
    """Record ``exc`` as the failure behind the call currently in flight.

    Last write wins: a tool that composes another tool sees the sub-call's
    failure first and its own second, and the event has to describe the one the
    agent was told about. A capture that turns out never to have escaped is
    discarded separately, by `tapped`.
    """
    try:
        cell = _open_cell.get()
        if cell is not None:
            cell.record(exc)
    except Exception as e:  # pragma: no cover - defensive
        write_to_log(f"Warning: inner tap could not record an exception - {e}")


def capture_in_flight(surfaced_message: str) -> None:
    """Record the exception being handled right now, if the SDK is surfacing it.

    For a conversion site that keeps only ``str(e)``: called from inside the
    SDK's own ``except`` block, `sys.exc_info` still holds the live exception
    with its traceback. Costs the customer nothing — no wrapper frame, no
    re-raise — so it is preferred wherever a conversion site can be observed
    directly.

    ``surfaced_message`` is the text the site is about to put on the wire, and
    the capture is taken **only when it is exactly ``str(exc)``**. One site on
    lowlevel v1 hands over the exception's own message
    (``_make_error_result(str(e))``); the schema-validation sites hand over a
    sentence the SDK composed instead (``f"Input validation error: …"``).
    Recording those would replace the one-line message the agent saw with a
    multi-line ``jsonschema`` dump that embeds the schema and the offending
    argument value — a payload change, on a common error class, that no one
    asked for. Equality is the exact test that separates the two.
    """
    try:
        exc = _in_flight()
        cell = _open_cell.get()
        if exc is None or cell is None or exc is cell.baseline:
            return
        if str(exc) == surfaced_message:
            cell.record(exc)
    except Exception as e:  # pragma: no cover - defensive
        write_to_log(f"Warning: inner tap could not record an exception - {e}")


def _in_flight() -> BaseException | None:
    try:
        return sys.exc_info()[1]
    except Exception:  # pragma: no cover - defensive
        return None


def tapped(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """``fn`` with a catch-record-re-raise around it, and nothing else changed.

    The re-raise is bare, so the SDK above receives the same exception object
    it would have received untracked and the wire result is byte-identical. The
    one observable difference is the tap's own frame in the traceback, which is
    what a middleware seam costs on every era that has one.

    The seam also carries the bookkeeping a composing tool needs: each entry
    takes a marker of its own and pushes it onto this task's chain of open
    seams, and on a NORMAL return it drops any capture recorded under that
    marker — that failure was handled in here and is not what the agent is
    being told. A capture it re-raises is left alone, which is the whole point
    of the tap, and so is a capture from a seam that merely ran beside it.
    """

    async def tap(*args: Any, **kwargs: Any) -> Any:
        cell = _open_cell.get()
        seam = object()
        token = _open_seams.set((*_open_seams.get(), seam))
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            capture(exc)
            raise
        else:
            if cell is not None:
                cell.discard_unescaped(seam)
            return result
        finally:
            _open_seams.reset(token)

    return tap


def probing(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``fn`` with a `capture_in_flight` read in front of it, and nothing else.

    For a *conversion* site rather than an invocation one: something the SDK
    calls from inside its own ``except`` to build the error result. The
    customer's exception never passes through here, so there is nothing to
    catch and re-raise — and nothing of ours ends up in their traceback.

    The message the site was handed is what gates the read (see
    `capture_in_flight`). It is read defensively — first positional argument,
    then the keyword lowlevel v1 names it by — because a call shape this does
    not recognize must cost a capture, never the customer's error path.
    """

    def probe(*args: Any, **kwargs: Any) -> Any:
        message = args[0] if args else kwargs.get("error_message")
        if isinstance(message, str):
            capture_in_flight(message)
        return fn(*args, **kwargs)

    return probe


def tap_method(
    owner: Any,
    name: str,
    state: dict[str, Any],
    key: str,
    wrap: Callable[[Any], Any] = tapped,
    accepts: Callable[[Any], bool] = inspect.iscoroutinefunction,
) -> bool:
    """Put a tap over ``owner.name``, in place. Never raises.

    ``state`` is the adapter's per-server install state and ``key`` the slot to
    record under, so a repeated ``track()`` re-wraps the customer's original
    rather than stacking a second tap on top of the first. ``wrap`` selects the
    form — `tapped` around an invocation, `probing` in front of a conversion.

    ``accepts`` is the precondition of that form, checked against the callable
    about to be wrapped. `tapped` awaits what it wraps, so a target that is not
    a coroutine function would turn every tool call into
    ``TypeError: … can't be used in 'await' expression`` — a break the customer
    sees on every call, and one the install-time ``try`` cannot catch because it
    happens later. Every seam this ships with is ``async def`` on both majors;
    a customer subclass that overrode one synchronously is refused the tap
    instead, and keeps the previous no-tap payload.
    """
    original_key = f"orig_tap_{key}"
    installed_key = f"tap_{key}"
    try:
        current = getattr(owner, name, None)
        if not callable(current):
            return False
        # Ours from an earlier pass? Then the original is the one we recorded.
        original = (
            state.get(original_key) if current is state.get(installed_key) else current
        )
        if original is None:
            return False
        if not accepts(original):
            write_to_log(
                f"Warning: '{name}' on this server is not the shape the inner "
                "tap wraps, so it is left alone; tool errors keep the surfaced "
                "message but lose the stack"
            )
            return False
        wrapper = wrap(original)
        # Installed first, recorded second: a `setattr` a server refuses would
        # otherwise leave the state claiming a tap that is not there, and the
        # next `track()` would unwrap an original nothing ever wrapped.
        setattr(owner, name, wrapper)
        state[original_key] = original
        state[installed_key] = wrapper
        return True
    except Exception as e:
        write_to_log(
            f"Warning: could not install the inner tap on '{name}'; tool errors "
            f"on this server keep the surfaced message but lose the stack - {e}"
        )
        return False
