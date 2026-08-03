"""Pure injection pipeline: schema mutation, registries, argument stripping.

The pipeline is deliberately pure — it takes the adapter's deep copies of the
customer's schemas, mutates them in place, and reports what it did through two
registries. Every assertion here is a wire-visible contract: what an MCP client
sees in tools/list, and what the customer's tool callback receives.
"""

import copy

import pytest

from agentcat.modules import constants as c
from agentcat.modules.injection import (
    InjectionResult,
    ToolSpec,
    build_injected_schemas,
    injected_parameter_names,
    mcp_instructions_schema_property,
    strip_injected_arguments,
)
from agentcat.types import AgentCatOptions


def spec(name="t", props=None, extra=None, out=None):
    # props={} must mean "an empty properties bag", not "give me the default".
    default = {"q": {"type": "string"}}
    schema = {"type": "object", "properties": dict(default if props is None else props)}
    schema.update(extra or {})
    return ToolSpec(name=name, input_schema=schema, output_schema=out)


# ── Brief cases ──────────────────────────────────────────────────────────────


def test_param_order_and_descriptions():
    s = spec()
    r = build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    keys = list(s.input_schema["properties"])
    assert keys == ["q", "session_id", "agent_id", "context"]
    props = s.input_schema["properties"]
    assert props["session_id"]["description"] == c.SESSION_ID_PARAM_DESCRIPTION
    assert props["agent_id"]["description"] == c.AGENT_ID_PARAM_DESCRIPTION
    assert "session_id" not in s.input_schema.get("required", [])
    assert "agent_id" in s.input_schema["required"]
    assert r.injected_params["t"] == {"session_id", "agent_id", "context"}


def test_hook_mode_omits_session_id_and_switches_agent_copy():
    s = spec()
    build_injected_schemas(
        [s],
        AgentCatOptions(
            enable_agent_tracking=True, resolve_session_id=lambda q, e: "x"
        ),
    )
    props = s.input_schema["properties"]
    assert "session_id" not in props
    assert props["agent_id"]["description"] == c.AGENT_ID_PARAM_DESCRIPTION_HOOK_MODE


def test_tracing_disabled_skips_handles_but_not_context():
    s = spec()
    r = build_injected_schemas([s], AgentCatOptions(enable_tracing=False))
    assert set(s.input_schema["properties"]) == {"q", "context"}
    assert r.injected_params["t"] == {"context"}


def test_additional_properties_false_removed():
    s = spec(extra={"additionalProperties": False})
    build_injected_schemas([s], AgentCatOptions())
    assert "additionalProperties" not in s.input_schema


def test_composed_schema_skipped_with_empty_registry_entry():
    s = ToolSpec(name="t", input_schema={"oneOf": [{"type": "object"}]})
    r = build_injected_schemas([s], AgentCatOptions())
    assert s.input_schema == {"oneOf": [{"type": "object"}]}
    assert r.injected_params["t"] == set()


def test_collision_skips_that_param_only():
    s = spec(props={"session_id": {"type": "string", "description": "customer"}})
    r = build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    assert s.input_schema["properties"]["session_id"]["description"] == "customer"
    assert r.injected_params["t"] == {"agent_id", "context"}


def test_get_more_tools_gets_handles_but_not_context():
    s = spec(name=c.GET_MORE_TOOLS_NAME, props={"context": {"type": "string"}})
    r = build_injected_schemas([s], AgentCatOptions())
    assert r.injected_params[c.GET_MORE_TOOLS_NAME] == {"session_id"}


def test_output_schema_extension_and_registry():
    s = spec(out={"type": "object", "properties": {"answer": {"type": "string"}}})
    r = build_injected_schemas([s], AgentCatOptions())
    prop = s.output_schema["properties"][c.MCP_INSTRUCTIONS_KEY]
    assert prop["description"] == c.MCP_INSTRUCTIONS_FIELD_DESCRIPTION
    task_prop = prop["properties"]["session_id"]
    assert task_prop["description"] == c.MCP_INSTRUCTIONS_SESSION_ID_DESCRIPTION
    # Default options are prompted mode with agent tracking off, so the copy
    # never mentions an agent_id the agent was not asked for.
    assert list(prop["properties"]) == ["session_id", "instructions"]
    assert c.MCP_INSTRUCTIONS_KEY not in s.output_schema.get("required", [])
    assert r.output_injected == {"t"}
    s2 = spec(out={"oneOf": []})
    r2 = build_injected_schemas([s2], AgentCatOptions())
    assert r2.output_injected == set()


