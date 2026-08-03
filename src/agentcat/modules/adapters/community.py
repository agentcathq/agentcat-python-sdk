"""Adapter for community FastMCP (the 3.x and 4.x eras).

Community FastMCP has a first-class middleware seam, so unlike the official
SDKs there is nothing to monkey-patch: one object inserted at index 0 of
``server.middleware`` sees every ``tools/list`` and ``tools/call`` before any
other layer does.

Era-specific knowledge only: FastMCP's snake_case ``Tool.parameters`` /
``output_schema``, its ``MiddlewareContext``/``ToolResult`` shapes, and the
provider registration used for ``get_more_tools``. Every decision about what to
resolve, strip, decorate or publish belongs to
:mod:`agentcat.modules.callpath` and is not re-derived here — the v1 middleware
this replaces reimplemented all of it inline.

Three invariants the middleware exists to protect:

- **Nothing customer-owned is mutated.** Tool schemas are copied before the
  injection pipeline rewrites them in place, and the request message is cloned
  via ``model_copy`` rather than rebuilt — a rebuilt ``CallToolRequestParams``
  silently drops ``_meta`` (and, from the 2026 era, ``input_responses`` /
  ``request_state``), which severs multi-round-trip continuations.
- **Index 0 is outermost.** FastMCP builds its chain with
  ``for mw in reversed(self.middleware)``, so element 0 runs first. A caching or
  dereferencing middleware below us therefore keys on the STRIPPED arguments and
  never caches our injected schemas.
- **The event records the call as the agent made it.** Raw (unstripped)
  arguments and the customer's undecorated result; the mint-back is wire-only.

``AgentCatMiddleware`` deliberately does not subclass ``fastmcp``'s
``Middleware``: that would require a module-scope ``fastmcp`` import, and
``import agentcat`` has to succeed with no MCP SDK installed at all. FastMCP
calls middleware objects, so the dispatch its base class performs is
reproduced in ``__call__``.
"""

from __future__ import annotations

import copy
import weakref
from typing import Any

from agentcat.modules.adapters._common import (
    current_tracking_data,
    install_state,
    now_ms,
    response_payload,
)
from agentcat.modules.adapters._inner_tap import inner_tap, tap_method
from agentcat.modules.callpath import (
    ResolvedCall,
    decorate_content,
    detect_mrtr,
    get_stripped_arguments,
    publish_tool_call_event,
    resolve_call,
    structured_mirror,
)
from agentcat.modules.constants import GET_MORE_TOOLS_NAME
from agentcat.modules.exceptions import capture_exception
from agentcat.modules.injection import ToolSpec, build_injected_schemas
from agentcat.modules.logging import write_to_log
from agentcat.modules.request_extra import extra_from_request_context
from agentcat.modules.tools import GET_MORE_TOOLS_DESCRIPTION
from agentcat.types import AgentCatData, ErrorData

# Eras this adapter serves. Verified against FastMCP 4.0.0b1: it renamed
# nothing this middleware touches — ``Tool.output_schema`` and
# ``ToolResult.structured_content`` are the live field names on both sides, and
# every read that COULD have drifted goes through ``_field``, snake-first with a
# camelCase fallback — so no code path branches on the era. (The MRTR
# continuation probes in ``on_call_tool`` are bare ``getattr``s: the 2026-era
# ``input_responses`` / ``request_state`` fields postdate the camelCase bridge
# entirely, so there is no older spelling for either to fall back to, and on
# era 3 the message model has neither.) The one v4-only difference this
# adapter had to answer, the era's second dispatch pass, turned out to be a
# property of the message rather than of the generation, and is handled as one
# (``_is_typed_message``). The era is recorded anyway: it names which generation
# an installed middleware was built for in the log, and it is the seam a later
# divergence would use.
ERA_V3 = 3
ERA_V4 = 4


