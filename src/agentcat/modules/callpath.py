"""Shared per-call orchestration for `tools/call`.

Every adapter — lowlevel v1, lowlevel v2, community FastMCP — wraps the
customer's handler with the same steps: resolve (handles, actor, client),
strip the parameters we injected, run, publish exactly one event, decorate the
wire result. This module is that shared middle. The primitives it composes
live in handles.py / injection.py / client_identity.py and are never
re-derived here.

Engine code: nothing version-specific from `mcp`/`fastmcp` is imported, so it
loads identically under mcp 1.x and 2.x. Nothing here may raise into the
customer's server, and nothing here mutates a customer object — the published
event carries the RAW arguments and the UNDECORATED result (mint-back is
wire-only), `get_stripped_arguments` returns a new dict and `decorate_content`
a new list.

Cross-SDK contract: 2026-07-28-cross-sdk-changelog.md §3.4, §6.2-§6.5; TS
reference src/engine/callWrap.ts.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from agentcat.modules import event_queue
from agentcat.modules.client_identity import (
    ClientIdentity,
    resolve_client_identity,
    resolve_protocol_version,
)
from agentcat.modules.constants import CONTEXT_PARAM
from agentcat.modules.handles import (
    HandleResolution,
    build_handle_tags,
    build_mint_back_text,
    build_structured_mint_back,
    mirror_into_structured_content,
    resolve_handles,
)
from agentcat.modules.identify import resolve_identity
from agentcat.modules.injection import (
    ToolSpec,
    build_injected_schemas,
    injected_parameter_names,
    strip_injected_arguments,
)
from agentcat.modules.internal import attach_event_metadata
from agentcat.modules.logging import write_to_log
from agentcat.types import (
    AgentCatData,
    AgentCatOptions,
    ErrorData,
    EventType,
    UnredactedEvent,
    UserIdentity,
)

# A failed call whose adapter could make nothing of the failure. Same shape as
# every other error payload so consumers never have to branch on presence.
_UNKNOWN_ERROR: ErrorData = {
    "message": "Unknown error",
    "type": None,
    "platform": "python",
}


@dataclass
class ResolvedCall:
    """Everything resolved about one tool call before its handler runs."""

    resolution: HandleResolution
    actor: UserIdentity | None
    client: ClientIdentity
    protocol_version: str | None
    intent: str | None
    # The originating request/extra, carried so the customer's event_tags and
    # event_properties callbacks receive the same pair `identify` got — the
    # documented contract of `internal.attach_event_metadata`, which
    # publish_tool_call_event runs long after the adapter's frame is gone.
    # Consequently a ResolvedCall is single-round state: an adapter must
    # resolve afresh on each MRTR round, never cache one across rounds, or
    # round N's callbacks would be handed round 1's request.
    request: Any = None
    extra: Any = None


async def resolve_call(
    data: AgentCatData,
    tool_name: str,
    raw_arguments: dict[str, Any],
    request: Any,
    extra: Any,
    meta_sources: list[Any],
    legacy_client: Callable[[], Any | None],
    protocol_fallback: str | None = None,
) -> ResolvedCall:
    """Resolve handles, actor, client and intent. Publishes nothing.

    None of the four resolvers raises: a `resolve_session_id` hook that blows up
    mints silently, a broken `identify` yields an anonymous call, and the
    client ladder floors at an empty identity. `tool_name` is taken so every
    adapter calls this with one shape; intent capture is unconditional.

    MUST run after `get_stripped_arguments` — every adapter does — because the
    registry it reads is what that call populates (and rebuilds on demand). It
    is the same registry, so the handles read here are exactly the parameters
    stripped there: a `session_id` the customer's own schema declared is neither
    taken from their handler nor consumed as ours.

    A tool absent from `declared_session_params` is ours — including one this
    instance never listed. That is the common case on stateless HTTP and it
    degrades safely: a customer's foreign value in that window is classified
    `invalid` rather than `foreign`, and both publish sessionless.
    """
    resolution = await resolve_handles(
        raw_arguments,
        data.options,
        data.project_id,
        request,
        extra,
        injected_parameter_names(
            tool_name, data.injected_params_registry, raw_arguments, data.options
        ),
        tool_name not in data.declared_session_params,
    )
    actor = await resolve_identity(data, request, extra)
    client = resolve_client_identity(meta_sources, legacy_client)
    protocol_version = resolve_protocol_version(meta_sources, protocol_fallback)
    # Read from the RAW arguments (pre-strip): the event records what the agent
    # sent, and a non-string `context` is not an explanation.
    context_value = raw_arguments.get(CONTEXT_PARAM)
    return ResolvedCall(
        resolution=resolution,
        actor=actor,
        client=client,
        protocol_version=protocol_version,
        intent=context_value if isinstance(context_value, str) else None,
        request=request,
        extra=extra,
    )


async def get_stripped_arguments(
    data: AgentCatData,
    options: AgentCatOptions,
    tool_name: str,
    raw_arguments: dict[str, Any],
    rebuild: Callable[[], Awaitable[list[ToolSpec]]] | None,
) -> dict[str, Any]:
    """A NEW argument dict with only the params AgentCat injected removed.

    Registry-first (§6.2). A call that lands on an instance which never served
    `tools/list` rebuilds the registries on demand from the adapter's list
    source (§6.3); the injection pipeline is deterministic, so a rebuilt
    registry matches what any listing instance advertised. Only a failed
    rebuild falls back to the shape+config-aware strip (see
    `injected_parameter_names`): a name is removed only when the enabled
    options would have injected it AND, for `session_id`, the value matches
    our minted shape — a customer-declared parameter rides through to their
    handler. The fallback also clears the output-injection registry, so the
    structured mirror stops gating on knowledge we no longer have (§3.4b).
    """
    registry = data.injected_params_registry
    if registry is None and rebuild is not None:
        try:
            result = build_injected_schemas(
                await rebuild(), options, data.reported_conflicts
            )
        except Exception as e:
            write_to_log(
                "Warning: injection registry rebuild-on-demand failed for tool "
                f"'{tool_name}', falling back to heuristic strip - {e}"
            )
            # Only the mirror gate is cleared. `injected_params_registry` is
            # left alone: it is already None on this path, and assigning would
            # destroy a good registry a concurrent call had just stored while
            # this one was awaiting — after which every later call would
            # heuristic-strip and eat customer `context` parameters.
            data.output_injection_registry = None
        else:
            registry = result.injected_params
            data.injected_params_registry = result.injected_params
            data.output_injection_registry = result.output_injected
            data.declared_session_params |= result.declared_session_params
            write_to_log(
                "Rebuilt injection registries on demand "
                "(tools/call before tools/list on this instance)"
            )
    return strip_injected_arguments(tool_name, raw_arguments, registry, options)


def detect_mrtr(
    result_type: str | None,
    has_input_responses: bool,
    has_request_state: bool = False,
) -> str | None:
    """Classify one round of a multi round-trip tool call (§6.4).

    `input_required` wins: an intermediate round that is itself a continuation
    is still intermediate, and only a completing round carries the mint-back.

    A continuation is EITHER shape the SEP-2322 driver can send. It answers an
    intermediate round that carried `input_requests` with the collected
    `inputResponses`, but an intermediate round that carried only
    `requestState` — a tool asking to be resumed rather than asking the client
    a question — is retried after a backoff with **no** responses at all
    (`mcp/client/_input_required.py::run_input_required_driver`). Keying the
    tag on `inputResponses` alone left that second shape untagged, which is the
    shape FastMCP 4's own `InputRequiredResult(request_state=...)` produces.
    `requestState` rides only on a round that is answering an earlier one — a
    first round never carries it — so it is a sound continuation witness on its
    own.
    """
    if result_type == "input_required":
        return "input_required"
    if has_input_responses or has_request_state:
        return "continuation"
    return None


def decorate_content(
    content: list[Any] | None,
    res: HandleResolution,
    make_text_block: Callable[[str], Any],
) -> list[Any] | None:
    """The trailing mint-back block, or None to leave the result untouched.

    Error state is deliberately not an input: the retry after a failure has to
    carry the same session, so `isError` results decorate on identical terms
    (§3.4a). Whether there is anything to say at all stays the single ruling of
    `build_mint_back_text` — a session minted on this call, or a supplied one
    this server never issued; never in hook mode, and never for a parameter
    AgentCat did not inject.
    """
    text = build_mint_back_text(res)
    if text is None or not isinstance(content, list):
        return None
    return [*content, make_text_block(text)]


def structured_mirror(
    sc: Any,
    res: HandleResolution,
    tool_name: str,
    output_registry: set[str] | None,
) -> Any | None:
    """Handle state mirrored into `structuredContent`, or None (§3.4b).

    Gated on the output-injection registry: mirroring a key the tool's own
    `outputSchema` never declared fails the customer's entire result on a
    schema-validating client. No registry at all means the rebuild failed —
    mirror anyway, since no schema we know about can be in play.
    """
    if output_registry is not None and tool_name not in output_registry:
        return None
    mint = build_structured_mint_back(res)
    if mint is None:
        return None
    return mirror_into_structured_content(sc, mint)


async def publish_tool_call_event(
    server_key: Any,
    data: AgentCatData,
    rc: ResolvedCall,
    tool_name: str,
    raw_arguments: dict[str, Any],
    response: Any,
    is_error: bool,
    error: ErrorData | None,
    duration_ms: int | None,
    mrtr: str | None,
    extra_params: dict[str, Any] | None,
) -> None:
    """Publish the one event a v2 tool call produces. Never raises.

    The event records the call as the agent made it: RAW arguments (handles and
    `context` included) and the customer's undecorated result. SDK tags merge
    over the customer's, so they win on collision and ride outside the 50-tag
    customer cap `validate_tags` enforces (§6.5).

    `error` is whatever `modules.exceptions.capture_exception` made of the
    failure — the adapter decides, because only it knows whether it holds a
    live exception or the `isError` result the SDK already flattened it into.
    A failure always records an error object, never a bare `is_error` flag.
    """
    try:
        now = datetime.now(timezone.utc)
        event = UnredactedEvent(
            # "" is sessionless (invalid/foreign); the wire carries null.
            session_id=rc.resolution.session_id or None,
            # Events timestamp when the call STARTED; the adapter measured how
            # long ago that was.
            timestamp=now - timedelta(milliseconds=duration_ms) if duration_ms else now,
            event_type=EventType.MCP_TOOLS_CALL.value,
            resource_name=tool_name,
            user_intent=rc.intent,
            parameters={"arguments": raw_arguments, **(extra_params or {})},
            response=response,
            is_error=is_error,
            error=(error or _UNKNOWN_ERROR) if is_error else None,
            duration=duration_ms,
            client_name=rc.client.name,
            client_version=rc.client.version,
            identify_actor_given_id=rc.actor.user_id if rc.actor else None,
            identify_actor_name=rc.actor.user_name if rc.actor else None,
            identify_data=rc.actor.user_data if rc.actor else None,
        )
        await attach_event_metadata(event, data, rc.request, rc.extra)
        # SDK tags last so they win, and after validate_tags so they are exempt
        # from the customer cap.
        event.tags = {
            **(event.tags or {}),
            **build_handle_tags(rc.resolution, rc.protocol_version, mrtr),
        }
        event_queue.publish_event(server_key, event)
    except Exception as e:
        write_to_log(
            f"Warning: failed to publish tools/call event for '{tool_name}' - {e}"
        )
