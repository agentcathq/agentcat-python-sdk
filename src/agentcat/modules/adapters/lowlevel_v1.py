"""Adapter for the official MCP SDK 1.x lowlevel ``Server``.

It also serves ``mcp.server.fastmcp.FastMCP`` through its ``_mcp_server``, so
both official flavors run one interception path instead of the v1 split
between handler overrides and tool-manager monkey patches.

Era-specific knowledge only: the type-keyed ``request_handlers`` dict, the
``ServerResult`` root wrapper, and camelCase model fields
(``inputSchema``/``structuredContent``/``isError``). Every decision about what
to resolve, strip, decorate or publish belongs to
:mod:`agentcat.modules.callpath` and is not re-derived here.

Two invariants the wrappers exist to protect:

- **Nothing customer-owned is mutated.** Listed tools are deep-copied before
  the injection pipeline rewrites their schemas in place, and the request is
  cloned before its arguments are stripped — v1 popped ``context`` off the
  caller's shared dict, which corrupted concurrent retries.
- **The event records the call as the agent made it.** Raw (unstripped)
  arguments and the customer's undecorated result; the mint-back is wire-only.

All ``mcp`` imports are function-local so ``import agentcat`` succeeds under
either SDK major.
"""

from __future__ import annotations

import copy
from typing import Any

from agentcat.modules.adapters._common import (
    current_tracking_data,
    install_state,
    now_ms,
    response_payload,
)
from agentcat.modules.adapters._inner_tap import (
    inner_tap,
    probing,
    tap_method,
)
from agentcat.modules.logging import write_to_log
from agentcat.modules.request_extra import extra_from_request_context
from agentcat.types import AgentCatData

# The two handlers this adapter wraps, keyed by the name of the ``Server``
# decorator that registers each one. Patching those decorators is how a
# handler the customer registers AFTER ``track()`` still ends up wrapped: on
# this generation registration writes straight into ``request_handlers``, so
# the decorator is the only seam there is.
_LIST = "list_tools"
_CALL = "call_tool"


def _safe_request_context(server: Any) -> Any:
    """The in-flight request context, or None outside a request.

    On a lowlevel v1 ``Server`` this is a property that raises when no request
    is active, so it is never read without a guard.
    """
    try:
        return server.request_context
    except Exception:
        return None


def _meta_extras(request: Any) -> Any:
    """The request's ``_meta`` object, where per-request client identity rides."""
    try:
        return getattr(getattr(request, "params", None), "meta", None)
    except Exception:
        return None


def _legacy_client_info(ctx: Any) -> Any:
    """Initialize-time ``clientInfo``, captured by the SDK's own ServerSession.

    The last rung of the client identity ladder, supplied lazily so the
    earlier meta rungs never pay for it.
    """
    try:
        return ctx.session.client_params.clientInfo
    except Exception:
        return None


def _arm_inner_tap(server: Any, facade: Any, state: dict[str, Any]) -> list[str]:
    """Arm the inner tap wherever this generation lets an exception be seen.

    This era has one interception point but two server shapes behind it, and
    they need different seams:

    - **The lowlevel error-result factory**, for every shape. The handler
      ``Server.call_tool()`` builds catches everything and keeps only
      ``str(e)`` — but it builds the result by calling
      ``self._make_error_result(...)`` from *inside* that ``except``, where
      ``sys.exc_info()`` still holds the live exception. Reading it there costs
      the customer nothing: no wrapper frame in their traceback, no re-raise,
      and the factory's own result is returned untouched. On a bare lowlevel
      server it is also the only seam there is, and the exception it finds is
      the customer handler's own.

      The SDK calls that factory from three places, and only one of them
      surfaces the exception's own message; `probing` records only when the
      message it is handed IS ``str(exc)``, which is exactly that one. The
      schema-validation sites keep the one-line wire text they always had
      rather than a multi-line ``jsonschema`` dump of the schema and the
      offending value.
    - **Official FastMCP's tool manager**, for the FastMCP shape. There the
      customer's tool is two layers below the handler and its exception arrives
      already wrapped in a ``ToolError``, whose traceback stops at the wrapper;
      the tool manager is the innermost seam outside the tool itself.
      ``FastMCP.call_tool`` cannot be used instead — the SDK binds it into the
      handler's closure at registration, so replacing the attribute afterwards
      would be ignored — while ``self._tool_manager.call_tool`` is looked up
      per call.

    Both are armed when both exist, and the order does not matter here: the
    tap is last-write-wins, and the second write is the same exception object
    as the first. The tool manager's seam records what it caught and re-raises
    it; the factory then reads `sys.exc_info()` from the ``except`` that same
    exception is propagating through, and `capture_in_flight` only records when
    the surfaced message IS ``str(exc)`` — so what it re-records is the object
    already there.
    """
    armed: list[str] = []
    # `probing` does not await what it wraps, so the precondition is only that
    # the factory is callable at all — not that it is a coroutine function.
    if tap_method(
        server,
        "_make_error_result",
        state,
        "error_result",
        wrap=probing,
        accepts=callable,
    ):
        armed.append("_make_error_result")
    manager = getattr(facade, "_tool_manager", None)
    if manager is not None and tap_method(manager, "call_tool", state, "tool_manager"):
        armed.append("_tool_manager.call_tool")
    return armed