def _field(obj: Any, snake: str, camel: str) -> Any:
    """snake_case first, camelCase fallback (v3 keeps a bridge; v4 does not)."""
    value = getattr(obj, snake, None)
    return getattr(obj, camel, None) if value is None else value


def _is_typed_message(message: Any) -> bool:
    """Whether ``context.message`` is the request model the hooks expect.

    FastMCP 4 dispatches the middleware chain a SECOND time for a component
    request that failed *before* the interior chain ran — malformed params, a
    routing failure — so that ``on_message``/``on_request`` observe every
    inbound message (``server/low_level.py::_dispatch_component``). That pass
    carries the same ``method``, but its message is the RAW params **mapping**:
    reconstructing a typed model is exactly what fails on a malformed message,
    so FastMCP deliberately does not try (``_raw_message``).

    It is observation only — the pass's ``call_next`` re-raises the original
    failure rather than dispatching — so there is nothing here for AgentCat to
    do: no tool ran, and no event describes a request that never became one.
    What matters is that we get out of the way intact. ``on_call_tool`` clones
    the message with ``model_copy``, which a ``dict`` does not have, and the
    ``AttributeError`` that raised REPLACED the customer server's own ``-32602``
    on the wire — an analytics library changing what a client is told.

    FastMCP 3 has no such pass, so this only ever fires on 4.x; it is written
    as a property of the message rather than an era branch because it is the
    honest precondition of both typed hooks either way.
    """
    return hasattr(message, "model_copy")


def _request_context(context: Any) -> Any:
    """The MCP ``RequestContext`` behind a middleware call, or None."""
    try:
        fastmcp_context = context.fastmcp_context
        return fastmcp_context.request_context if fastmcp_context else None
    except Exception:
        return None


def _connection_key(source: Any) -> Any:
    """The per-connection object a handshake capture is filed under, or None.

    FastMCP builds one ``ServerSession`` per connection — and, under stateless
    HTTP, a fresh one per REQUEST — so it is the only thing in reach that tells
    one caller from another. ``initialize`` runs before the MCP
    ``RequestContext`` exists (``fastmcp_context.request_context`` is None
    there), so the session is read off the FastMCP context when capturing and
    off the request context when looking up; on a stateful connection those are
    the same object, and under stateless HTTP they deliberately are not.
    """
    try:
        return getattr(source, "session", None)
    except Exception:
        return None


def _flattened(result: Any) -> Any:
    """A ``ToolResult`` in the shape the error extractor recognizes.

    ``capture_exception`` reads the wire ``CallToolResult`` (camelCase
    ``isError`` + ``content``), not FastMCP's snake_case ``ToolResult``, so the
    result is converted first — otherwise the recorded message would be a
    pydantic repr of the model.
    """
    try:
        return result.to_mcp_result()
    except Exception:
        return result


