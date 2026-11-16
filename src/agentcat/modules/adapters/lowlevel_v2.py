"""Adapter for the official MCP SDK 2.x lowlevel ``Server``.

It also serves ``mcp.server.mcpserver.MCPServer`` through its
``_lowlevel_server``, so both modern official flavors run one interception
path — the same arrangement ``lowlevel_v1`` has with official FastMCP.

Era-specific knowledge only: the method-keyed ``_request_handlers`` table, the
frozen ``HandlerEntry`` registration record, ``(ctx, params)`` handlers that
return complete result models, snake_case model fields
(``input_schema``/``structured_content``/``is_error``), and the 2026
multi-round-trip result vocabulary. Every decision about what to resolve,
strip, decorate or publish belongs to :mod:`agentcat.modules.callpath` and is
not re-derived here.

Four invariants the wrappers exist to protect:

- **Nothing customer-owned is mutated.** Listed tools are deep-copied before
  the injection pipeline rewrites their schemas in place, and the request
  params are cloned via ``model_copy`` rather than rebuilt — a rebuilt
  ``CallToolRequestParams`` drops ``input_responses`` / ``request_state`` /
  ``_meta``, which severs multi-round-trip continuations.
- **The event records the call as the agent made it.** Raw (unstripped)
  arguments and the customer's undecorated result; the mint-back is wire-only.
- **An intermediate MRTR round is never decorated.** This is the first era
  where a tool can ask the client for more input, so ``input_required`` is real
  behavior here: the round publishes, tagged, but goes back undecorated.
- **The registration seam stays armed.** ``track()`` can run before a single
  ``tools/*`` handler exists, and a customer may replace one afterwards;
  either way the wrapper ends up on top exactly once.

All ``mcp`` imports are function-local so ``import agentcat`` succeeds under
either SDK major.
"""

from __future__ import annotations

import copy
import dataclasses
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
from agentcat.modules.detection import HANDLER_REGISTRATION_NAMES
from agentcat.modules.exceptions import capture_exception
from agentcat.modules.injection import ToolSpec, build_injected_schemas
from agentcat.modules.logging import write_to_log
from agentcat.modules.request_extra import extra_from_request_context
from agentcat.modules.server_info import (
    FEEDBACK_NOTE,
    LIST_REQUEST,
    TOOL_CALL,
    Page,
    PageFetcher,
    ServerInfoSource,
    identity_text,
)
from agentcat.modules.server_info import check as check_server_info
from agentcat.modules.tools import (
    GET_MORE_TOOLS_DESCRIPTION,
    GET_MORE_TOOLS_SCHEMA,
    handle_report_missing,
)
from agentcat.types import AgentCatData

LIST_METHOD = "tools/list"
CALL_METHOD = "tools/call"


# The lists of the server info heartbeat: the key of each one in the body, its
# method, and its field on the result model and on a raw mapping.
_INFO_LISTS = (
    ("tools", LIST_METHOD, "tools", "tools"),
    ("resources", "resources/list", "resources", "resources"),
    (
        "resourceTemplates",
        "resources/templates/list",
        "resource_templates",
        "resourceTemplates",
    ),
    ("prompts", "prompts/list", "prompts", "prompts"),
)


def lists_for_another_request(ctx: Any) -> bool:
    """Whether a ``tools/list`` pass serves an HTTP request for another method.

    On the 2026 Streamable HTTP path mcp reads the tool list through the
    registered handler inside a ``tools/call``, to check the ``Mcp-Param-*``
    headers of the call. That pass gets the HTTP request of the call, and the
    transport has already made sure that its ``Mcp-Method`` header names the
    method of its body. A request with no such header (stdio, the older HTTP
    path) is a client's listing.
    """
    try:
        method = ctx.request.headers.get("mcp-method")
    except Exception:
        return False
    return isinstance(method, str) and method != LIST_METHOD


