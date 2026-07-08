"""Structured error capture on the low-level (non-FastMCP) server path.

overrides/mcp_server.py must record errors via modules.exceptions.capture_exception
(message/type/frames/platform), matching the official and community paths,
instead of a bare {"message": ...} dict.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from mcp.server import Server as LowLevelServer
from mcp.types import CallToolRequest, CallToolRequestParams

from agentcat.modules.internal import (
    reset_all_tracking_data,
    set_server_tracking_data,
)
from agentcat.modules.overrides.mcp_server import override_lowlevel_mcp_server
from agentcat.types import AgentCatData, AgentCatOptions, SessionInfo


def _make_request(name="boom", arguments=None) -> CallToolRequest:
    return CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments or {}),
    )


class TestLowLevelErrorCapture:
    def setup_method(self):
        reset_all_tracking_data()

    def teardown_method(self):
        reset_all_tracking_data()

    def _setup_server(self) -> LowLevelServer:
        server = LowLevelServer("lowlevel-error-server")

        @server.list_tools()
        async def list_tools():
            return []

        @server.call_tool()
        async def call_tool(name, arguments):
            raise ValueError("tool blew up")

        data = AgentCatData(
            project_id="test_project",
            session_id="ses_lowlevel_err",
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=AgentCatOptions(
                enable_tracing=True,
                enable_tool_call_context=False,
                enable_report_missing=False,
            ),
        )
        set_server_tracking_data(server, data)
        override_lowlevel_mcp_server(server, data)
        return server

    async def test_error_response_captured_structurally(self):
        """The MCP SDK converts tool exceptions into isError CallToolResults;
        the published event must carry structured ErrorData, not just a bare
        message dict."""
        server = self._setup_server()
        handler = server.request_handlers[CallToolRequest]

        with patch(
            "agentcat.modules.overrides.mcp_server.event_queue"
        ) as mock_event_queue:
            await handler(_make_request())

        events = [
            call.args[1] for call in mock_event_queue.publish_event.call_args_list
        ]
        call_events = [e for e in events if e.event_type == "mcp:tools/call"]
        assert call_events, "tools/call event not published"
        event = call_events[0]

        assert event.is_error is True
        assert isinstance(event.error, dict)
        assert "tool blew up" in event.error["message"]
        # Structured ErrorData shape from capture_exception
        assert event.error["platform"] == "python"
        assert "type" in event.error

    async def test_handler_exception_captured_with_frames(self):
        """An exception escaping the original handler must be captured with
        full structure: type, stack frames, and platform."""
        server = LowLevelServer("lowlevel-raise-server")

        async def exploding_handler(request):
            raise RuntimeError("handler exploded")

        server.request_handlers[CallToolRequest] = exploding_handler

        data = AgentCatData(
            project_id="test_project",
            session_id="ses_lowlevel_raise",
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=AgentCatOptions(
                enable_tracing=True,
                enable_tool_call_context=False,
                enable_report_missing=False,
            ),
        )
        set_server_tracking_data(server, data)
        override_lowlevel_mcp_server(server, data)
        handler = server.request_handlers[CallToolRequest]

        with patch(
            "agentcat.modules.overrides.mcp_server.event_queue"
        ) as mock_event_queue:
            with pytest.raises(RuntimeError, match="handler exploded"):
                await handler(_make_request())

        events = [
            call.args[1] for call in mock_event_queue.publish_event.call_args_list
        ]
        call_events = [e for e in events if e.event_type == "mcp:tools/call"]
        assert call_events, "tools/call event not published"
        event = call_events[0]

        assert event.is_error is True
        error = event.error
        assert error["message"] == "handler exploded"
        assert error["type"] == "RuntimeError"
        assert error["platform"] == "python"
        assert error["frames"], "expected structured stack frames"
        assert any(
            frame["function"] == "exploding_handler" for frame in error["frames"]
        )
        assert "handler exploded" in error["stack"]

    async def test_success_response_has_no_error(self):
        server = LowLevelServer("lowlevel-ok-server")

        @server.list_tools()
        async def list_tools():
            return []

        @server.call_tool()
        async def call_tool(name, arguments):
            return []

        data = AgentCatData(
            project_id="test_project",
            session_id="ses_lowlevel_ok",
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=AgentCatOptions(
                enable_tracing=True,
                enable_tool_call_context=False,
                enable_report_missing=False,
            ),
        )
        set_server_tracking_data(server, data)
        override_lowlevel_mcp_server(server, data)
        handler = server.request_handlers[CallToolRequest]

        with patch(
            "agentcat.modules.overrides.mcp_server.event_queue"
        ) as mock_event_queue:
            await handler(_make_request(name="ok"))

        events = [
            call.args[1] for call in mock_event_queue.publish_event.call_args_list
        ]
        call_events = [e for e in events if e.event_type == "mcp:tools/call"]
        assert call_events
        assert call_events[0].is_error is False
        assert call_events[0].error is None