class AgentCatMiddleware:
    """One middleware for both community eras; ``era`` selects the field bridge.

    Stateless apart from the initialize-time client capture: the tracking data
    is re-read per request so a repeated ``track()`` takes effect, and a
    ``ResolvedCall`` is never held across rounds (it carries that round's
    request/extra for the customer's tag and property callbacks).
    """

    def __init__(self, data: AgentCatData, server: Any, era: int) -> None:
        self._data = data
        self._server = server
        self._era = era
        # Handshake clientInfo, the last rung of the client identity ladder,
        # filed PER CONNECTION. One middleware object serves every connection,
        # so a single "last seen" slot would let a later client's initialize
        # rename an earlier client's call — which is exactly what happens under
        # stateless HTTP, where the session carries no client_params and the
        # ladder reaches this rung on every call. Weakly keyed, so a finished
        # connection's entry goes away with its session.
        self._handshake_clients: weakref.WeakKeyDictionary[Any, dict[str, Any]] = (
            weakref.WeakKeyDictionary()
        )
        # The get_more_tools Tool object install_community registered, if any,
        # and — once conceded — where it moves to. The conceded record is kept
        # rather than dropped so a listing that was already in flight when we
        # conceded still filters out the tool we just un-registered. See
        # _concede_get_more_tools.
        self._registered_get_more_tools: Any = None
        self._conceded_get_more_tools: Any = None

    # ── dispatch ────────────────────────────────────────────────────────────

    async def __call__(self, context: Any, call_next: Any) -> Any:
        method = getattr(context, "method", None)
        # Deliberately silent: on v4 this is the expected outer pass, which
        # fires on every malformed request, and a log line there would be noise
        # a customer cannot act on. The residual cost is that a layer inserted
        # ABOVE us after track() which hands down a mapping would take tracking
        # dead with no diagnostic — an inversion of the index-0 install that
        # nothing in FastMCP does today.
        if not _is_typed_message(getattr(context, "message", None)):
            return await call_next(context)
        if method == "initialize":
            return await self.on_initialize(context, call_next)
        if method == "tools/list":
            return await self.on_list_tools(context, call_next)
        if method == "tools/call":
            return await self.on_call_tool(context, call_next)
        return await call_next(context)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _current_data(self) -> AgentCatData:
        """The tracking data as of this request, so a re-track takes effect."""
        return current_tracking_data(self._server, self._data)

    def _meta_sources(self, message: Any, request_context: Any) -> list[Any]:
        """Where per-request client identity may ride, best rung first."""
        return [
            getattr(message, "meta", None),
            getattr(request_context, "meta", None),
        ]

    def _legacy_client_info(self, request_context: Any) -> Any:
        """Initialize-time ``clientInfo`` for THIS connection, or None.

        Two forms of the same rung (design §7, rung 3): what the SDK kept on
        the session, then what this middleware saw on the same session's
        ``initialize``. Both are per connection, so neither can name a client
        that did not make this call. A stateless-HTTP request gets a brand new
        session that never handshook, so it misses both and identity floors at
        empty — the honest answer, and the one ``stateless=True`` produced in
        1.x. Under the 2026-era protocol there is no ``initialize`` at all, so
        this rung is simply never populated.
        """
        try:
            info = request_context.session.client_params.clientInfo
        except Exception:
            info = None
        if info is not None:
            return info
        return self._remembered_handshake_client(_connection_key(request_context))

    def _remember_handshake_client(self, key: Any, client: dict[str, Any]) -> None:
        """File one connection's handshake identity. Never raises."""
        if key is None:
            return
        try:
            self._handshake_clients[key] = client
        except TypeError as e:
            # A session object that cannot be weakly referenced cannot be told
            # apart from any other, so it gets no entry rather than a shared one.
            write_to_log(f"Warning: could not file the handshake clientInfo - {e}")

    def _remembered_handshake_client(self, key: Any) -> dict[str, Any] | None:
        if key is None:
            return None
        try:
            return self._handshake_clients.get(key)
        except TypeError:
            return None

    def _specs(self, tools: list[Any]) -> list[ToolSpec]:
        """Copies of the listed schemas, ready for the in-place injection pass.

        Only the schema dicts are copied. Deep-copying the ``Tool`` itself
        raises on every tool holding live runtime state — an OpenAPI tool's
        ``httpx.AsyncClient`` carries a ``threading.RLock`` — which silently
        dropped injection for whole servers in v1.
        """
        return [
            ToolSpec(
                tool.name,
                copy.deepcopy(getattr(tool, "parameters", None) or {}),
                copy.deepcopy(_field(tool, "output_schema", "outputSchema")),
            )
            for tool in tools
        ]

    # ── hooks ───────────────────────────────────────────────────────────────

    async def on_initialize(self, context: Any, call_next: Any) -> Any:
        """Capture this connection's client identity. Publishes nothing (v2).

        The MCP ``RequestContext`` does not exist yet at this point, so the
        connection is identified by the FastMCP context's session — the same
        object the tool call will present.
        """
        try:
            message = context.message
            params = getattr(message, "params", None) or message
            info = _field(params, "client_info", "clientInfo")
            if info is not None:
                self._remember_handshake_client(
                    _connection_key(getattr(context, "fastmcp_context", None)),
                    {
                        "name": getattr(info, "name", None),
                        "version": getattr(info, "version", None),
                    },
                )
        except Exception as e:
            write_to_log(f"Warning: could not read initialize clientInfo - {e}")
        return await call_next(context)

    async def on_list_tools(self, context: Any, call_next: Any) -> Any:
        """Serve the customer's listing with AgentCat's parameters injected.

        Publishes nothing: v2 intercepts ``tools/list`` for schema injection
        only. On any failure the customer's own list is served unmodified.
        """
        # The list handed back here has passed through every layer below us, so
        # it may hold COPIES of our own tool — hence authoritative=False.
        tools = await self._concede_get_more_tools(
            list(await call_next(context)), authoritative=False
        )
        tracking = self._current_data()
        try:
            specs = self._specs(tools)
            injected = build_injected_schemas(
                specs, tracking.options, tracking.reported_conflicts
            )
            tracking.injected_params_registry = injected.injected_params
            tracking.output_injection_registry = injected.output_injected
            # Union, never replace: membership only grows, and a concurrent
            # listing on another instance may already have recorded a tool this
            # one did not see.
            tracking.declared_session_params |= injected.declared_session_params
            return [
                tool.model_copy(update=self._schema_update(spec))
                for tool, spec in zip(tools, specs, strict=True)
            ]
        except Exception as e:
            write_to_log(
                "Warning: tools/list injection failed, serving the customer's "
                f"unmodified list - {e}"
            )
            return tools

    def _is_ours(self, tool: Any, ours: Any) -> bool:
        """Whether a listed tool is the ``get_more_tools`` AgentCat registered.

        Object identity is the exact answer but not a sufficient one: layers
        below us hand back ``model_copy`` results — a customer middleware that
        re-stamps tools, FastMCP's own visibility transforms, v4's
        dereferencing middleware — and **a copy of our own tool must never read
        as a customer's**. So identity first, then two copy-tolerant fallbacks:
        the underlying function object, which a ``model_copy`` carries over,
        and our canonical description, which is the copy an agent would see.

        This is the single definition of "ours" for both halves of the
        concession: whatever it calls ours is what the decision ignores AND
        what the advertised list drops, so the two can never disagree.
        """
        if ours is None:
            return False
        if tool is ours:
            return True
        if getattr(tool, "name", None) != GET_MORE_TOOLS_NAME:
            return False
        our_fn = getattr(ours, "fn", None)
        if our_fn is not None and getattr(tool, "fn", None) is our_fn:
            return True
        return bool(getattr(tool, "description", None) == GET_MORE_TOOLS_DESCRIPTION)

    def _foreign_get_more_tools(self, tools: list[Any], ours: Any) -> bool:
        """Whether anyone but us supplies ``get_more_tools`` in this listing."""
        return any(
            getattr(tool, "name", None) == GET_MORE_TOOLS_NAME
            and not self._is_ours(tool, ours)
            for tool in tools
        )

    async def _concede_get_more_tools(
        self, tools: list[Any], authoritative: bool
    ) -> list[Any]:
        """Give the name back the moment another provider turns out to own it.

        ``install_community`` can only read the LOCAL provider synchronously,
        so a ``get_more_tools`` supplied by a mounted sub-server, a proxy, an
        OpenAPI provider or ``add_provider`` is invisible to it. The local
        provider is added first and wins aggregation, so ours would answer for
        theirs — the one thing the ownership gate exists to prevent (spec §12).
        An all-provider listing is the first view that can settle it, so both
        views we get — the client-facing one and the rebuild-on-demand one —
        run this.

        ``authoritative`` says whether ``tools`` already IS the raw
        all-provider view. When it is not, a positive finding is re-checked
        against that raw view before anything is un-registered: if a layer
        below us rewrote our own tool past recognition, the honest answer is
        "nobody else supplies this" and the safe move is to leave the
        registration alone. Un-registering on a false positive would advertise
        a ``get_more_tools`` that no longer resolves, and the next call to it
        would fail.
        """
        ours = self._registered_get_more_tools
        if ours is None:
            # Already conceded — keep filtering, so a listing that was already
            # in flight when we conceded cannot advertise the tool we removed.
            conceded = self._conceded_get_more_tools
            if conceded is None:
                return tools
            return [tool for tool in tools if not self._is_ours(tool, conceded)]

        if not self._foreign_get_more_tools(tools, ours):
            return tools
        if not authoritative and not await self._provider_view_has_a_foreign_one(ours):
            return tools

        self._registered_get_more_tools = None
        self._conceded_get_more_tools = ours
        removed = _unregister_get_more_tools(self._server, ours)
        write_to_log(
            "A provider on this server supplies its own get_more_tools; "
            + (
                "un-registered AgentCat's so the customer's tool answers."
                if removed
                else "AgentCat's was already gone from the local registry."
            )
        )
        return [tool for tool in tools if not self._is_ours(tool, ours)]

    async def _provider_view_has_a_foreign_one(self, ours: Any) -> bool:
        """Re-ask the question of the raw provider listing, middleware bypassed.

        Only reached when a processed listing already looked foreign, so the
        extra listing is paid on the rare path, never on every ``tools/list``.
        """
        try:
            listed = list(await self._server.list_tools(run_middleware=False))
        except Exception as e:
            write_to_log(
                "Warning: could not re-read the provider listing to confirm a "
                f"get_more_tools owner; leaving AgentCat's registered - {e}"
            )
            return False
        return self._foreign_get_more_tools(listed, ours)

    def _schema_update(self, spec: ToolSpec) -> dict[str, Any]:
        update: dict[str, Any] = {"parameters": spec.input_schema}
        if spec.output_schema is not None:
            update["output_schema"] = spec.output_schema
        return update

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        tracking = self._current_data()
        options = tracking.options
        message = context.message
        name = getattr(message, "name", None) or "Unknown Tool"
        raw_arguments = dict(getattr(message, "arguments", None) or {})

        async def rebuild() -> list[ToolSpec]:
            """The list source for registry rebuild-on-demand (changelog 6.3).

            ``run_middleware=False`` is the seam in both community eras: it
            returns the provider's own tools without re-entering this hook.
            That is the raw all-provider view, so it settles the get_more_tools
            ownership question too — and this is the path a tools/call takes on
            an instance that never served a listing, which is precisely the
            case install_community registers eagerly for.
            """
            listed = list(await self._server.list_tools(run_middleware=False))
            return self._specs(
                await self._concede_get_more_tools(listed, authoritative=True)
            )

        stripped = await get_stripped_arguments(
            tracking, options, name, raw_arguments, rebuild
        )
        stripped_context = context.copy(
            message=message.model_copy(update={"arguments": stripped})
        )

        # Tracing off: still strip what we injected, so the customer's tool runs
        # exactly as it would untracked — then get out of the way. No handle
        # resolution, no mint-back (there is no session_id parameter to echo), and
        # no event.
        if not options.enable_tracing:
            return await call_next(stripped_context)

        request_context = _request_context(context)
        try:
            # Resolved fresh every round: the ResolvedCall carries this round's
            # message/extra for the customer's tag and property callbacks.
            resolved = await resolve_call(
                tracking,
                name,
                raw_arguments,
                message,
                request_context,
                meta_sources=self._meta_sources(message, request_context),
                legacy_client=lambda: self._legacy_client_info(request_context),
            )
        except Exception as e:
            # Belt and braces at the customer boundary: the resolvers are all
            # documented not to raise, but a tool call must never fail because
            # analytics did. Degrade to an untraced call.
            write_to_log(
                f"Warning: AgentCat resolution failed for tool '{name}', running "
                f"it untraced - {e}"
            )
            return await call_next(stripped_context)

        started = now_ms()
        extra_params = extra_from_request_context(
            request_context, getattr(context, "fastmcp_context", None)
        )

        # The tap's slot is open for exactly the rest of the chain and the
        # publish that reads it, and closes on every exit path including the
        # raise below.
        with inner_tap() as tap:
            try:
                result = await call_next(stripped_context)
            except Exception as e:
                # FastMCP surfaces a failing tool as a raised error, so unlike
                # the official adapters this path holds the live exception,
                # with its type and traceback intact.
                await self._publish(
                    tracking,
                    resolved,
                    name,
                    raw_arguments,
                    response=None,
                    is_error=True,
                    error=capture_exception(e),
                    started=started,
                    mrtr=None,
                    extra_params=extra_params,
                )
                raise

            mrtr = detect_mrtr(
                self._result_type(result),
                getattr(message, "input_responses", None) is not None,
                getattr(message, "request_state", None) is not None,
            )
            is_error = bool(getattr(result, "is_error", False))
            await self._publish(
                tracking,
                resolved,
                name,
                raw_arguments,
                response=response_payload(result),
                is_error=is_error,
                # An `is_error` result that never raised THROUGH us: a layer
                # below answered with one. The tap holds the exception when a
                # local one produced it, and there simply is none when a proxy
                # passed an upstream error through — then the surfaced message
                # is the whole truth.
                error=tap.error(_flattened(result)) if is_error else None,
                started=started,
                mrtr=mrtr,
                extra_params=extra_params,
            )

        # An intermediate multi-round-trip round is never decorated: only the
        # completing round carries the mint-back (changelog 6.4).
        if mrtr == "input_required":
            return result
        return self._decorated(result, resolved, name, tracking)

    def _result_type(self, result: Any) -> str | None:
        """The 2026-era ``resultType`` discriminator, however it is spelled."""
        declared = _field(result, "result_type", "resultType")
        if isinstance(declared, str):
            return declared
        return (
            "input_required"
            if type(result).__name__ == "InputRequiredToolResult"
            else None
        )

    async def _publish(
        self,
        tracking: AgentCatData,
        resolved: ResolvedCall,
        name: str,
        raw_arguments: dict[str, Any],
        response: dict[str, Any] | None,
        is_error: bool,
        error: ErrorData | None,
        started: int,
        mrtr: str | None,
        extra_params: dict[str, Any],
    ) -> None:
        await publish_tool_call_event(
            self._server,
            tracking,
            resolved,
            name,
            raw_arguments,
            response=response,
            is_error=is_error,
            error=error,
            duration_ms=now_ms() - started,
            mrtr=mrtr,
            extra_params=extra_params,
        )

    def _decorated(
        self,
        result: Any,
        resolved: ResolvedCall,
        name: str,
        tracking: AgentCatData,
    ) -> Any:
        """The wire result with the mint-back appended, or the original."""
        update: dict[str, Any] = {}
        content = getattr(result, "content", None)
        decorated = decorate_content(
            list(content) if isinstance(content, list) else None,
            resolved.resolution,
            _text_block,
        )
        if decorated is not None:
            update["content"] = decorated
        mirrored = structured_mirror(
            _field(result, "structured_content", "structuredContent"),
            resolved.resolution,
            name,
            tracking.output_injection_registry,
        )
        if mirrored is not None:
            update["structured_content"] = mirrored
        # A copy, never an in-place edit: the event above still references the
        # customer's own result object.
        return result.model_copy(update=update) if update else result