def _list_params(params_type: Any, cursor: Any) -> Any:
    """The params a client sends for one page of a list.

    A bare-callable registration carries no params type; the SDK's own model
    is what the runner would hand such a handler.
    """
    if params_type is None:
        from mcp.types import PaginatedRequestParams

        params_type = PaginatedRequestParams
    return params_type() if cursor is None else params_type(cursor=cursor)


def _result_field(result: Any, snake: str, camel: str) -> Any:
    """A field of a list result, which is a model or a raw mapping."""
    if isinstance(result, dict):
        return result.get(camel, result.get(snake))
    return getattr(result, snake, None)


def page_fetcher(
    handler: Any,
    params_type: Any,
    ctx: Any,
    snake: str,
    camel: str,
    finish: Any = None,
) -> PageFetcher:
    """Reads one list page by page through its registered handler.

    ``ctx`` is the context of the request that started the heartbeat, so a
    handler that reads the session or the lifespan state gets the real thing.
    ``finish`` turns the handler's result into the one a client receives.
    """

    async def fetch(cursor: Any) -> Page:
        result = await handler(ctx, _list_params(params_type, cursor))
        if finish is not None:
            result = finish(result)
        return (
            list(_result_field(result, snake, camel)),
            _result_field(result, "next_cursor", "nextCursor"),
        )

    return fetch


def list_fetchers(server: Any, ctx: Any) -> dict[str, PageFetcher]:
    """A fetcher for each list this server has a handler for."""
    fetchers: dict[str, PageFetcher] = {}
    for key, method, snake, camel in _INFO_LISTS:
        entry = _entry(server, method)
        if entry is not None:
            fetchers[key] = page_fetcher(
                _handler_of(entry),
                getattr(entry, "params_type", None),
                ctx,
                snake,
                camel,
            )
    return fetchers


def _entry(server: Any, method: str) -> Any:
    """The registration record for ``method``, or None."""
    try:
        return server._request_handlers.get(method)
    except Exception:
        return None


def _handler_of(entry: Any) -> Any:
    """The callable inside a registration record.

    2.0 wraps it in a ``HandlerEntry``; the pre-2.0 development line stored the
    callable directly, and the classifier still accepts that shape.
    """
    return getattr(entry, "handler", entry)


def _registration(entry: Any, handler: Any) -> Any:
    """``entry`` with ``handler`` swapped in, for writing back to the table.

    ``HandlerEntry`` is a frozen dataclass, so the record is rebuilt rather
    than mutated — via ``dataclasses.replace``, which carries every field
    forward by name and so cannot silently drop one upstream adds later (a
    validator, a title, an annotation). Positional reconstruction is the
    fallback for a record that is not a dataclass at all, and a record that is
    just the callable has no params type to carry.
    """
    params_type = getattr(entry, "params_type", None)
    if params_type is None:
        return handler
    if dataclasses.is_dataclass(entry) and not isinstance(entry, type):
        return dataclasses.replace(entry, handler=handler)
    return type(entry)(params_type, handler)


def _legacy_client_info(ctx: Any) -> Any:
    """Handshake-time ``client_info``, captured by the SDK's own connection.

    The last rung of the client identity ladder, supplied lazily so the
    earlier meta rungs never pay for it. On a 2026-era wire there is no
    handshake and the envelope rung above answers first; on a legacy-era
    connection this is the only rung there is.
    """
    try:
        return ctx.session.client_params.client_info
    except Exception:
        return None


def _text_block(text: str) -> Any:
    from mcp.types import TextContent

    return TextContent(type="text", text=text)


def _make_get_more_tools() -> Any:
    from mcp.types import Tool

    # Built from a keyword mapping rather than literal kwargs. `input_schema`
    # and `read_only_hint` are the 2.x field names, and a type-check pass run
    # against the 1.x models — which is exactly what happens in the legacy
    # dependency set, where this whole module is dead code — sees only their
    # camelCase predecessors. Same era bridge `adapters/community.py` uses for
    # its annotations mapping.
    annotations: dict[str, Any] = {"read_only_hint": True}
    fields: dict[str, Any] = {
        "name": GET_MORE_TOOLS_NAME,
        "description": GET_MORE_TOOLS_DESCRIPTION,
        "input_schema": copy.deepcopy(GET_MORE_TOOLS_SCHEMA),
        # Spec defaults assume the worst; declare the honest hint so
        # annotation-aware clients skip the confirmation prompt. Handed over as
        # the mapping itself: pydantic coerces it into whichever annotations
        # model the running SDK declares, and nothing here has to import a type
        # whose availability moved between generations.
        "annotations": annotations,
    }
    return Tool(**fields)


