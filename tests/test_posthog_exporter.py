"""Tests for the PostHog telemetry exporter.

Mirrors the TypeScript SDK's PostHogExporter (src/modules/exporters/posthog.ts):
events POST to /batch, error events add a $exception capture, and tool calls
optionally add an $ai_span capture when enable_ai_tracing is set.
"""

import re
from datetime import datetime, timezone
from unittest.mock import MagicMock

from agentcat.modules.exporters.posthog import (
    DEFAULT_POSTHOG_HOST,
    PostHogExporter,
    to_uuidv7,
)
from agentcat.modules.telemetry import TelemetryManager
from agentcat.types import Event
from agentcat.utils import generate_prefixed_ksuid

UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def make_event(**kwargs) -> Event:
    defaults = {
        "id": "evt_2QjWl3PYbPnPUyYd5b5mFyJXk1a",
        "event_type": "mcp:tools/call",
        "project_id": "proj_123",
        "session_id": "ses_2QjWl3PYbPnPUyYd5b5mFyJXk1a",
        "resource_name": "add_todo",
        "duration": 250,
        "timestamp": datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        "server_name": "todo-server",
        "server_version": "1.0.0",
        "client_name": "Cursor",
        "client_version": "2.6.22",
    }
    defaults.update(kwargs)
    return Event(**defaults)


def export_and_get_payload(exporter: PostHogExporter, event: Event) -> dict:
    exporter.session = MagicMock()
    exporter.export(event)
    assert exporter.session.post.called
    call = exporter.session.post.call_args
    return call.args[0] if call.args else call.kwargs.get("url"), call.kwargs["json"]


class TestToUUIDv7:
    def test_deterministic(self):
        session_id = generate_prefixed_ksuid("ses")
        assert to_uuidv7(session_id) == to_uuidv7(session_id)

    def test_different_inputs_differ(self):
        assert to_uuidv7(generate_prefixed_ksuid("ses")) != to_uuidv7(
            generate_prefixed_ksuid("ses")
        )

    def test_valid_uuidv7_format(self):
        result = to_uuidv7(generate_prefixed_ksuid("ses"))
        assert UUID_V7_RE.match(result), result

    def test_invalid_ksuid_falls_back_without_error(self):
        result = to_uuidv7("ses_not-a-real-ksuid")
        assert UUID_V7_RE.match(result), result

    def test_none_input_does_not_crash(self):
        result = to_uuidv7(None)
        assert UUID_V7_RE.match(result), result


class TestPostHogExporterConfig:
    def test_default_host(self):
        exporter = PostHogExporter({"type": "posthog", "api_key": "phc_test"})
        assert exporter.batch_url == f"{DEFAULT_POSTHOG_HOST}/batch"
        assert exporter.enable_ai_tracing is False

    def test_custom_host_trailing_slash_stripped(self):
        exporter = PostHogExporter(
            {"type": "posthog", "api_key": "phc_test", "host": "https://eu.i.posthog.com/"}
        )
        assert exporter.batch_url == "https://eu.i.posthog.com/batch"

    def test_telemetry_manager_registration(self):
        manager = TelemetryManager(
            {"ph": {"type": "posthog", "api_key": "phc_test"}}
        )
        assert manager.get_exporter_count() == 1
        assert isinstance(manager.exporters["ph"], PostHogExporter)


class TestPostHogCaptureEvent:
    def _exporter(self, **config):
        return PostHogExporter({"type": "posthog", "api_key": "phc_test", **config})

    def test_batch_payload_structure(self):
        _, payload = export_and_get_payload(self._exporter(), make_event())
        assert payload["api_key"] == "phc_test"
        assert len(payload["batch"]) == 1
        capture = payload["batch"][0]
        assert capture["event"] == "mcp_tool_call"
        assert capture["type"] == "capture"
        assert capture["timestamp"].startswith("2025-06-01T12:00:00")

    def test_distinct_id_prefers_actor(self):
        _, payload = export_and_get_payload(
            self._exporter(), make_event(identify_actor_given_id="alice")
        )
        assert payload["batch"][0]["distinct_id"] == "alice"

    def test_distinct_id_falls_back_to_session_then_anonymous(self):
        event = make_event()
        _, payload = export_and_get_payload(self._exporter(), event)
        assert payload["batch"][0]["distinct_id"] == event.session_id

        _, payload = export_and_get_payload(
            self._exporter(), make_event(session_id=None)
        )
        assert payload["batch"][0]["distinct_id"] == "anonymous"

    def test_capture_properties(self):
        event = make_event(user_intent="add an item", parameters={"a": 1})
        _, payload = export_and_get_payload(self._exporter(), event)
        props = payload["batch"][0]["properties"]

        assert props["$session_id"] == to_uuidv7(event.session_id)
        assert UUID_V7_RE.match(props["$session_id"])
        assert props["source"] == "agentcat"
        assert props["resource_name"] == "add_todo"
        assert props["tool_name"] == "add_todo"  # tools/call adds tool_name
        assert props["duration_ms"] == 250
        assert props["server_name"] == "todo-server"
        assert props["server_version"] == "1.0.0"
        assert props["client_name"] == "Cursor"
        assert props["client_version"] == "2.6.22"
        assert props["project_id"] == "proj_123"
        assert props["user_intent"] == "add an item"
        assert props["parameters"] == {"a": 1}

    def test_event_name_mapping(self):
        exporter = self._exporter()
        assert exporter.map_event_type("mcp:tools/call") == "mcp_tool_call"
        assert exporter.map_event_type("mcp:tools/list") == "mcp_tools_list"
        assert exporter.map_event_type("mcp:initialize") == "mcp_initialize"
        assert exporter.map_event_type("mcp:resources/read") == "mcp_resource_read"
        assert exporter.map_event_type("mcp:resources/list") == "mcp_resources_list"
        assert exporter.map_event_type("mcp:prompts/get") == "mcp_prompt_get"
        assert exporter.map_event_type("mcp:prompts/list") == "mcp_prompts_list"
        # Fallback for unmapped types
        assert exporter.map_event_type("mcp:ping") == "mcp_ping"
        assert (
            exporter.map_event_type("mcp:resources/templates/list")
            == "mcp_resources_templates_list"
        )

    def test_person_properties_from_identity(self):
        event = make_event(
            identify_actor_given_id="alice",
            identify_actor_name="Alice",
            identify_data={"plan": "pro"},
        )
        _, payload = export_and_get_payload(self._exporter(), event)
        props = payload["batch"][0]["properties"]
        assert props["$set"] == {"name": "Alice", "plan": "pro"}

    def test_customer_tags_and_properties_spread(self):
        event = make_event(
            tags={"env": "prod", "source": "customer-override"},
            properties={"feature_flags": ["dark_mode"]},
        )
        _, payload = export_and_get_payload(self._exporter(), event)
        props = payload["batch"][0]["properties"]
        assert props["env"] == "prod"
        # Customer tags can override AgentCat defaults
        assert props["source"] == "customer-override"
        assert props["feature_flags"] == ["dark_mode"]