def _text_block(text: str) -> Any:
    from mcp.types import TextContent

    return TextContent(type="text", text=text)


def _customer_owns_get_more_tools(server: Any) -> bool:
    """Whether a tool named ``get_more_tools`` is already registered.

    Read off the local provider's component store, the only synchronous view of
    the registry (``list_tools`` is async and ``install_community`` is not).
    Registering over an existing tool would replace the customer's handler with
    our canned reply, and nothing AgentCat does may alter tool behavior
    (spec §12), so an unreadable registry is treated as owned.
    """
    try:
        components = server._local_provider._components.values()
    except Exception as e:
        write_to_log(
            "Warning: could not read the tool registry to check for a "
            f"get_more_tools collision; skipping registration - {e}"
        )
        return True
    return any(getattr(c, "name", None) == GET_MORE_TOOLS_NAME for c in components)


def _unregister_get_more_tools(server: Any, ours: Any) -> bool:
    """Remove the tool we registered — and only ever that exact object.

    Returns whether anything was actually removed, so the caller's log never
    claims an un-registration that did not happen.

    Removing by name would be wrong: a customer who registers their own
    ``get_more_tools`` after ``track()`` replaces ours in the local store under
    the same key, and deleting by name would then delete theirs. The identity
    check makes the removal a no-op in exactly that case.
    """
    try:
        provider = server._local_provider
        if provider._components.get(ours.key) is not ours:
            return False
        provider.remove_tool(GET_MORE_TOOLS_NAME)
        return True
    except Exception as e:
        write_to_log(f"Warning: could not un-register get_more_tools - {e}")
        return False


