"""Integration and unit tests for event_tags and event_properties."""

import json
import time
from unittest.mock import MagicMock

import pytest

from mcpcat import MCPCatOptions, track
from mcpcat.modules.constants import AGENTCAT_SOURCE
from mcpcat.modules.event_queue import EventQueue, set_event_queue
from mcpcat.modules.internal import (
    attach_event_metadata,
    resolve_event_properties,
    resolve_event_tags,
)
from mcpcat.modules.redaction import redact_event
from mcpcat.types import EventType, MCPCatData, SessionInfo, UnredactedEvent

from .test_utils.client import create_test_client
from .test_utils.todo_server import create_todo_server


def _make_data(event_tags=None, event_properties=None) -> MCPCatData:
    return MCPCatData(
        project_id="p",
        session_id="ses_x",
        last_activity=None,
        session_info=SessionInfo(),
        options=MCPCatOptions(
            event_tags=event_tags, event_properties=event_properties
        ),
    )


class TestResolvers:
    async def test_no_callback_returns_none(self):
        data = _make_data()
        assert await resolve_event_tags(data, None, None) is None
        assert await resolve_event_properties(data, None, None) is None

    async def test_tags_callback_result_is_validated(self):
        data = _make_data(event_tags=lambda r, e: {"env": "prod", "bad!": "x"})
        assert await resolve_event_tags(data, None, None) == {"env": "prod"}

    async def test_tags_callback_none_returns_none(self):
        data = _make_data(event_tags=lambda r, e: None)
        assert await resolve_event_tags(data, None, None) is None

    async def test_tags_callback_empty_returns_none(self):
        data = _make_data(event_tags=lambda r, e: {})
        assert await resolve_event_tags(data, None, None) is None

    async def test_tags_callback_exception_swallowed(self):
        def boom(r, e):
            raise RuntimeError("nope")

        data = _make_data(event_tags=boom)
        assert await resolve_event_tags(data, None, None) is None

    async def test_properties_callback_result_passed_through(self):
        data = _make_data(event_properties=lambda r, e: {"x": 1, "y": [1, 2]})
        assert await resolve_event_properties(data, None, None) == {
            "x": 1,
            "y": [1, 2],
        }

    async def test_properties_callback_exception_swallowed(self):
        def boom(r, e):
            raise ValueError("bad")

        data = _make_data(event_properties=boom)
        assert await resolve_event_properties(data, None, None) is None

    async def test_async_tags_callback_awaited(self):
        async def async_cb(r, e):
            return {"env": "prod", "bad!": "x"}

        data = _make_data(event_tags=async_cb)
        assert await resolve_event_tags(data, None, None) == {"env": "prod"}

    async def test_async_properties_callback_awaited(self):
        async def async_cb(r, e):
            return {"x": 1, "nested": {"y": 2}}

        data = _make_data(event_properties=async_cb)
        assert await resolve_event_properties(data, None, None) == {
            "x": 1,
            "nested": {"y": 2},
        }

    async def test_async_tags_callback_exception_swallowed(self):
        async def boom(r, e):
            raise RuntimeError("async nope")

        data = _make_data(event_tags=boom)
        assert await resolve_event_tags(data, None, None) is None

    async def test_callbacks_receive_request_and_extra(self):
        seen: list = []

        def cb(r, e):
            seen.append((r, e))
            return {"k": "v"}

        data = _make_data(event_tags=cb)
        await resolve_event_tags(data, "req-obj", "extra-obj")
        assert seen == [("req-obj", "extra-obj")]


