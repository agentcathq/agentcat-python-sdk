"""Tests for agentcat.publish_custom_event.

Mirrors the TypeScript SDK's publishCustomEvent (src/index.ts): custom events
carry the fixed "agentcat:custom" event type and can be published either
through a tracked server (using its session) or with a raw MCP session ID
string (deriving a deterministic AgentCat session ID).
"""

from unittest.mock import patch

import pytest

import agentcat
from agentcat import AgentCatOptions, CustomEventData, publish_custom_event
from agentcat.modules.constants import AGENTCAT_CUSTOM_EVENT_TYPE
from agentcat.modules.internal import (
    get_server_tracking_data,
    reset_all_tracking_data,
)
from agentcat.modules.session import derive_session_id_from_mcp_session
from agentcat.utils import parse_prefixed_ksuid

from .test_utils.todo_server import create_todo_server


class TestDeriveSessionId:
    """Deterministic session ID derivation from MCP session IDs."""

    def test_same_inputs_produce_same_session_id(self):
        a = derive_session_id_from_mcp_session("mcp-session-1", "proj_123")
        b = derive_session_id_from_mcp_session("mcp-session-1", "proj_123")
        assert a == b

    def test_different_session_ids_differ(self):
        a = derive_session_id_from_mcp_session("mcp-session-1", "proj_123")
        b = derive_session_id_from_mcp_session("mcp-session-2", "proj_123")
        assert a != b

    def test_different_projects_differ(self):
        a = derive_session_id_from_mcp_session("mcp-session-1", "proj_123")
        b = derive_session_id_from_mcp_session("mcp-session-1", "proj_456")
        assert a != b

    def test_project_id_optional(self):
        a = derive_session_id_from_mcp_session("mcp-session-1")
        b = derive_session_id_from_mcp_session("mcp-session-1")
        assert a == b
        assert a != derive_session_id_from_mcp_session("mcp-session-1", "proj_123")

    def test_result_is_valid_prefixed_ksuid(self):
        derived = derive_session_id_from_mcp_session("mcp-session-1", "proj_123")
        prefix, ksuid = parse_prefixed_ksuid(derived)
        assert prefix == "ses"
        # Timestamp must fall within [2024-01-01, 2024-01-01 + 1 year)
        assert 1704067200 <= ksuid.timestamp < 1704067200 + 365 * 24 * 60 * 60


class TestPublishCustomEventValidation:
    def test_missing_project_id_raises(self):
        with pytest.raises(ValueError, match="project_id is required"):
            publish_custom_event("mcp-session-1", "")

    def test_none_project_id_raises(self):
        with pytest.raises(ValueError, match="project_id is required"):
            publish_custom_event("mcp-session-1", None)

    def test_untracked_server_raises(self):
        reset_all_tracking_data()
        server = create_todo_server()
        with pytest.raises(ValueError, match="Server is not tracked"):
            publish_custom_event(server, "proj_123")

    def test_invalid_first_parameter_raises(self):
        with pytest.raises(TypeError, match="MCP server object or a session ID"):
            publish_custom_event(42, "proj_123")

    def test_none_first_parameter_raises(self):
        with pytest.raises(TypeError, match="MCP server object or a session ID"):
            publish_custom_event(None, "proj_123")


class TestPublishCustomEventWithSessionId:
    """String session ID path: event goes directly to the event queue."""

    def _publish(self, event_data=None, mcp_session_id="mcp-session-1"):
        with patch("agentcat.modules.event_queue.event_queue") as mock_queue:
            publish_custom_event(mcp_session_id, "proj_123", event_data)
            assert mock_queue.add.call_count == 1
            return mock_queue.add.call_args.args[0]

    def test_event_core_fields(self):
        event = self._publish()
        assert event.event_type == AGENTCAT_CUSTOM_EVENT_TYPE
        assert event.project_id == "proj_123"
        assert event.session_id == derive_session_id_from_mcp_session(
            "mcp-session-1", "proj_123"
        )
        assert event.timestamp is not None

    def test_event_data_fields_mapped(self):
        event = self._publish(
            CustomEventData(
                resource_name="custom-action",
                parameters={"action": "user-feedback", "rating": 5},
                response={"ok": True},
                message="User provided feedback",
                duration=1234,
                is_error=True,
                error={"message": "Custom error occurred", "code": "ERR_001"},
            )
        )
        assert event.resource_name == "custom-action"
        assert event.parameters == {"action": "user-feedback", "rating": 5}
        assert event.response == {"ok": True}
        assert event.user_intent == "User provided feedback"
        assert event.duration == 1234
        assert event.is_error is True
        assert event.error == {"message": "Custom error occurred", "code": "ERR_001"}

    def test_tags_are_validated(self):
        event = self._publish(
            CustomEventData(
                tags={"env": "prod", "bad\nkey!": "x", "numeric": 5},
            )
        )
        assert event.tags == {"env": "prod"}

    def test_properties_attached(self):
        event = self._publish(
            CustomEventData(properties={"feature_flags": ["dark_mode"]})
        )
        assert event.properties == {"feature_flags": ["dark_mode"]}

    def test_empty_properties_omitted(self):
        event = self._publish(CustomEventData(properties={}))
        assert event.properties is None


class TestPublishCustomEventWithTrackedServer:
    def setup_method(self):
        reset_all_tracking_data()

    def teardown_method(self):
        reset_all_tracking_data()

    def test_tracked_server_uses_its_session(self):
        server = create_todo_server()
        agentcat.track(server, "proj_tracked", AgentCatOptions())
        data = get_server_tracking_data(server)
        assert data is not None

        with patch("agentcat.modules.event_queue.event_queue") as mock_queue:
            publish_custom_event(
                server,
                "proj_tracked",
                CustomEventData(resource_name="feature-usage"),
            )
            assert mock_queue.add.call_count == 1
            event = mock_queue.add.call_args.args[0]

        assert event.event_type == AGENTCAT_CUSTOM_EVENT_TYPE
        assert event.session_id == data.session_id
        assert event.project_id == "proj_tracked"
        assert event.resource_name == "feature-usage"
        # Published via publish_event → session info merged in
        assert event.sdk_language is not None