def _mcpserver_behind(server: Any, state: dict[str, Any]) -> Any:
    """The ``MCPServer`` that owns ``server``, found from its own handler.

    ``track()`` supplies the facade when it was handed one, but a customer who
    tracks ``mcpserver._lowlevel_server`` directly hands it only the lowlevel
    object. The registered ``tools/call`` handler is a bound method of the
    MCPServer either way, and the ownership check — that the object's own
    ``_lowlevel_server`` IS this server — is what keeps a customer handler
    bound to some unrelated object with a ``call_tool`` attribute out.
    """
    owner = getattr(state.get(f"orig_{CALL_METHOD}"), "__self__", None)
    if owner is None or getattr(owner, "_lowlevel_server", None) is not server:
        return None
    return owner


def _arm_inner_tap(server: Any, facade: Any, state: dict[str, Any]) -> list[str]:
    """Arm the inner tap on ``MCPServer.call_tool``, if there is an MCPServer.

    ``MCPServer._handle_call_tool`` — the ``tools/call`` handler this adapter
    wraps — catches everything its ``call_tool`` raises and keeps only
    ``str(e)``, so the exception dies one frame below the wrapper. There is no
    method call inside that ``except`` to read ``sys.exc_info()`` from the way
    lowlevel v1 has, so the seam is ``call_tool`` itself: looked up on the
    instance per call, and the innermost thing outside the customer's tool.

    A **bare** lowlevel v2 server needs nothing. This generation lets a
    handler's exception through to the runner, so the adapter's own ``except``
    already holds it live — which is why there is no owner to find there and
    this is a no-op.
    """
    owner = facade if facade is not None else _mcpserver_behind(server, state)
    if owner is None or not callable(getattr(owner, "call_tool", None)):
        return []
    return ["call_tool"] if tap_method(owner, "call_tool", state, "call_tool") else []