class TestAttachMetadata:
    def _event(self):
        return UnredactedEvent(
            session_id="s",
            event_type=EventType.MCP_TOOLS_CALL.value,
        )

    async def test_no_data_is_noop(self):
        event = self._event()
        await attach_event_metadata(event, None, None, None)
        assert getattr(event, "tags", None) is None
        assert getattr(event, "properties", None) is None

    async def test_attaches_tags_and_properties(self):
        data = _make_data(
            event_tags=lambda r, e: {"env": "prod"},
            event_properties=lambda r, e: {"flag": True},
        )
        event = self._event()
        await attach_event_metadata(event, data, None, None)
        assert event.tags == {"env": "prod"}
        assert event.properties == {"flag": True}

    async def test_attaches_from_async_callbacks(self):
        async def tags_cb(r, e):
            return {"env": "prod"}

        async def props_cb(r, e):
            return {"flag": True}

        data = _make_data(event_tags=tags_cb, event_properties=props_cb)
        event = self._event()
        await attach_event_metadata(event, data, None, None)
        assert event.tags == {"env": "prod"}
        assert event.properties == {"flag": True}

    async def test_only_assigns_non_none(self):
        data = _make_data(event_tags=lambda r, e: None)
        event = self._event()
        await attach_event_metadata(event, data, None, None)
        assert getattr(event, "tags", None) is None

    async def test_callback_failure_does_not_block_event(self):
        def raises(r, e):
            raise RuntimeError("nope")

        data = _make_data(event_tags=raises, event_properties=lambda r, e: {"a": 1})
        event = self._event()
        await attach_event_metadata(event, data, None, None)
        assert getattr(event, "tags", None) is None
        assert event.properties == {"a": 1}


class TestRedactionExemption:
    def test_tags_and_properties_not_redacted(self):
        event = {
            "session_id": "s",
            "tags": {"env": "production", "trace": "abc"},
            "properties": {"secret_looking": "ssn-123-45-6789"},
            "parameters": {"q": "redact me"},
        }
        result = redact_event(event, lambda s: "[REDACTED]")
        assert result["tags"] == {"env": "production", "trace": "abc"}
        assert result["properties"] == {"secret_looking": "ssn-123-45-6789"}
        assert result["parameters"]["q"] == "[REDACTED]"


class TestFastMCPIntegration:
    """End-to-end test against the FastMCP test server."""

    @pytest.fixture(autouse=True)
    def restore_queue(self):
        from mcpcat.modules.event_queue import event_queue as original
        yield
        set_event_queue(original)

    @pytest.mark.asyncio
    async def test_callbacks_flow_through_to_published_event(self):
        captured = []
        mock_api = MagicMock()
        mock_api.publish_event = MagicMock(side_effect=lambda publish_event_request: captured.append(publish_event_request))
        set_event_queue(EventQueue(api_client=mock_api))

        server = create_todo_server()
        track(
            server,
            "proj_test",
            MCPCatOptions(
                event_tags=lambda req, ctx: {"env": "test", "bad!": "dropped"},
                event_properties=lambda req, ctx: {"flag": True, "build": "abc"},
            ),
        )

        async with create_test_client(server) as client:
            await client.call_tool("add_todo", {"text": "hi"})
            time.sleep(0.5)

        tool_events = [e for e in captured if e.event_type == EventType.MCP_TOOLS_CALL.value]
        assert tool_events, "no tool call event captured"
        event = tool_events[0]
        assert event.tags == {"env": "test"}  # bad! dropped by validation
        assert event.properties == {"flag": True, "build": "abc"}

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_break_publish(self):
        captured = []
        mock_api = MagicMock()
        mock_api.publish_event = MagicMock(side_effect=lambda publish_event_request: captured.append(publish_event_request))
        set_event_queue(EventQueue(api_client=mock_api))

        def boom(req, ctx):
            raise RuntimeError("callback broken")

        server = create_todo_server()
        track(server, "proj_test", MCPCatOptions(event_tags=boom, event_properties=boom))

        async with create_test_client(server) as client:
            await client.call_tool("add_todo", {"text": "hi"})
            time.sleep(0.5)

        tool_events = [e for e in captured if e.event_type == EventType.MCP_TOOLS_CALL.value]
        assert tool_events
        assert tool_events[0].tags is None
        assert tool_events[0].properties is None

    @pytest.mark.asyncio
    async def test_async_callbacks_flow_through_to_published_event(self):
        captured = []
        mock_api = MagicMock()
        mock_api.publish_event = MagicMock(side_effect=lambda publish_event_request: captured.append(publish_event_request))
        set_event_queue(EventQueue(api_client=mock_api))

        async def async_tags(req, ctx):
            return {"env": "test", "bad!": "dropped"}

        async def async_props(req, ctx):
            return {"flag": True, "nested": {"x": 1}}

        server = create_todo_server()
        track(
            server,
            "proj_test",
            MCPCatOptions(event_tags=async_tags, event_properties=async_props),
        )

        async with create_test_client(server) as client:
            await client.call_tool("add_todo", {"text": "hi"})
            time.sleep(0.5)

        tool_events = [e for e in captured if e.event_type == EventType.MCP_TOOLS_CALL.value]
        assert tool_events, "no tool call event captured"
        event = tool_events[0]
        assert event.tags == {"env": "test"}
        assert event.properties == {"flag": True, "nested": {"x": 1}}


