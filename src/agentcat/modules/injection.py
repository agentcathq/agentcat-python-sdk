"""Pure schema-injection pipeline: handles, context, and strip registries.

The pipeline is pure and deterministic: (adapter's deep-copied tool schemas,
options) -> schemas mutated in place + registries recording exactly what was
injected, so rebuild-on-demand can reproduce the registries on a fresh
instance that never served tools/list. Mirrors the TS SDK's
handle-injection.ts and context-parameters.ts as composed by
listWrap.buildInjectedList.

Two passes over the tools, in order:

1. Handle pass — session_id/agent_id input params plus the _mcp_instructions
   outputSchema extension. Runs only when at least one handle is injectable
   (prompted-mode tracing -> session_id, agent tracking -> agent_id); when
   neither is, the pass is skipped wholesale and schemas keep their original
   shape (no normalization, additionalProperties untouched).
2. Context pass — the optional context param, independent of the handle
   flags. get_more_tools is exempt (its bespoke context is a real
   parameter); it is NOT exempt from handles, since its calls publish
   events.

Resulting property order: customer params, session_id, agent_id, context.
session_id is never required — omission is the minting signal. agent_id and
context are both appended to required; that is client-side compliance
only, since callWrap tolerates either being absent, and it is the only
enforcement an injected parameter has.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from agentcat.modules.constants import (
    AGENT_ID_PARAM,
    AGENT_ID_PARAM_DESCRIPTION,
    AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE,
    CONTEXT_PARAM,
    GET_MORE_TOOLS_NAME,
    MCP_INSTRUCTIONS_AGENT_ID_DESCRIPTION,
    MCP_INSTRUCTIONS_FIELD_DESCRIPTION,
    MCP_INSTRUCTIONS_KEY,
    MCP_INSTRUCTIONS_SESSION_ID_DESCRIPTION,
    SESSION_ID_PARAM,
    SESSION_ID_PARAM_DESCRIPTION,
)
from agentcat.modules.logging import write_to_log
from agentcat.types import AgentCatOptions

_COMPOSED_KEYS = ("oneOf", "allOf", "anyOf")
_ALL_INJECTABLE = (SESSION_ID_PARAM, AGENT_ID_PARAM, CONTEXT_PARAM)


@dataclass
class ToolSpec:
    """One listed tool as the adapter hands it to the pipeline.

    The schemas are the adapter's deep copies of the customer's originals;
    the pipeline mutates them in place.
    """

    name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


@dataclass
class InjectionResult:
    """What the pipeline injected, keyed for later stripping.

    injected_params has an entry for EVERY tool seen (possibly empty), so
    the strip fallback applies only to tools never seen in any listing.
    output_injected lists tools whose outputSchema gained _mcp_instructions.
    declared_session_params lists tools whose OWN schema declared
    `session_id`; see `build_injected_schemas` for why that is a separate
    signal rather than "absent from injected_params".
    """

    injected_params: dict[str, set[str]]
    output_injected: set[str]
    declared_session_params: set[str] = field(default_factory=set)


def mcp_instructions_schema_property(
    include_session_id: bool, include_agent_id: bool
) -> dict[str, Any]:
    """Build a fresh _mcp_instructions outputSchema fragment.

    Sub-properties mirror the modes — no session_id in hook mode, no agent_id
    when tracking is off — so the copy never references a parameter the
    agent cannot see. `instructions` is unconditional.
    """
    sub_properties: dict[str, Any] = {}
    if include_session_id:
        sub_properties[SESSION_ID_PARAM] = {
            "type": "string",
            "description": MCP_INSTRUCTIONS_SESSION_ID_DESCRIPTION,
        }
    if include_agent_id:
        sub_properties[AGENT_ID_PARAM] = {
            "type": "string",
            "description": MCP_INSTRUCTIONS_AGENT_ID_DESCRIPTION,
        }
    sub_properties["instructions"] = {"type": "string"}
    return {
        "type": "object",
        "description": MCP_INSTRUCTIONS_FIELD_DESCRIPTION,
        "properties": sub_properties,
    }


def _inject_param(
    tool_name: str,
    schema: dict[str, Any],
    name: str,
    description: str,
    required: bool,
    entry: set[str],
) -> None:
    """Add one string param, honoring collisions and the required array."""
    properties = schema.setdefault("properties", {})
    if name in properties:
        write_to_log(
            f"WARN: Tool \"{tool_name}\" already has '{name}' parameter. "
            f"Skipping {name} injection."
        )
        return
    properties[name] = {"type": "string", "description": description}
    if required:
        existing = schema.get("required")
        if isinstance(existing, list):
            if name not in existing:
                existing.append(name)
        else:
            schema["required"] = [name]
    entry.add(name)


def _report_session_conflict(tool_name: str, reported: set[str] | None) -> None:
    """Tell the customer their `session_id` collides, once per tool.

    ERROR, not WARN: unlike the other two collisions, this one costs them
    correlation on that tool entirely. The pipeline reruns on every
    `tools/list`, so an undeduped log would repeat for the life of the
    process. Copy is byte-parallel with the TS SDK's handle-injection.ts.
    """
    if reported is not None:
        if tool_name in reported:
            return
        reported.add(tool_name)
    write_to_log(
        f'ERROR: Tool "{tool_name}" already declares a '
        f"'{SESSION_ID_PARAM}' parameter. AgentCat will not inject its own, "
        "and calls to this tool are published without a session, so they "
        "cannot be correlated. Your parameter is untouched and still reaches "
        "your handler. If you already manage sessions, pass a "
        "resolve_session_id hook to track() — AgentCat will derive its "
        f"session from your identifier and stop injecting {SESSION_ID_PARAM} "
        "entirely."
    )


def _add_handle_parameters(
    tool: ToolSpec,
    inject_session_id: bool,
    inject_agent_id: bool,
    result: InjectionResult,
    reported_conflicts: set[str] | None = None,
) -> None:
    """Inject session_id/agent_id and extend the outputSchema for one tool."""
    schema = tool.input_schema
    entry = result.injected_params[tool.name]
    declares_session_id = SESSION_ID_PARAM in (schema.get("properties") or {})
    if any(key in schema for key in _COMPOSED_KEYS):
        # Injection is skipped, but ownership still has to be recorded: a
        # schema that composes AND declares session_id at its root is the
        # customer's parameter, not ours to read at call time. (Only the root
        # bag is visible here — a session_id nested inside a branch is
        # unreachable, the same limitation the injection itself has.)
        if inject_session_id and declares_session_id:
            result.declared_session_params.add(tool.name)
            _report_session_conflict(tool.name, reported_conflicts)
        write_to_log(
            f'WARN: Tool "{tool.name}" has complex schema (oneOf/allOf/anyOf). '
            "Skipping handle injection."
        )
        return
    if not schema:
        # In-place normalization: the adapter's dict object must survive.
        schema["type"] = "object"
        schema["properties"] = {}
        schema["required"] = []
    schema.setdefault("properties", {})
    if schema.get("additionalProperties") is False:
        del schema["additionalProperties"]

    if inject_session_id:
        if declares_session_id:
            # The customer owns this name on this tool. Record it so call-time
            # resolution never reads their value as an AgentCat handle.
            result.declared_session_params.add(tool.name)
            _report_session_conflict(tool.name, reported_conflicts)
        else:
            _inject_param(
                tool.name,
                schema,
                SESSION_ID_PARAM,
                SESSION_ID_PARAM_DESCRIPTION,
                False,
                entry,
            )
    if inject_agent_id:
        # Hook mode has no session_id param anywhere; never reference one the
        # agent cannot see.
        description = (
            AGENT_ID_PARAM_DESCRIPTION
            if inject_session_id
            else AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE
        )
        _inject_param(tool.name, schema, AGENT_ID_PARAM, description, True, entry)

    _extend_output_schema(tool, inject_session_id, inject_agent_id, result)


def _extend_output_schema(
    tool: ToolSpec,
    inject_session_id: bool,
    inject_agent_id: bool,
    result: InjectionResult,
) -> None:
    """Declare the optional _mcp_instructions property on a plain-object
    outputSchema so schema-validating clients accept the mirrored field.

    Never added to required. Composed schemas have no single properties bag
    to extend and are skipped (mint-back stays content-only), same policy as
    the input side.
    """
    schema = tool.output_schema
    if not isinstance(schema, dict):
        return
    if any(key in schema for key in _COMPOSED_KEYS):
        write_to_log(
            f'WARN: Tool "{tool.name}" has complex outputSchema '
            f"(oneOf/allOf/anyOf). Skipping {MCP_INSTRUCTIONS_KEY} injection; "
            "mint-back stays content-only for this tool."
        )
        return
    properties = schema.setdefault("properties", {})
    if MCP_INSTRUCTIONS_KEY in properties:
        write_to_log(
            f'WARN: Tool "{tool.name}" already declares '
            f"'{MCP_INSTRUCTIONS_KEY}' in outputSchema. Skipping injection."
        )
        return
    properties[MCP_INSTRUCTIONS_KEY] = mcp_instructions_schema_property(
        inject_session_id, inject_agent_id
    )
    result.output_injected.add(tool.name)


def _add_context_parameter(tool: ToolSpec, description: str, entry: set[str]) -> None:
    """Inject the context param for one tool, appended to `required`.

    Required is the existing behavior — both v1 paths appended it
    (`overrides/official/monkey_patch.py` and the retired
    `modules/context_parameters.py`, on `bb1e9bc`) and the TS SDK still does
    (`context-parameters.ts`) — and it is the only enforcement
    there is. Nothing server-side rejects a call that omits `context`; a
    schema-validating client refusing to send one is the whole mechanism, the
    same argument the migration guide makes for `agent_id`. Optional `context`
    means agents quietly stop supplying it and `user_intent` coverage decays
    with nothing to show for it.

    `session_id` is the deliberate exception (never required): omitting it is how
    an agent signals "mint me one".
    """
    schema = tool.input_schema
    properties = schema.get("properties")
    if isinstance(properties, dict) and CONTEXT_PARAM in properties:
        write_to_log(
            f"WARN: Tool \"{tool.name}\" already has '{CONTEXT_PARAM}' "
            "parameter. Skipping context injection."
        )
        return
    if any(key in schema for key in _COMPOSED_KEYS):
        write_to_log(
            f'WARN: Tool "{tool.name}" has complex schema (oneOf/allOf/anyOf). '
            "Skipping context injection."
        )
        return
    # TS-faithful (context-parameters.ts): the context injector drops
    # additionalProperties: false itself, so a schema the (skipped) handle
    # pass never normalized still admits the injected param.
    if schema.get("additionalProperties") is False:
        del schema["additionalProperties"]
    _inject_param(tool.name, schema, CONTEXT_PARAM, description, True, entry)


def build_injected_schemas(
    tools: list[ToolSpec],
    options: AgentCatOptions,
    reported_conflicts: set[str] | None = None,
) -> InjectionResult:
    """Run both injection passes over the listed tools, in place.

    Deterministic and config-derived: identical (tools, options) inputs yield
    identical schemas and registries. `reported_conflicts` is the only
    exception — a per-server set that suppresses repeat `session_id` collision
    reports across successive listings, and the only thing here that carries
    state between calls.

    `declared_session_params` is a POSITIVE signal — "the customer's schema
    declared this name" — not the negation of `injected_params`. The two are
    not complements: a composed schema gets an entry in `injected_params`
    that stays empty, because the whole pass is skipped for it. Reading
    ownership off that emptiness would classify every composed-schema tool as
    the customer's and publish it sessionless, when in fact nobody declared
    the name and the tool is ours to correlate exactly as before.
    """
    inject_session_id = options.enable_tracing and options.resolve_session_id is None
    inject_agent_id = options.enable_tracing and options.enable_agent_tracking
    injected_params: dict[str, set[str]] = {tool.name: set() for tool in tools}
    result = InjectionResult(injected_params=injected_params, output_injected=set())

    # Handle pass first: property order is customer -> session_id -> agent_id
    # -> context. The output-schema extension lives inside this pass, so it
    # is gated with it (handle-injection.ts:59).
    if inject_session_id or inject_agent_id:
        for tool in tools:
            _add_handle_parameters(
                tool, inject_session_id, inject_agent_id, result, reported_conflicts
            )
    if options.enable_tool_call_context:
        for tool in tools:
            if tool.name == GET_MORE_TOOLS_NAME:
                continue
            _add_context_parameter(
                tool,
                options.custom_context_description,
                injected_params[tool.name],
            )
    return result


def injected_parameter_names(
    tool_name: str, registry: dict[str, set[str]] | None
) -> frozenset[str]:
    """The parameter names AgentCat injected into `tool_name`.

    With a registry, a listed tool reports exactly its recorded entry and an
    unlisted tool reports nothing (it was never advertised through the
    pipeline). Without one (tools/call before any tools/list, and the rebuild
    failed) fall back to all three names — except get_more_tools' bespoke
    context, which is a real parameter.

    Single source of truth for both consumers, deliberately: what
    `strip_injected_arguments` removes before the customer's handler runs is
    exactly what the call path may read as an AgentCat handle. Split the two
    and a customer's OWN `session_id` parameter — which the injection pass
    correctly skipped, and the strip correctly spares — gets consumed as the
    analytics handle anyway, collapsing every call to that tool into one task
    and putting a customer-domain identifier into `session_id`, a field the
    customer's redaction hook is not allowed to touch.
    """
    if registry is not None:
        return frozenset(registry.get(tool_name, ()))
    if tool_name == GET_MORE_TOOLS_NAME:
        return frozenset((SESSION_ID_PARAM, AGENT_ID_PARAM))
    return frozenset(_ALL_INJECTABLE)


def strip_injected_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    registry: dict[str, set[str]] | None,
) -> dict[str, Any]:
    """Return a new dict with ONLY the params AgentCat injected removed."""
    names: Iterable[str] = injected_parameter_names(tool_name, registry)
    cleaned = dict(arguments)
    for name in names:
        cleaned.pop(name, None)
    return cleaned