def test_determinism():
    def make():
        return [spec(), spec(name="u", extra={"additionalProperties": False})]

    a, b = make(), make()
    ra = build_injected_schemas(a, AgentCatOptions(enable_agent_tracking=True))
    rb = build_injected_schemas(b, AgentCatOptions(enable_agent_tracking=True))
    assert ra == rb and [t.input_schema for t in a] == [t.input_schema for t in b]


def test_strip_registry_driven_and_heuristic():
    args = {"q": 1, "session_id": "s", "agent_id": "a", "context": "c"}
    reg = {"t": {"session_id", "context"}}
    out = strip_injected_arguments("t", args, reg)
    assert out == {"q": 1, "agent_id": "a"} and args["session_id"] == "s"  # clone
    # Registry present but tool not in it: it was never advertised through the
    # pipeline, so nothing was injected for it — strip nothing.
    assert strip_injected_arguments("unknown", args, reg) == args
    assert strip_injected_arguments("t", args, None) == {"q": 1}
    gmt = {"context": "real", "session_id": "s"}
    stripped = strip_injected_arguments(c.GET_MORE_TOOLS_NAME, gmt, None)
    assert stripped == {"context": "real"}


# ── Ported from the TS SDK: required-array semantics ─────────────────────────
# src/tests/handle-injection.test.ts — "creates the required array when the
# schema has none", "does not duplicate agent_id in an existing required
# array", "leaves required untouched when the customer declares agent_id".


def test_required_list_preserved_when_appending_agent_id():
    s = spec(props={"text": {"type": "string"}}, extra={"required": ["text"]})
    build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    assert s.input_schema["required"] == ["text", "agent_id", "context"]


def test_required_is_created_for_the_params_that_need_it():
    with_agent = spec()
    build_injected_schemas([with_agent], AgentCatOptions(enable_agent_tracking=True))
    assert with_agent.input_schema["required"] == ["agent_id", "context"]

    # session_id is the one injected param that is never required — omitting it
    # is how an agent asks to be minted one — but context is, so a schema that
    # declared no required array does grow one.
    without_agent = spec()
    build_injected_schemas([without_agent], AgentCatOptions())
    assert without_agent.input_schema["required"] == ["context"]

    # ...and with the context pass off there is nothing to require at all.
    neither = spec()
    build_injected_schemas([neither], AgentCatOptions(enable_tool_call_context=False))
    assert "required" not in neither.input_schema


def test_agent_id_not_duplicated_in_existing_required_array():
    # A schema can list a name in required without declaring the property;
    # injection must not push a second copy.
    s = spec(props={}, extra={"required": ["agent_id"]})
    build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    assert s.input_schema["required"] == ["agent_id", "context"]


def test_customer_declared_agent_id_leaves_required_untouched():
    s = spec(
        props={"agent_id": {"type": "string", "description": "mine"}},
        extra={"required": []},
    )
    r = build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    # agent_id is theirs, so it is neither redescribed nor required by us.
    # context is still ours, and still required.
    assert s.input_schema["required"] == ["context"]
    assert s.input_schema["properties"]["agent_id"]["description"] == "mine"
    assert "agent_id" not in r.injected_params["t"]


def test_empty_properties_tool_receives_all_params():
    s = spec(props={})
    r = build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    assert list(s.input_schema["properties"]) == ["session_id", "agent_id", "context"]
    assert r.injected_params["t"] == {"session_id", "agent_id", "context"}


# ── Ported: additionalProperties variants ────────────────────────────────────
# Only the `false` form blocks optional handles; every other form is the
# customer's business and must survive untouched.


def test_additional_properties_true_and_dict_forms_untouched():
    permissive = spec(name="p", extra={"additionalProperties": True})
    schema_form = spec(name="s", extra={"additionalProperties": {"type": "string"}})
    build_injected_schemas([permissive, schema_form], AgentCatOptions())
    assert permissive.input_schema["additionalProperties"] is True
    assert schema_form.input_schema["additionalProperties"] == {"type": "string"}