class TestPostHogExceptionEvent:
    def _exporter(self):
        return PostHogExporter({"type": "posthog", "api_key": "phc_test"})

    def test_error_event_adds_exception_capture(self):
        event = make_event(
            is_error=True,
            error={
                "message": "boom",
                "type": "ValueError",
                "stack": "Traceback ...",
            },
        )
        _, payload = export_and_get_payload(self._exporter(), event)
        assert len(payload["batch"]) == 2
        exception = payload["batch"][1]
        assert exception["event"] == "$exception"
        props = exception["properties"]
        assert props["$exception_source"] == "backend"
        assert props["$exception_message"] == "boom"
        assert props["$exception_type"] == "ValueError"
        assert props["$exception_stacktrace"] == "Traceback ..."
        assert props["$session_id"] == to_uuidv7(event.session_id)
        assert props["tool_name"] == "add_todo"

    def test_non_error_event_has_no_exception(self):
        _, payload = export_and_get_payload(self._exporter(), make_event())
        assert [e["event"] for e in payload["batch"]] == ["mcp_tool_call"]


class TestPostHogAISpanEvent:
    def _exporter(self, enable_ai_tracing=True):
        return PostHogExporter(
            {
                "type": "posthog",
                "api_key": "phc_test",
                "enable_ai_tracing": enable_ai_tracing,
            }
        )

    def test_ai_span_disabled_by_default(self):
        exporter = PostHogExporter({"type": "posthog", "api_key": "phc_test"})
        _, payload = export_and_get_payload(exporter, make_event())
        assert [e["event"] for e in payload["batch"]] == ["mcp_tool_call"]

    def test_ai_span_emitted_for_tool_calls(self):
        event = make_event(parameters={"in": 1}, response={"out": 2})
        _, payload = export_and_get_payload(self._exporter(), event)
        assert [e["event"] for e in payload["batch"]] == [
            "mcp_tool_call",
            "$ai_span",
        ]
        props = payload["batch"][1]["properties"]
        assert props["$ai_session_id"] == f"agentcat_{event.session_id}"
        assert props["$ai_trace_id"] == to_uuidv7(event.session_id)
        assert props["$ai_span_id"] == to_uuidv7(event.id)
        assert props["$ai_span_name"] == "add_todo"
        assert props["$ai_is_error"] is False
        assert props["$ai_latency"] == 0.25
        assert props["$ai_input_state"] == {"in": 1}
        assert props["$ai_output_state"] == {"out": 2}

    def test_ai_span_not_emitted_for_non_tool_calls(self):
        event = make_event(event_type="mcp:initialize", resource_name=None)
        _, payload = export_and_get_payload(self._exporter(), event)
        assert [e["event"] for e in payload["batch"]] == ["mcp_initialize"]

    def test_ai_span_error_fields(self):
        event = make_event(is_error=True, error={"message": "boom"})
        _, payload = export_and_get_payload(self._exporter(), event)
        # capture + $exception + $ai_span
        assert [e["event"] for e in payload["batch"]] == [
            "mcp_tool_call",
            "$exception",
            "$ai_span",
        ]
        props = payload["batch"][2]["properties"]
        assert props["$ai_is_error"] is True
        assert props["$ai_error"] == {"message": "boom"}


class TestPostHogExportResilience:
    def test_network_error_is_swallowed(self):
        exporter = PostHogExporter({"type": "posthog", "api_key": "phc_test"})
        exporter.session = MagicMock()
        exporter.session.post.side_effect = RuntimeError("network down")
        # Must not raise — telemetry never crashes the host
        exporter.export(make_event())