def install_lowlevel_v1(server: Any, data: AgentCatData, facade: Any = None) -> None:
    """Wrap ``tools/list`` and ``tools/call`` on a lowlevel v1 server.

    ``server`` is the lowlevel object the detector handed back (a bare
    ``Server``, or a FastMCP's ``_mcp_server``); ``facade`` is the high-level
    object that owns it when there is one, which only the inner tap needs;
    ``data`` is the tracking data already stored for it. Idempotent: calling
    ``track()`` again re-wraps the same originals with fresh closures rather
    than stacking a second pass.

    ``track()`` may legitimately run before a single ``@server.call_tool()``
    exists, and a customer may register one afterwards, so the registration
    decorators are patched too — otherwise this adapter would install nothing
    at all on a fresh ``Server()`` and be silently overwritten by the
    customer's own registration.

    ``initialize`` is deliberately left alone. v2 publishes no initialize
    event, and the only thing the old override took from it — the handshake
    ``clientInfo`` — the SDK already keeps on the session, where the client
    identity ladder reads it per request.
    """
    from mcp.types import (
        CallToolRequest,
        ListToolsRequest,
        ServerResult,
        TextContent,
        Tool,
    )

    from agentcat.modules.callpath import (
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
    from agentcat.modules.tools import (
        GET_MORE_TOOLS_DESCRIPTION,
        GET_MORE_TOOLS_SCHEMA,
        handle_report_missing,
    )

    handlers = server.request_handlers
    request_types = {_LIST: ListToolsRequest, _CALL: CallToolRequest}
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

    def original(key: str) -> Any:
        """The customer's handler for ``key`` as of right now.

        Read from the shared state on every call rather than captured, so a
        handler the customer re-registers after ``track()`` is the one that
        runs — the wrapper on top of it does not need replacing for that.
        """
        return state.get(f"orig_{key}")

    def make_get_more_tools() -> Tool:
        # Spec defaults assume the worst; declare the honest hint so
        # annotation-aware clients skip the confirmation prompt. Passed as a
        # plain mapping, not the `ToolAnnotations` model — that model only
        # exists from mcp 1.7, and importing it unconditionally used to make
        # `install_lowlevel_v1` raise on 1.2-1.6, where `track()`'s blanket
        # except turned AgentCat into a silent no-op. `Tool` is extra="allow"
        # on every 1.x, so the mapping is absorbed below 1.7 and coerced into
        # the model at and above it.
        #
        # Typed `Any` for the same reason `adapters/community.py` does it: the
        # field is declared `ToolAnnotations | None`, so a type-check pass
        # rejects the mapping that pydantic accepts at runtime.
        annotations: Any = {"readOnlyHint": True}
        return Tool(
            name=GET_MORE_TOOLS_NAME,
            description=GET_MORE_TOOLS_DESCRIPTION,
            inputSchema=copy.deepcopy(GET_MORE_TOOLS_SCHEMA),
            annotations=annotations,
        )

    def advertised_tools(
        original: Any, options: Any
    ) -> tuple[list[Any], set[int]]:
        """Deep copies of the listed tools, plus get_more_tools when enabled.

        Copies because the injection pipeline rewrites schemas in place and
        FastMCP hands out the very dict its tool manager holds. A tool whose
        copy fails is carried through VERBATIM (the customer's object, never
        given to the pipeline) with its id() in the returned skip set — one
        uncopyable tool must not take down the whole listing.
        """
        nonlocal agentcat_advertises_get_more_tools
        tools: list[Any] = []
        skipped: set[int] = set()
        listed = getattr(getattr(original, "root", None), "tools", None)
        if isinstance(listed, list):
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
        agentcat_advertises_get_more_tools = (
            options.enable_report_missing and not customer_owns_get_more_tools
        )
        # Appended before injection so it receives handle parameters too; the
        # context pass skips it by name, so early placement cannot double-inject.
        if agentcat_advertises_get_more_tools:
            tools.append(make_get_more_tools())
        return tools, skipped

    def specs_for(tools: list[Any], skipped: set[int]) -> list[ToolSpec | None]:
        """Specs aligned index-for-index with `tools`; None for skipped ones."""
        return [
            None
            if id(tool) in skipped
            else ToolSpec(
                tool.name, tool.inputSchema, getattr(tool, "outputSchema", None)
            )
            for tool in tools
        ]

    async def rebuild() -> list[ToolSpec]:
        """The list source for registry rebuild-on-demand (changelog 6.3)."""
        list_handler = original(_LIST)
        if list_handler is None:
            return []
        listed = await list_handler(ListToolsRequest(method="tools/list"))
        tools, skipped = advertised_tools(listed, current_data().options)
        return [spec for spec in specs_for(tools, skipped) if spec is not None]

    async def wrapped_list(request: Any) -> Any:
        listed = await original(_LIST)(request)
        tracking = current_data()
        try:
            tools, skipped = advertised_tools(listed, tracking.options)
            specs = specs_for(tools, skipped)
            injected = build_injected_schemas(
                [spec for spec in specs if spec is not None],
                tracking.options,
                tracking.reported_conflicts,
            )
            tracking.injected_params_registry = injected.injected_params
            tracking.output_injection_registry = injected.output_injected
            # Union, never replace: membership only grows, and a concurrent
            # listing on another instance may already have recorded a tool this
            # one did not see.
            tracking.declared_session_params |= injected.declared_session_params
            for tool, spec in zip(tools, specs, strict=True):
                if spec is None:
                    continue  # uncopyable tool: served verbatim, never mutated
                tool.inputSchema = spec.input_schema
                if spec.output_schema is not None:
                    tool.outputSchema = spec.output_schema
            # A copy of the customer's result rather than a rebuilt one: _meta
            # and any extra fields their handler set survive (Result is
            # extra='allow' on 1.x), exactly like the v2 adapter.
            return ServerResult(listed.root.model_copy(update={"tools": tools}))
        except Exception as e:
            write_to_log(
                "Warning: tools/list injection failed, serving the customer's "
                f"unmodified list - {e}"
            )
            return listed

    async def run_original(request: Any, arguments: dict[str, Any]) -> Any:
        """Dispatch to the customer's handler on a clone carrying only their args."""
        params = request.params.model_copy(update={"arguments": arguments})
        return await original(_CALL)(request.model_copy(update={"params": params}))

    async def wrapped_call(request: Any) -> Any:
        tracking = current_data()
        options = tracking.options
        name = getattr(request.params, "name", None) or "Unknown Tool"
        raw_arguments = dict(getattr(request.params, "arguments", None) or {})

        # Runs first: on an instance that never served a listing this rebuilds
        # the registries, which is also what settles whether the customer ships
        # a get_more_tools of their own.
        stripped = await get_stripped_arguments(
            tracking, options, name, raw_arguments, rebuild
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
                return ServerResult(await handle_report_missing(stripped))
            return await run_original(request, stripped)

        context = _safe_request_context(server)
        try:
            # Resolved fresh every round: the ResolvedCall carries this round's
            # request/extra for the customer's tag and property callbacks.
            #
            # `request.params`, not `request`: the customer-facing hooks
            # (`identify`, `event_tags`, `event_properties`, `resolve_session_id`)
            # take ONE shape on every flavor, and the params model is the only
            # one all four adapters can produce — mcp 2.x hands its handler
            # `(ctx, params)` with no request object anywhere, and community
            # FastMCP's `context.message` is params too. So `request.name` and
            # `request.arguments` here, exactly as everywhere else.
            resolved = await resolve_call(
                tracking,
                name,
                raw_arguments,
                request.params,
                context,
                meta_sources=[_meta_extras(request)],
                legacy_client=lambda: _legacy_client_info(context),
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
                return ServerResult(await handle_report_missing(stripped))
            return await run_original(request, stripped)

        started = now_ms()

        # The tap's slot is open for exactly the customer's handler and the
        # publish that reads it, and closes on every exit path including the
        # raise below.
        with inner_tap() as tap:
            try:
                if serve_report_missing:
                    inner = await handle_report_missing(stripped)
                else:
                    inner = (await run_original(request, stripped)).root
            except Exception as e:
                await publish_tool_call_event(
                    server,
                    tracking,
                    resolved,
                    name,
                    raw_arguments,
                    response=None,
                    is_error=True,
                    # The live exception, so this path keeps its type and
                    # traceback. Better than anything the tap could hold, and
                    # it is the same object when the tap holds one at all.
                    error=capture_exception(e),
                    duration_ms=now_ms() - started,
                    mrtr=None,
                    extra_params=extra_from_request_context(context),
                )
                raise

            mrtr = detect_mrtr(getattr(inner, "resultType", None), False)
            is_error = bool(getattr(inner, "isError", False))
            await publish_tool_call_event(
                server,
                tracking,
                resolved,
                name,
                raw_arguments,
                response=response_payload(inner),
                is_error=is_error,
                # The SDK caught the exception and kept only its message, so
                # `tap.error` reaches past the flattened result for the object
                # the tap recorded — and falls back to that result's message
                # when nothing local raised at all.
                error=tap.error(inner) if is_error else None,
                duration_ms=now_ms() - started,
                mrtr=mrtr,
                extra_params=extra_from_request_context(context),
            )

        # An intermediate multi-round-trip round is never decorated: only the
        # completing round carries the mint-back (changelog 6.4).
        if mrtr == "input_required":
            return ServerResult(inner)

        update: dict[str, Any] = {}
        content = getattr(inner, "content", None)
        decorated = decorate_content(
            list(content) if isinstance(content, list) else None,
            resolved.resolution,
            lambda text: TextContent(type="text", text=text),
        )
        if decorated is not None:
            update["content"] = decorated
        mirrored = structured_mirror(
            getattr(inner, "structuredContent", None),
            resolved.resolution,
            name,
            tracking.output_injection_registry,
        )
        if mirrored is not None:
            update["structuredContent"] = mirrored
        # A copy, never an in-place edit: the event above still references the
        # customer's own result object.
        return ServerResult(inner.model_copy(update=update) if update else inner)

    def swap(key: str) -> bool:
        """Put our wrapper on top of whatever is registered for ``key``.

        Returns whether anything was wrapped. Never raises: a handler table we
        cannot rewrite leaves the customer's handler exactly where it was,
        which costs analytics for that method and nothing else.
        """
        try:
            current = handlers.get(request_types[key])
            # Ours from an earlier pass? Then the customer's handler is the one
            # we recorded, not the wrapper sitting in the table.
            customer = original(key) if current is state.get(key) else current
            if customer is None:
                return False
            wrapper = wrapped_list if key == _LIST else wrapped_call
            state[f"orig_{key}"] = customer
            state[key] = wrapper
            handlers[request_types[key]] = wrapper
            return True
        except Exception as e:
            write_to_log(
                f"Warning: could not wrap '{key}' on this server; it stays "
                f"untracked - {e}"
            )
            return False

    def rearm(key: str) -> bool:
        """Patch the registration decorator so later registrations land wrapped.

        Registration on this generation writes straight into
        ``request_handlers``, so there is no single ``add_request_handler`` to
        patch the way lowlevel v2 has — the decorator that performs the write
        is the seam. The patch is an instance attribute shadowing the class
        method, and it unstacks itself on a re-``track()`` the same way the
        handlers do.
        """
        decorator_key = f"{key}_decorator"
        current = getattr(server, key, None)
        if not callable(current):
            return False
        register = (
            state.get(f"orig_{decorator_key}")
            if current is state.get(decorator_key)
            else current
        )
        if register is None:
            return False

        def rearming(*args: Any, **kwargs: Any) -> Any:
            decorator = register(*args, **kwargs)

            def apply(func: Any) -> Any:
                registered = decorator(func)
                swap(key)
                return registered

            return apply

        state[f"orig_{decorator_key}"] = register
        state[decorator_key] = rearming
        try:
            setattr(server, key, rearming)
        except Exception as e:
            write_to_log(
                f"Warning: could not re-arm '{key}' registration; a handler "
                f"registered after track() will not be tracked - {e}"
            )
            return False
        return True

    wrapped = [key for key in (_LIST, _CALL) if swap(key)]
    rearmed = [key for key in (_LIST, _CALL) if rearm(key)]
    tapped_seams = _arm_inner_tap(server, facade, state)
    write_to_log(
        f"Installed lowlevel-v1 adapter on server {id(server)} "
        f"(wrapped={wrapped or 'none yet'}, re-armed={rearmed or 'no'}, "
        f"inner-tap={tapped_seams or 'none'})"
    )