# ── Ported: deep-copy isolation ──────────────────────────────────────────────
# The adapter hands the pipeline deep copies and the pipeline mutates them in
# place, so nested customer structures must come out byte-identical and the
# minted _mcp_instructions fragment must never be shared between tools.


def test_nested_customer_schema_is_not_mutated():
    nested = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "object",
                "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
                "required": ["tags"],
            }
        },
    }
    original = copy.deepcopy(nested["properties"]["filter"])
    s = ToolSpec(name="t", input_schema=nested)
    build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    assert s.input_schema["properties"]["filter"] == original


def test_pipeline_mutates_in_place_rather_than_replacing_schemas():
    s = spec()
    schema_ref = s.input_schema
    build_injected_schemas([s], AgentCatOptions())
    assert s.input_schema is schema_ref


def test_mcp_instructions_fragment_is_not_shared_between_tools():
    a = spec(name="a", out={"type": "object", "properties": {}})
    b = spec(name="b", out={"type": "object", "properties": {}})
    build_injected_schemas([a, b], AgentCatOptions())
    frag_a = a.output_schema["properties"][c.MCP_INSTRUCTIONS_KEY]
    frag_b = b.output_schema["properties"][c.MCP_INSTRUCTIONS_KEY]
    assert frag_a == frag_b
    frag_a["properties"]["session_id"]["description"] = "mutated"
    assert frag_b["properties"]["session_id"]["description"] != "mutated"


def test_mcp_instructions_schema_property_shape():
    frag = mcp_instructions_schema_property(True, True)
    assert frag["type"] == "object"
    assert frag["description"] == c.MCP_INSTRUCTIONS_FIELD_DESCRIPTION
    assert list(frag["properties"]) == ["session_id", "agent_id", "instructions"]
    task_prop = frag["properties"]["session_id"]
    assert task_prop["description"] == c.MCP_INSTRUCTIONS_SESSION_ID_DESCRIPTION
    agent_prop = frag["properties"]["agent_id"]
    assert agent_prop["description"] == c.MCP_INSTRUCTIONS_AGENT_ID_DESCRIPTION
    assert frag["properties"]["instructions"] == {"type": "string"}
    assert mcp_instructions_schema_property(True, True) is not frag


def test_mcp_instructions_schema_property_tracks_the_flags():
    # The copy never references a parameter the agent cannot see, so each
    # sub-property is gated by the handle that produced it. `instructions` is
    # unconditional.
    prompted_only = mcp_instructions_schema_property(True, False)
    assert list(prompted_only["properties"]) == ["session_id", "instructions"]
    hook_only = mcp_instructions_schema_property(False, True)
    assert list(hook_only["properties"]) == ["agent_id", "instructions"]


# ── Ported: registry completeness & injection order ──────────────────────────


def test_registry_has_an_entry_for_every_tool():
    plain = spec(name="plain")
    composed = ToolSpec(name="composed", input_schema={"allOf": [{"type": "object"}]})
    owns_all = spec(
        name="owns_all",
        props={
            "session_id": {"type": "string"},
            "agent_id": {"type": "string"},
            "context": {"type": "string"},
        },
    )
    r = build_injected_schemas(
        [plain, composed, owns_all], AgentCatOptions(enable_agent_tracking=True)
    )
    assert set(r.injected_params) == {"plain", "composed", "owns_all"}
    assert r.injected_params["composed"] == set()
    assert r.injected_params["owns_all"] == set()


def test_context_disabled_leaves_only_handles():
    s = spec()
    r = build_injected_schemas(
        [s],
        AgentCatOptions(enable_agent_tracking=True, enable_tool_call_context=False),
    )
    assert list(s.input_schema["properties"]) == ["q", "session_id", "agent_id"]
    assert r.injected_params["t"] == {"session_id", "agent_id"}


def test_context_uses_custom_description():
    s = spec()
    build_injected_schemas([s], AgentCatOptions(custom_context_description="why?"))
    assert s.input_schema["properties"]["context"]["description"] == "why?"


def test_get_more_tools_keeps_bespoke_context_and_gains_both_handles():
    s = spec(
        name=c.GET_MORE_TOOLS_NAME,
        props={"context": {"type": "string", "description": "bespoke"}},
        extra={"required": ["context"]},
    )
    r = build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    props = s.input_schema["properties"]
    assert list(props) == ["context", "session_id", "agent_id"]
    assert props["context"]["description"] == "bespoke"
    assert r.injected_params[c.GET_MORE_TOOLS_NAME] == {"session_id", "agent_id"}