def _register_get_more_tools(server: Any) -> Any:
    """Register ``get_more_tools`` on the server's own provider; returns it.

    Provider registration rather than a listing-time append, so the tool
    survives the compositions community FastMCP is built around: a mounted or
    proxied parent enumerates through the provider and never runs this
    server's middleware. The returned object is what
    ``AgentCatMiddleware._concede_get_more_tools`` compares listings against.
    """
    # `fastmcp.tools` is the public home of Tool in both eras;
    # `fastmcp.tools.tool` is a sys.modules shim that no type checker resolves.
    from fastmcp.tools import Tool

    from agentcat.modules.tools import (
        GET_MORE_TOOLS_DESCRIPTION,
        GET_MORE_TOOLS_SCHEMA,
        handle_report_missing,
    )

    async def get_more_tools(context: str) -> str:
        result = await handle_report_missing({"context": context})
        block = result.content[0] if result.content else None
        return getattr(block, "text", "No additional tools available.")

    # Spec defaults assume the worst; declare the honest hint so
    # annotation-aware clients skip the confirmation prompt. Passed as a plain
    # mapping, not the v3 `ToolAnnotations` model — pydantic coerces it into
    # whichever annotations type the running FastMCP declares.
    annotations: Any = {"readOnlyHint": True}
    tool = Tool.from_function(
        get_more_tools,
        name=GET_MORE_TOOLS_NAME,
        description=GET_MORE_TOOLS_DESCRIPTION,
        annotations=annotations,
    )
    # The generated schema loses the agent-facing parameter copy (pydantic
    # writes no description for a bare `context: str`), so the canonical
    # schema — byte-guarded against the TypeScript SDK — is substituted.
    schema = copy.deepcopy(GET_MORE_TOOLS_SCHEMA)
    registered = server.add_tool(tool.model_copy(update={"parameters": schema}))
    write_to_log("Registered get_more_tools on the community provider")
    return registered