def install_lowlevel_v2(server: Any, data: AgentCatData, facade: Any = None) -> None:
    """Wrap ``tools/list`` and ``tools/call`` on a lowlevel v2 server.

    ``server`` is the lowlevel object the detector handed back (a bare
    ``Server``, or an ``MCPServer``'s ``_lowlevel_server``); ``facade`` is the
    ``MCPServer`` that owns it when there is one, which only the inner tap
    needs; ``data`` is the tracking data already stored for it.

    ``initialize`` is deliberately left alone — it cannot be overridden on this
    generation, and there is nothing there v2 wants: the only thing the old 1.x
    override took from it, the handshake ``clientInfo``, the SDK already keeps
    on the connection where the client identity ladder reads it per request.
    """
    state = install_state(server)
    if state is None:
        return  # already logged

    # Whether WE are the one advertising get_more_tools. Stated positively on
    # purpose: it is set at the append site by every pass over the listing
    # (client-facing or rebuild-on-demand), so until a listing has actually
    # advertised our tool this stays False — and a server with no tools/list
    # handler at all, or one whose listing raised, cannot hijack a customer's
    # own get_more_tools by default.
    agentcat_advertises_get_more_tools = False

    def current_data() -> AgentCatData:
        """The tracking data as of this request, so a re-track takes effect."""
        return current_tracking_data(server, data)

    def original(method: str) -> Any:
        """The customer's handler for ``method`` as of right now.

        Read from the shared state on every call rather than captured, so a
        handler the customer re-registers after ``track()`` is the one that
        runs — the wrapper on top of it does not need replacing for that.
        """
        return state.get(f"orig_{method}")

    def advertised_tools(
        result: Any, options: Any, record: bool = True
    ) -> tuple[list[Any], set[int]] | None:
        """Deep copies of the listed tools, plus get_more_tools when enabled.

        None when the handler returned something with no tool list to inject
        into (a raw dict, an error shape), which the caller serves untouched.
        Copies because the injection pipeline rewrites schemas in place. A
        tool whose copy fails is carried through VERBATIM — the customer's
        original object, never handed to the pipeline so it is never mutated
        — and its id() lands in the returned skip set so the callers exclude
        it from injection; one uncopyable tool must not take down the whole
        listing (or, on the rebuild path, brand the call's arguments).

        ``record=False`` leaves the ownership flag alone: the server info
        heartbeat reads a listing without changing how a call is served.
        """
        nonlocal agentcat_advertises_get_more_tools
        listed = getattr(result, "tools", None)
        if not isinstance(listed, list):
            return None
        tools: list[Any] = []
        skipped: set[int] = set()
        for tool in listed:
            try:
                tools.append(tool.model_copy(deep=True))
            except Exception as e:
                write_to_log(
                    "Warning: could not copy tool "
                    f"'{getattr(tool, 'name', '<unnamed>')}' for injection; "
                    f"serving it verbatim without handle parameters - {e}"
                )
                tools.append(tool)
                skipped.add(id(tool))
        customer_owns_get_more_tools = any(
            getattr(tool, "name", None) == GET_MORE_TOOLS_NAME for tool in tools
        )
        advertises = options.enable_report_missing and not customer_owns_get_more_tools
        if record:
            agentcat_advertises_get_more_tools = advertises
        # Appended before injection so it receives handle parameters too; the
        # context pass skips it by name, so early placement cannot double-inject.
        if advertises:
            tools.append(_make_get_more_tools())
        return tools, skipped

    def specs_for(tools: list[Any], skipped: set[int]) -> list[ToolSpec | None]:
        """Specs aligned index-for-index with `tools`; None for skipped ones."""
        return [
            None
            if id(tool) in skipped
            else ToolSpec(
                tool.name, tool.input_schema, getattr(tool, "output_schema", None)
            )
            for tool in tools
        ]

    def empty_list_params() -> Any:
        """A default-constructed params model for a listing we ask for.

        The registered params type is all-optional (``PaginatedRequestParams``),
        which is what the runner would hand a handler for a request that
        carried none — so a customer handler reading ``params.cursor`` sees the
        same thing it always does. A bare-callable registration carries no
        params_type; the real model is the fallback then, NOT None — a
        customer list handler that dereferences its params must not crash the
        rebuild (and with it, the call's argument strip).
        """
        params_type = getattr(_entry(server, LIST_METHOD), "params_type", None)
        if params_type is None:
            try:
                from mcp.types import PaginatedRequestParams

                return PaginatedRequestParams()
            except Exception:
                return None
        try:
            return params_type()
        except Exception:
            return None

    def injected_listing(result: Any, record: bool = True) -> Any:
        """The listing a client receives for the customer's ``result``.

        ``record=False`` builds it without writing the registries a later call
        strips by, which is what the server info heartbeat needs: it reads
        every page, and a page must not replace what a client's listing wrote.
        """
        tracking = current_data()
        try:
            advertised = advertised_tools(result, tracking.options, record)
            if advertised is None:
                return result
            tools, skipped = advertised
            specs = specs_for(tools, skipped)
            injected = build_injected_schemas(
                [spec for spec in specs if spec is not None],
                tracking.options,
                tracking.reported_conflicts,
            )
            if record:
                tracking.injected_params_registry = injected.injected_params
                tracking.output_injection_registry = injected.output_injected
                # Union, never replace: membership only grows, and a concurrent
                # listing on another instance may already have recorded a tool
                # this one did not see.
                tracking.declared_session_params |= injected.declared_session_params
            for tool, spec in zip(tools, specs, strict=True):
                if spec is None:
                    continue  # uncopyable tool: served verbatim, never mutated
                tool.input_schema = spec.input_schema
                if spec.output_schema is not None:
                    tool.output_schema = spec.output_schema
            # A copy rather than a mutation: next_cursor, cache hints and any
            # other field the customer's handler set are carried over, and the
            # result object they still hold is left alone.
            return result.model_copy(update={"tools": tools})
        except Exception as e:
            write_to_log(
                "Warning: tools/list injection failed, serving the customer's "
                f"unmodified list - {e}"
            )
            return result

    def server_info_source(ctx: Any) -> ServerInfoSource:
        """What the heartbeat reads from this server, as of right now."""
        lists = list_fetchers(server, ctx)
        list_handler = original(LIST_METHOD)
        if list_handler is None:
            lists.pop("tools", None)
        else:
            # The table holds this adapter's wrapper. The heartbeat goes to the
            # customer's handler and injects without recording.
            lists["tools"] = page_fetcher(
                list_handler,
                getattr(_entry(server, LIST_METHOD), "params_type", None),
                ctx,
                "tools",
                "tools",
                finish=lambda result: injected_listing(result, record=False),
            )
        return ServerInfoSource(
            title=identity_text(server, "title"),
            instructions=identity_text(server, "instructions"),
            lists=lists,
        )

    def check_after(ctx: Any, params: Any, is_list: bool) -> None:
        """Run the heartbeat check for a request this adapter has served.

        Called from a ``finally``, so nothing may leave it: an error here would
        replace the result of the request.
        """
        try:
            if is_list:
                if lists_for_another_request(ctx):
                    return
                kind = LIST_REQUEST
            elif (
                getattr(params, "name", None) == GET_MORE_TOOLS_NAME
                and agentcat_advertises_get_more_tools
            ):
                kind = FEEDBACK_NOTE
            else:
                kind = TOOL_CALL
            check_server_info(current_data(), kind, lambda: server_info_source(ctx))
        except Exception:
            return

    async def wrapped_list(ctx: Any, params: Any) -> Any:
        try:
            return injected_listing(await original(LIST_METHOD)(ctx, params))
        finally:
            check_after(ctx, params, is_list=True)

    async def wrapped_call(ctx: Any, params: Any) -> Any:
        try:
            return await tracked_call(ctx, params)
        finally:
            check_after(ctx, params, is_list=False)

    async def tracked_call(ctx: Any, params: Any) -> Any:
        tracking = current_data()
        options = tracking.options
        name = getattr(params, "name", None) or "Unknown Tool"
        raw_arguments = dict(getattr(params, "arguments", None) or {})

        async def rebuild() -> list[ToolSpec]:
            """The list source for registry rebuild-on-demand (changelog 6.3).

            Driven off this request's own context, so a customer handler that
            reads the session or the lifespan state gets the real thing.
            """
            list_handler = original(LIST_METHOD)
            if list_handler is None:
                return []
            listed = await list_handler(ctx, empty_list_params())
            advertised = advertised_tools(listed, current_data().options)
            if advertised is None:
                return []
            tools, skipped = advertised
            return [spec for spec in specs_for(tools, skipped) if spec is not None]

        # Runs first: on an instance that never served a listing this rebuilds
        # the registries, which is also what settles whether the customer ships
        # a get_more_tools of their own.
        stripped = await get_stripped_arguments(
            tracking, options, name, raw_arguments, rebuild
        )

        async def run_customer() -> Any:
            """Dispatch to the customer's handler on a clone carrying only
            their arguments. ``model_copy`` keeps ``input_responses`` /
            ``request_state`` / ``_meta``, which a rebuilt params model loses."""
            return await original(CALL_METHOD)(
                ctx, params.model_copy(update={"arguments": stripped})
            )

        # Answer get_more_tools ourselves only when WE are the one advertising
        # it. A customer who happens to name a tool `get_more_tools` keeps it:
        # silently swapping their handler for our canned reply would alter tool
        # behavior, which nothing AgentCat does may do (spec §12).
        serve_report_missing = (
            name == GET_MORE_TOOLS_NAME and agentcat_advertises_get_more_tools
        )

        # Tracing off: still strip what we injected, so the customer's tool runs
        # exactly as it would untracked — then get out of the way. No handle
        # resolution, no mint-back (there is no session_id parameter to echo), and
        # no event. get_more_tools still answers (changelog 6.6).
        if not options.enable_tracing:
            if serve_report_missing:
                return await handle_report_missing(stripped)
            return await run_customer()

        try:
            # Resolved fresh every round: the ResolvedCall carries this round's
            # params/context for the customer's tag and property callbacks, so
            # it must never be cached across MRTR rounds.
            resolved = await resolve_call(
                tracking,
                name,
                raw_arguments,
                params,
                ctx,
                meta_sources=[getattr(ctx, "meta", None)],
                legacy_client=lambda: _legacy_client_info(ctx),
                protocol_fallback=getattr(ctx, "protocol_version", None),
            )
        except Exception as e:
            # Belt and braces at the customer boundary: the resolvers are all
            # documented not to raise, but a tool call must never fail because
            # analytics did. Degrade to an untraced call.
            write_to_log(
                f"Warning: AgentCat resolution failed for tool '{name}', running "
                f"it untraced - {e}"
            )
            if serve_report_missing:
                return await handle_report_missing(stripped)
            return await run_customer()

        started = now_ms()

        # The tap's slot is open for exactly the customer's handler and the
        # publish that reads it, and closes on every exit path including the
        # raise below.
        with inner_tap() as tap:
            try:
                if serve_report_missing:
                    result = await handle_report_missing(stripped)
                else:
                    result = await run_customer()
            except Exception as e:
                # Unlike 1.x, this generation lets a bare handler's exception
                # through to the runner, so this path holds the live exception
                # with its type and traceback intact.
                await _publish(
                    tracking,
                    resolved,
                    name,
                    raw_arguments,
                    ctx,
                    response=None,
                    is_error=True,
                    error=capture_exception(e),
                    started=started,
                    mrtr=None,
                )
                raise

            mrtr = detect_mrtr(
                getattr(result, "result_type", None),
                getattr(params, "input_responses", None) is not None,
                getattr(params, "request_state", None) is not None,
            )
            is_error = bool(getattr(result, "is_error", False))
            await _publish(
                tracking,
                resolved,
                name,
                raw_arguments,
                ctx,
                response=response_payload(result),
                is_error=is_error,
                # An `MCPServer` handler flattens the exception before this
                # wrapper sees it, so `tap.error` reaches past the result for
                # the object the inner tap recorded — and falls back to that
                # result's message when nothing local raised at all.
                error=tap.error(result) if is_error else None,
                started=started,
                mrtr=mrtr,
            )

        # An intermediate multi-round-trip round is never decorated: only the
        # completing round carries the mint-back (changelog 6.4). It has no
        # content to append to and no structured payload to mirror into, and
        # even if it did the handle belongs on the round that finishes.
        if mrtr == "input_required":
            return result
        return _decorated(result, resolved, name, tracking)

    async def _publish(
        tracking: AgentCatData,
        resolved: ResolvedCall,
        name: str,
        raw_arguments: dict[str, Any],
        ctx: Any,
        response: dict[str, Any] | None,
        is_error: bool,
        error: Any,
        started: int,
        mrtr: str | None,
    ) -> None:
        await publish_tool_call_event(
            server,
            tracking,
            resolved,
            name,
            raw_arguments,
            response=response,
            is_error=is_error,
            error=error,
            duration_ms=now_ms() - started,
            mrtr=mrtr,
            extra_params=extra_from_request_context(ctx),
        )

    def _decorated(
        result: Any, resolved: ResolvedCall, name: str, tracking: AgentCatData
    ) -> Any:
        """The wire result with the mint-back appended, or the original."""
        if not hasattr(result, "model_copy"):
            # A handler that returned a raw dict is serialized as-is by the
            # runner; there is no model to copy and nothing safe to edit. The
            # call is still tracked, but the agent never sees the mint-back and
            # so mints a fresh task on every call — say so, or that shows up as
            # an unexplained pile of one-call tasks.
            write_to_log(
                f"Warning: tool '{name}' returned a raw mapping rather than a "
                "result model, so the session_id mint-back cannot be attached; "
                "every call to it will start a new task"
            )
            return result
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
            getattr(result, "structured_content", None),
            resolved.resolution,
            name,
            tracking.output_injection_registry,
        )
        if mirrored is not None:
            update["structured_content"] = mirrored
        # A copy, never an in-place edit: the event above still references the
        # customer's own result object.
        return result.model_copy(update=update) if update else result

    def swap(method: str) -> bool:
        """Put our wrapper on top of whatever is registered for ``method``.

        Returns whether anything was wrapped. Never raises: a table shape we
        cannot rewrite leaves the customer's handler exactly where it was,
        which costs analytics for that method and nothing else.
        """
        entry = _entry(server, method)
        if entry is None:
            return False
        try:
            current = _handler_of(entry)
            # Ours from an earlier pass? Then the customer's handler is the one
            # we recorded, not the wrapper sitting in the table.
            customer = original(method) if current is state.get(method) else current
            if customer is None:
                return False
            wrapper = wrapped_list if method == LIST_METHOD else wrapped_call
            state[f"orig_{method}"] = customer
            state[method] = wrapper
            server._request_handlers[method] = _registration(entry, wrapper)
            return True
        except Exception as e:
            write_to_log(
                f"Warning: could not wrap '{method}' on this server; it stays "
                f"untracked - {e}"
            )
            return False

    def rearm_seam(name: str, already_patched: list[Any]) -> bool:
        """Patch one registration seam so later registrations land wrapped.

        Without this, ``track()`` on a server whose ``tools/*`` handlers are
        registered afterwards installs nothing at all, and a customer who
        replaces a handler later silently drops out of tracking. The patch is
        an instance attribute shadowing the class method, and it unstacks
        itself on a re-``track()`` the same way the handlers do.

        ``already_patched`` collects the underlying functions patched so far.
        A build whose public name is a bare alias for the private one resolves
        to the same function twice; patching it twice would register twice.
        """
        current = getattr(server, name, None)
        if not callable(current):
            return False
        add = (
            state.get(f"orig_seam_{name}")
            if current is state.get(f"seam_{name}")
            else current
        )
        if add is None:
            return False
        underlying = getattr(add, "__func__", add)
        if any(patched is underlying for patched in already_patched):
            return False

        def rearming_add(*args: Any, **kwargs: Any) -> Any:
            result = add(*args, **kwargs)
            method = args[0] if args else kwargs.get("method")
            if method in (LIST_METHOD, CALL_METHOD):
                swap(method)
            return result

        state[f"orig_seam_{name}"] = add
        state[f"seam_{name}"] = rearming_add
        try:
            setattr(server, name, rearming_add)
        except Exception as e:
            write_to_log(
                f"Warning: could not re-arm '{name}'; a tools handler "
                f"registered after track() through it will not be tracked - {e}"
            )
            return False
        already_patched.append(underlying)
        return True

    def rearm() -> list[str]:
        """Patch EVERY registration seam this build exposes.

        Not just the first one found: the classifier accepts both spellings
        because the method has been public and private at different points on
        the 2.x line, and the natural refactor shape — a public wrapper
        delegating to a private one — exposes both at once. Patching only the
        public name there would silently drop re-arm for anyone who registers
        through the private one.
        """
        patched: list[Any] = []
        return [
            name for name in HANDLER_REGISTRATION_NAMES if rearm_seam(name, patched)
        ]

    wrapped = [method for method in (LIST_METHOD, CALL_METHOD) if swap(method)]
    seams = rearm()
    tapped_seams = _arm_inner_tap(server, facade, state)
    write_to_log(
        f"Installed lowlevel-v2 adapter on server {id(server)} "
        f"(wrapped={wrapped or 'none yet'}, re-armed={seams or 'no'}, "
        f"inner-tap={tapped_seams or 'none'})"
    )