class TestDatadogExporter:
    def _event(self, **kwargs):
        event = UnredactedEvent(
            session_id="s1",
            event_type=EventType.MCP_TOOLS_CALL.value,
            resource_name="my_tool",
        )
        for k, v in kwargs.items():
            setattr(event, k, v)
        return event

    def test_source_and_customer_tags_added_to_ddtags(self):
        from mcpcat.modules.exporters.datadog import DatadogExporter

        exporter = DatadogExporter(
            {"type": "datadog", "api_key": "k", "site": "datadoghq.com", "service": "svc", "env": "prod"}
        )
        event = self._event(
            tags={"Trace:Id": "abc,def", "Region": "us-east-1"},
            properties={"flag": True},
        )
        log = exporter.event_to_log(event)
        ddtags = log["ddtags"].split(",")
        assert f"source:{AGENTCAT_SOURCE}" in ddtags
        assert "agentcat.trace_id:abc_def" in ddtags  # key sanitized + value comma→_
        assert "agentcat.region:us-east-1" in ddtags
        assert log["ddsource"] == AGENTCAT_SOURCE
        assert log["mcp"]["tags"] == {"Trace:Id": "abc,def", "Region": "us-east-1"}
        assert log["mcp"]["properties"] == {"flag": True}


class TestOTLPExporter:
    def test_source_and_customer_tags_and_properties(self):
        from mcpcat.modules.exporters.otlp import OTLPExporter

        exporter = OTLPExporter({"type": "otlp"})
        event = UnredactedEvent(
            session_id="s1",
            event_type=EventType.MCP_TOOLS_CALL.value,
        )
        event.tags = {"env": "prod"}
        event.properties = {"flag": True, "count": 3}
        attrs = exporter._get_span_attributes(event)
        keys = {a["key"]: a["value"].get("stringValue") for a in attrs}
        assert keys.get("source") == AGENTCAT_SOURCE
        assert keys.get("agentcat.tag.env") == "prod"
        assert json.loads(keys["agentcat.properties"]) == {"flag": True, "count": 3}


class TestSentryExporter:
    def test_source_and_customer_tags_namespaced_and_properties_in_context(self):
        from mcpcat.modules.exporters.sentry import SentryExporter

        exporter = SentryExporter(
            {
                "type": "sentry",
                "dsn": "https://abc123@sentry.example.com/42",
                "environment": "prod",
                "enable_tracing": True,
            }
        )
        event = UnredactedEvent(
            session_id="s1",
            event_type=EventType.MCP_TOOLS_CALL.value,
            resource_name="my_tool",
            duration=100,
        )
        event.tags = {"env": "test", "trace_id": "abc"}
        event.properties = {"flag": True, "nested": {"x": 1}}

        tags = exporter.build_tags(event)
        assert tags["source"] == AGENTCAT_SOURCE
        assert tags["agentcat.env"] == "test"
        assert tags["agentcat.trace_id"] == "abc"

        contexts = exporter.build_contexts(event, {"trace_id": "t"})
        assert contexts["trace"] == {"trace_id": "t"}
        assert contexts["agentcat"] == {"flag": True, "nested": {"x": 1}}