def install_community(server: Any, data: AgentCatData, era: int) -> None:
    """Insert the AgentCat middleware at index 0 of a community FastMCP server.

    ``era`` is 3 or 4. Idempotent: a repeated ``track()`` replaces the existing
    middleware rather than stacking a second one — a stacked pass would inject
    session_id on the inside, find it already present on the outside, and never
    record it as strippable, handing the customer's tool a parameter it never
    declared.

    ``get_more_tools`` is registered here so it exists for a ``tools/call`` that
    arrives on an instance which never served a listing. That is only half the
    ownership check — this function can read the local provider synchronously
    but not the mounted/proxied/OpenAPI ones — so every all-provider view we
    later get (a client listing, or the rebuild-on-demand one) re-checks and
    concedes the name if anyone else turns out to supply it.

    Two residual asymmetries of provider registration, both requiring a
    customer tool literally named ``get_more_tools``:

    - Turning ``enable_report_missing`` OFF on a *later* ``track()`` leaves the
      already-registered tool in place; disable it before the first ``track()``.
    - If the customer registers theirs on the LOCAL provider after ``track()``,
      the server's own ``on_duplicate`` policy decides: the default (``warn``)
      lets theirs replace ours, which is the right outcome, but ``ignore``
      would discard theirs and ``error`` would raise on their registration.
      ``_on_duplicate`` is readable synchronously, so the ``error`` sub-case —
      the only one where AgentCat makes the customer's own code raise — could
      be guarded at no cost to ``warn``/``replace`` servers if it ever bites.
      Registering before ``track()`` avoids all of it today, and that is what
      the local gate below already handles.
    """
    chain = server.middleware
    existing = [mw for mw in chain if isinstance(mw, AgentCatMiddleware)]
    for mw in existing:
        chain.remove(mw)
    # Index 0 is outermost: FastMCP builds the chain over `reversed(middleware)`.
    middleware = AgentCatMiddleware(data, server, era)
    chain.insert(0, middleware)

    # Carry a previous install's registration forward. The local gate below
    # cannot tell OUR get_more_tools from a customer's, so on a re-track it
    # reports "already owned" and registration is skipped — and without this the
    # fresh middleware would hold no marker, could never concede the name, and a
    # re-tracked server would go back to hijacking a provider-supplied tool.
    for previous in existing:
        if previous._registered_get_more_tools is not None:
            middleware._registered_get_more_tools = previous._registered_get_more_tools
            break
        if previous._conceded_get_more_tools is not None:
            middleware._conceded_get_more_tools = previous._conceded_get_more_tools
            break

    register = data.options.enable_report_missing and not _customer_owns_get_more_tools(
        server
    )
    if register:
        try:
            middleware._registered_get_more_tools = _register_get_more_tools(server)
        except Exception as e:
            write_to_log(f"Warning: could not register get_more_tools - {e}")

    tapped = _arm_inner_tap(server)
    write_to_log(
        f"Installed community adapter (era {era}) on server {id(server)} "
        f"at middleware index 0 (replaced={len(existing)}, "
        f"inner-tap={tapped or 'none'})"
    )