# ── Ported: outputSchema edges ───────────────────────────────────────────────
# src/tests/handle-injection.test.ts "outputSchema injection" + v2
# schema-edges.test.ts "outputSchema injection".


def test_output_schema_contract_otherwise_untouched():
    out = {
        "type": "object",
        "properties": {"count": {"type": "number"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    s = spec(out=out)
    build_injected_schemas([s], AgentCatOptions())
    # additionalProperties: false stays on the output side — our property is
    # declared, so it does not need loosening.
    assert s.output_schema["additionalProperties"] is False
    assert s.output_schema["required"] == ["count"]
    assert s.output_schema["properties"]["count"] == {"type": "number"}


def test_customer_declared_mcp_instructions_output_never_clobbered():
    out = {
        "type": "object",
        "properties": {
            c.MCP_INSTRUCTIONS_KEY: {"type": "string", "description": "customer-owned"}
        },
    }
    s = spec(out=out)
    r = build_injected_schemas([s], AgentCatOptions())
    prop = s.output_schema["properties"][c.MCP_INSTRUCTIONS_KEY]
    assert prop == {"type": "string", "description": "customer-owned"}
    assert r.output_injected == set()


def test_no_output_schema_registers_nothing():
    s = spec()
    r = build_injected_schemas([s], AgentCatOptions())
    assert s.output_schema is None
    assert r.output_injected == set()


def test_output_schema_without_properties_bag_gets_one_created():
    # A declared-but-bare object schema is still a single extendable bag;
    # the pipeline mints `properties` rather than skipping the tool.
    s = spec(out={"type": "object"})
    r = build_injected_schemas([s], AgentCatOptions())
    assert list(s.output_schema["properties"]) == [c.MCP_INSTRUCTIONS_KEY]
    assert r.output_injected == {"t"}


def test_composed_input_schema_skips_output_injection_too():
    out = {"type": "object", "properties": {"count": {"type": "number"}}}
    s = ToolSpec(
        name="mixed",
        input_schema={"anyOf": [{"type": "object"}]},
        output_schema=out,
    )
    r = build_injected_schemas([s], AgentCatOptions())
    assert c.MCP_INSTRUCTIONS_KEY not in s.output_schema["properties"]
    assert r.output_injected == set()
    assert r.injected_params["mixed"] == set()


# ── Handle-pass gating ───────────────────────────────────────────────────────
# handle-injection.ts:59 returns early unless at least one handle is
# injectable, and listWrap.ts:50-62 computes the two flags as
# inject_session_id = enable_tracing and resolve_session_id is None
# inject_agent_id = enable_tracing and enable_agent_tracking
# The output-schema extension lives inside that pass, so it is gated too.
# Context injection is a separate pass and runs regardless.


def _out():
    return {"type": "object", "properties": {"count": {"type": "number"}}}


def test_tracing_disabled_leaves_output_schemas_untouched():
    s = spec(out=_out())
    r = build_injected_schemas([s], AgentCatOptions(enable_tracing=False))
    assert s.output_schema == _out()
    assert r.output_injected == set()
    assert r.injected_params["t"] == {"context"}


def test_hook_mode_without_agent_tracking_skips_the_whole_handle_pass():
    s = spec(out=_out())
    r = build_injected_schemas(
        [s], AgentCatOptions(resolve_session_id=lambda q, e: "x")
    )
    assert set(s.input_schema["properties"]) == {"q", "context"}
    assert s.output_schema == _out()
    assert r.output_injected == set()
    assert r.injected_params["t"] == {"context"}


def test_no_injectable_handle_and_no_context_leaves_an_empty_registry_entry():
    s = spec(out=_out())
    r = build_injected_schemas(
        [s], AgentCatOptions(enable_tracing=False, enable_tool_call_context=False)
    )
    assert set(s.input_schema["properties"]) == {"q"}
    assert r.injected_params["t"] == set()
    assert r.output_injected == set()


def test_hook_mode_with_agent_tracking_extends_output_without_session_id():
    s = spec(out=_out())
    r = build_injected_schemas(
        [s],
        AgentCatOptions(
            enable_agent_tracking=True, resolve_session_id=lambda q, e: "x"
        ),
    )
    frag = s.output_schema["properties"][c.MCP_INSTRUCTIONS_KEY]
    assert list(frag["properties"]) == ["agent_id", "instructions"]
    assert r.output_injected == {"t"}


def test_prompted_mode_with_agent_tracking_extends_output_with_both():
    s = spec(out=_out())
    build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    frag = s.output_schema["properties"][c.MCP_INSTRUCTIONS_KEY]
    assert list(frag["properties"]) == ["session_id", "agent_id", "instructions"]


# ── Input-schema normalization ───────────────────────────────────────────────
# handle-injection.ts:82-89 — an absent schema becomes a bare object schema,
# and an existing schema missing its properties bag has one created.


def test_empty_input_schema_is_normalized_before_injection():
    s = ToolSpec(name="t", input_schema={})
    build_injected_schemas([s], AgentCatOptions())
    assert s.input_schema["type"] == "object"
    assert list(s.input_schema["properties"]) == ["session_id", "context"]
    assert s.input_schema["required"] == ["context"]


def test_input_schema_without_properties_bag_gets_one_created():
    s = ToolSpec(name="t", input_schema={"type": "object", "title": "Bare"})
    r = build_injected_schemas([s], AgentCatOptions(enable_agent_tracking=True))
    assert list(s.input_schema["properties"]) == ["session_id", "agent_id", "context"]
    assert s.input_schema["title"] == "Bare"
    assert r.injected_params["t"] == {"session_id", "agent_id", "context"}


# ── Ported: strip semantics ──────────────────────────────────────────────────


def test_strip_preserves_a_customer_owned_session_id():
    # The registry records only what the pipeline injected, so a tool that
    # declares its own session_id keeps the caller's value.
    reg = {"deploy": {"agent_id", "context"}}
    args = {"session_id": "prod-42", "agent_id": "agt_1", "context": "why"}
    assert strip_injected_arguments("deploy", args, reg) == {"session_id": "prod-42"}


def test_injected_names_is_the_single_source_the_strip_reads():
    """The strip and the handle extraction MUST agree, so they read one
    function. A name this does not report is the customer's parameter: it stays
    in their arguments and it is not ours to read as a handle."""
    reg = {"deploy": {"agent_id", "context"}, "t": {"session_id", "context"}}
    assert injected_parameter_names("deploy", reg) == frozenset({"agent_id", "context"})
    assert injected_parameter_names("t", reg) == frozenset({"session_id", "context"})
    # Registry present, tool absent: never advertised through the pipeline.
    assert injected_parameter_names("unknown", reg) == frozenset()
    # No registry at all (rebuild failed): the heuristic, which the strip
    # follows too — so the two stay consistent even on the degraded path.
    assert injected_parameter_names("t", None) == frozenset(
        {"session_id", "agent_id", "context"}
    )
    assert injected_parameter_names(c.GET_MORE_TOOLS_NAME, None) == frozenset(
        {"session_id", "agent_id"}
    )


@pytest.mark.parametrize(
    "registry",
    [{"deploy": {"agent_id", "context"}}, {"deploy": set()}, {}, None],
    ids=["own-task-id", "nothing-injected", "unlisted", "no-registry"],
)
def test_what_the_strip_removes_is_what_the_names_report(registry):
    args = {"session_id": "prod-42", "agent_id": "agt_1", "context": "why", "q": 1}
    names = injected_parameter_names("deploy", registry)
    stripped = strip_injected_arguments("deploy", args, registry)
    assert set(args) - set(stripped) == names & set(args)


def test_strip_always_returns_a_new_dict():
    args = {"q": 1}
    for registry in ({"t": set()}, None, {}):
        out = strip_injected_arguments("t", args, registry)
        assert out is not args
    assert strip_injected_arguments("t", {}, None) == {}


def test_strip_heuristic_leaves_unrelated_arguments_alone():
    args = {"text": "x", "session_id": "a", "agent_id": "b", "context": "c", "id": 7}
    assert strip_injected_arguments("any", args, None) == {"text": "x", "id": 7}


def test_injection_result_equality_is_value_based():
    a = InjectionResult(injected_params={"t": {"context"}}, output_injected=set())
    b = InjectionResult(injected_params={"t": {"context"}}, output_injected=set())
    assert a == b