def _arm_inner_tap(server: Any) -> list[str]:
    """Arm the inner tap on ``FastMCP.call_tool``. Never raises.

    The one thing on a community server that a middleware seam cannot do. The
    middleware chain sees a raised tool error and already records it in full —
    but a layer BELOW us that answers with an ``is_error`` result instead of
    raising (``providers/proxy.py`` does it deliberately for an upstream error,
    and any error-handling middleware a customer writes does it too) leaves
    nothing for the chain to catch. The tap has to sit under all of them.

    ``call_tool`` is where "all of them" ends: the middleware chain's innermost
    ``call_next`` is ``self.call_tool(..., run_middleware=False)``, dispatched
    through the instance on every call, below every middleware and below the
    extension interceptors. Position in ``server.middleware`` therefore cannot
    matter — including for a middleware the customer adds after ``track()``,
    which a tail middleware of ours would have ended up above.

    The wrapper is a pass-through that records and re-raises, so the same
    exception reaches the same layer it would have untracked. It is also
    entered a second time per call, from the top with ``run_middleware=True``,
    where nothing has raised yet and the outer entry is a no-op.
    """
    state = install_state(server)
    if state is None:
        return []  # already logged
    return ["call_tool"] if tap_method(server, "call_tool", state, "call_tool") else []
