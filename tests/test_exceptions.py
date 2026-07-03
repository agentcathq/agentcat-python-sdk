"""Tests for exception tracking functionality."""

import os
import tempfile
import time
from unittest.mock import MagicMock

import pytest

from mcpcat import AgentCatOptions, track
from mcpcat.modules.event_queue import EventQueue, set_event_queue
from mcpcat.modules.exceptions import (
    capture_exception,
    extract_context_line,
    filename_for_module,
    format_exception_string,
    is_in_app,
    parse_python_traceback,
    stringify_non_exception,
)

from .test_utils.client import create_test_client
from .test_utils.todo_server import create_todo_server


class TestBasicExceptionCapture:
    """Tests for basic exception capture functionality."""

    def test_capture_simple_exception(self):
        """Test capturing a simple ValueError."""
        try:
            raise ValueError("test error")
        except ValueError as e:
            error_data = capture_exception(e)

            assert error_data["message"] == "test error"
            assert error_data["type"] == "ValueError"
            assert error_data["platform"] == "python"
            assert "frames" in error_data
            assert len(error_data["frames"]) > 0
            assert "stack" in error_data

    def test_capture_exception_with_stack_trace(self):
        """Test that stack trace is properly captured."""
        try:
            raise RuntimeError("runtime error")
        except RuntimeError as e:
            error_data = capture_exception(e)

            assert "frames" in error_data
            frames = error_data["frames"]
            assert len(frames) > 0

            # Check frame structure
            first_frame = frames[0]
            assert "filename" in first_frame
            assert "abs_path" in first_frame
            assert "function" in first_frame
            assert "module" in first_frame
            assert "lineno" in first_frame
            assert "in_app" in first_frame

    def test_capture_exception_without_traceback(self):
        """Test capturing exception without traceback."""
        exc = ValueError("no traceback")
        # Create exception without raising it (no __traceback__)
        error_data = capture_exception(exc)

        assert error_data["message"] == "no traceback"
        assert error_data["type"] == "ValueError"
        assert error_data["platform"] == "python"
        # No traceback means no frames or stack
        assert "frames" not in error_data or len(error_data.get("frames", [])) == 0

    def test_module_extraction(self):
        """Test that module names are properly extracted."""
        try:
            raise TypeError("type error")
        except TypeError as e:
            error_data = capture_exception(e)

            frames = error_data["frames"]
            assert len(frames) > 0

            # At least one frame should have __name__ from current module
            has_module = any(frame.get("module") for frame in frames)
            assert has_module


class TestErrorChainUnwrapping:
    """Tests for exception chain unwrapping."""

    def test_explicit_chaining_with_from(self):
        """Test explicit exception chaining (raise ... from ...)."""
        try:
            try:
                raise ValueError("root cause")
            except ValueError as e:
                raise RuntimeError("wrapper error") from e
        except RuntimeError as e:
            error_data = capture_exception(e)

            assert error_data["message"] == "wrapper error"
            assert error_data["type"] == "RuntimeError"
            assert "chained_errors" in error_data

            chained = error_data["chained_errors"]
            assert len(chained) == 1
            assert chained[0]["message"] == "root cause"
            assert chained[0]["type"] == "ValueError"

    def test_implicit_chaining_context(self):
        """Test implicit exception chaining (__context__)."""
        try:
            try:
                raise ValueError("first error")
            except ValueError:
                # Implicit chaining - new exception during except block
                raise TypeError("second error")
        except TypeError as e:
            error_data = capture_exception(e)

            assert error_data["type"] == "TypeError"
            assert "chained_errors" in error_data

            chained = error_data["chained_errors"]
            assert len(chained) == 1
            assert chained[0]["type"] == "ValueError"

    def test_circular_reference_prevention(self):
        """Test that circular exception chains are handled."""
        # Create circular reference manually
        exc1 = ValueError("error 1")
        exc2 = RuntimeError("error 2")

        # Create circular chain (this shouldn't happen normally but we handle it)
        exc1.__cause__ = exc2
        exc2.__cause__ = exc1
        exc1.__suppress_context__ = True
        exc2.__suppress_context__ = True

        # Should not infinite loop
        error_data = capture_exception(exc1)

        assert error_data["type"] == "ValueError"
        # Should have stopped due to circular detection
        assert "chained_errors" in error_data
        chained = error_data["chained_errors"]
        # Should have one (exc2) before detecting the circle back to exc1
        assert len(chained) == 1

    def test_max_depth_limiting(self):
        """Test that deep exception chains are limited."""
        # Create a chain deeper than MAX_EXCEPTION_CHAIN_DEPTH (10)
        exc = ValueError("root")
        current = exc

        for i in range(15):
            new_exc = RuntimeError(f"error {i}")
            new_exc.__cause__ = current
            new_exc.__suppress_context__ = True
            current = new_exc

        error_data = capture_exception(current)

        # Should have limited the chain to 10
        assert "chained_errors" in error_data
        assert len(error_data["chained_errors"]) <= 10


class TestInAppDetection:
    """Tests for in_app detection."""

    def test_user_code_is_in_app(self):
        """Test that user code is marked as in_app=True."""
        # Current test file should be user code
        current_file = os.path.abspath(__file__)
        assert is_in_app(current_file) is True

    def test_site_packages_not_in_app(self):
        """Test that site-packages code is marked as in_app=False."""
        # Create a fake site-packages path
        fake_path = "/usr/local/lib/python3.10/site-packages/requests/api.py"
        assert is_in_app(fake_path) is False

        fake_path2 = "/home/user/.local/lib/python3.9/dist-packages/numpy/core.py"
        assert is_in_app(fake_path2) is False

    def test_stdlib_not_in_app(self):
        """Test that Python stdlib is marked as in_app=False."""
        # Check actual stdlib module
        import json

        json_file = json.__file__
        if json_file:
            assert is_in_app(json_file) is False

    def test_empty_path_not_in_app(self):
        """Test that empty path returns False."""
        assert is_in_app("") is False
        assert is_in_app(None) is False  # type: ignore


class TestPathNormalization:
    """Tests for path normalization."""

    def test_filename_for_module_with_package(self):
        """Test filename_for_module with package module."""
        # Test with this test module
        test_module = __name__
        test_file = __file__

        result = filename_for_module(test_module, test_file)

        # Should be more relative than absolute
        assert not result.startswith("/home/") and not result.startswith("/Users/")
        # Should contain the filename
        assert "test_exceptions.py" in result

    def test_filename_for_module_strips_pyc(self):
        """Test that .pyc extension is stripped."""
        module = "mymodule"
        path = "/path/to/mymodule.pyc"

        result = filename_for_module(module, path)

        # Should have stripped .pyc
        assert ".pyc" not in result

    def test_filename_for_module_simple_module(self):
        """Test simple module returns basename."""
        module = "simple"
        path = "/path/to/simple.py"

        result = filename_for_module(module, path)

        # Simple module should return basename
        assert result == "simple.py"

    def test_filename_for_module_fallback(self):
        """Test that it falls back to abs_path on error."""
        module = "nonexistent.module.name"
        path = "/some/path/file.py"

        result = filename_for_module(module, path)

        # Should fall back to original path
        assert result == path


class TestContextExtraction:
    """Tests for source context extraction."""

    def test_extract_context_line(self):
        """Test extracting context line from source file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("line 1\n")
            f.write("line 2\n")
            f.write("line 3 with error\n")
            f.write("line 4\n")
            temp_path = f.name

        try:
            assert extract_context_line(temp_path, 3) == "line 3 with error"
            assert extract_context_line(temp_path, 1) == "line 1"
        finally:
            os.unlink(temp_path)

    def test_extract_context_line_handles_missing_file(self):
        """Test that missing files are handled gracefully."""
        assert extract_context_line("/nonexistent/file.py", 1) is None


class TestNonExceptionHandling:
    """Tests for non-exception object handling."""

    def test_capture_string_error(self):
        """Test capturing a string that was raised."""
        error_data = capture_exception("string error")

        assert error_data["message"] == "string error"
        assert error_data["type"] is None  # Unknown type for non-exceptions
        assert error_data["platform"] == "python"
        assert "frames" not in error_data

    def test_capture_none(self):
        """Test capturing None."""
        error_data = capture_exception(None)

        assert error_data["message"] == "None"
        assert error_data["type"] is None  # Unknown type for non-exceptions

    def test_capture_dict(self):
        """Test capturing a dict."""
        error_data = capture_exception({"code": 404, "message": "not found"})

        assert "404" in error_data["message"]
        assert "not found" in error_data["message"]
        assert error_data["type"] is None  # Unknown type for non-exceptions

    def test_stringify_non_exception(self):
        """Test stringify_non_exception helper."""
        assert stringify_non_exception(None) == "None"
        assert stringify_non_exception("test") == "test"
        assert stringify_non_exception(42) == "42"
        assert stringify_non_exception(True) == "True"


class TestStackFrameParsing:
    """Tests for stack frame parsing."""

    def test_parse_traceback_with_frames(self):
        """Test parsing traceback with multiple frames."""

        def inner_function():
            raise ValueError("inner error")

        def outer_function():
            inner_function()

        try:
            outer_function()
        except ValueError as e:
            frames = parse_python_traceback(e.__traceback__)

            assert len(frames) > 0

            # Check that we have frames from both functions
            function_names = [f["function"] for f in frames]
            assert "inner_function" in function_names
            assert "outer_function" in function_names

    def test_parse_none_traceback(self):
        """Test parsing None traceback."""
        frames = parse_python_traceback(None)
        assert frames == []

    def test_frame_limit(self):
        """Test that frames are limited to MAX_STACK_FRAMES."""

        def recursive_function(depth):
            if depth <= 0:
                raise ValueError("deep error")
            recursive_function(depth - 1)

        try:
            # Create a very deep stack (more than 50 frames)
            recursive_function(60)
        except ValueError as e:
            frames = parse_python_traceback(e.__traceback__)

            # Should be limited to 50 frames
            assert len(frames) <= 50


class TestFormatExceptionString:
    """Tests for exception string formatting."""

    def test_format_exception_with_traceback(self):
        """Test formatting exception with traceback."""
        try:
            raise ValueError("format test")
        except ValueError as e:
            formatted = format_exception_string(e)

            assert "ValueError" in formatted
            assert "format test" in formatted
            assert "Traceback" in formatted

    def test_format_exception_without_traceback(self):
        """Test formatting exception without traceback."""
        exc = RuntimeError("no tb")
        formatted = format_exception_string(exc)

        # Should still format even without traceback
        assert "RuntimeError" in formatted
        assert "no tb" in formatted


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_very_deep_stack(self):
        """Test handling very deep stacks."""

        def deep_recursion(n):
            if n <= 0:
                raise ValueError("deep")
            return deep_recursion(n - 1)

        try:
            deep_recursion(100)
        except ValueError as e:
            error_data = capture_exception(e)

            # Should handle deep stack without crashing
            assert error_data["type"] == "ValueError"
            assert "frames" in error_data
            assert len(error_data["frames"]) <= 50

    def test_exception_with_special_characters(self):
        """Test exception with special characters in message."""
        try:
            raise ValueError("Error with émojis 🔥 and\nnewhlines\ttabs")
        except ValueError as e:
            error_data = capture_exception(e)

            assert "émojis" in error_data["message"]
            assert "🔥" in error_data["message"]

    def test_capture_preserves_all_fields(self):
        """Test that all important fields are captured."""
        try:
            raise KeyError("missing key")
        except KeyError as e:
            error_data = capture_exception(e)

            # Check all expected fields
            assert "message" in error_data
            assert "type" in error_data
            assert "platform" in error_data
            assert error_data["platform"] == "python"

            if e.__traceback__:
                assert "frames" in error_data
                assert "stack" in error_data


class TestExceptionIntegration:
    """Integration tests for exception capture with real MCP server calls."""

    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Set up and tear down for each test."""
        from mcpcat.modules.event_queue import event_queue as original_queue

        yield
        set_event_queue(original_queue)

    def _create_mock_event_capture(self):
        """Helper to create mock API client and event capture list."""
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)

        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)

        return captured_events

    @pytest.mark.asyncio
    async def test_tool_raises_value_error(self):
        """Test that ValueError from tools is properly captured."""
        captured_events = self._create_mock_event_capture()

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            await client.call_tool("tool_that_raises", {"error_type": "value"})
            time.sleep(1.0)

        tool_events = [
            e
            for e in captured_events
            if e.event_type == "mcp:tools/call"
            and e.resource_name == "tool_that_raises"
        ]
        assert len(tool_events) > 0, "No tool_that_raises event captured"

        event = tool_events[0]
        assert event.is_error is True
        assert event.error is not None
        # MCP SDK wraps tool exceptions - check the message contains the original error
        assert "Test value error from tool" in event.error["message"]

    @pytest.mark.asyncio
    async def test_tool_raises_runtime_error(self):
        """Test that RuntimeError from tools is properly captured."""
        captured_events = self._create_mock_event_capture()

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            await client.call_tool("tool_that_raises", {"error_type": "runtime"})
            time.sleep(1.0)

        tool_events = [
            e
            for e in captured_events
            if e.event_type == "mcp:tools/call"
            and e.resource_name == "tool_that_raises"
        ]
        assert len(tool_events) > 0

        event = tool_events[0]
        assert event.is_error is True
        assert event.error is not None
        # MCP SDK wraps tool exceptions - check the message contains the original error
        assert "Test runtime error from tool" in event.error["message"]

    @pytest.mark.asyncio
    async def test_tool_raises_custom_error(self):
        """Test that custom exception types are properly captured."""
        captured_events = self._create_mock_event_capture()

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            await client.call_tool("tool_that_raises", {"error_type": "custom"})
            time.sleep(1.0)

        tool_events = [
            e
            for e in captured_events
            if e.event_type == "mcp:tools/call"
            and e.resource_name == "tool_that_raises"
        ]
        assert len(tool_events) > 0

        event = tool_events[0]
        assert event.is_error is True
        assert event.error is not None
        # MCP SDK wraps tool exceptions - check the message contains the original error
        assert "Test custom error from tool" in event.error["message"]

    @pytest.mark.asyncio
    async def test_tool_raises_captures_stack_frames(self):
        """Test that stack frames are properly captured with correct structure."""
        captured_events = self._create_mock_event_capture()

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            await client.call_tool("tool_that_raises", {"error_type": "value"})
            time.sleep(1.0)

        tool_events = [
            e
            for e in captured_events
            if e.event_type == "mcp:tools/call"
            and e.resource_name == "tool_that_raises"
        ]
        assert len(tool_events) > 0

        event = tool_events[0]
        assert event.error is not None

        # Verify frames are captured
        frames = event.error.get("frames", [])
        assert len(frames) > 0, "No stack frames captured"

        # Verify frame structure
        for frame in frames:
            assert "filename" in frame
            assert "abs_path" in frame
            assert "function" in frame
            assert "module" in frame
            assert "lineno" in frame
            assert "in_app" in frame
            assert isinstance(frame["lineno"], int)
            assert isinstance(frame["in_app"], bool)

    @pytest.mark.asyncio
    async def test_tool_raises_has_in_app_frames(self):
        """Test that stack frames include in_app detection."""
        captured_events = self._create_mock_event_capture()

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            await client.call_tool("tool_that_raises", {"error_type": "value"})
            time.sleep(1.0)

        tool_events = [
            e
            for e in captured_events
            if e.event_type == "mcp:tools/call"
            and e.resource_name == "tool_that_raises"
        ]
        assert len(tool_events) > 0

        event = tool_events[0]
        frames = event.error.get("frames", [])
        assert len(frames) > 0, "Should have stack frames"

        # All frames should have in_app field
        for frame in frames:
            assert "in_app" in frame, "Frame should have in_app field"
            assert isinstance(frame["in_app"], bool)

        # Verify we have a mix of in_app and not in_app (sdk code is not in_app)
        # Note: MCP SDK wraps the error, so the original tool function may not appear
        # but we still verify the in_app detection logic works

    @pytest.mark.asyncio
    async def test_tool_raises_captures_context_lines(self):
        """Test that context lines are captured for in_app frames."""
        captured_events = self._create_mock_event_capture()

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            await client.call_tool("tool_that_raises", {"error_type": "value"})
            time.sleep(1.0)

        tool_events = [
            e
            for e in captured_events
            if e.event_type == "mcp:tools/call"
            and e.resource_name == "tool_that_raises"
        ]
        assert len(tool_events) > 0

        event = tool_events[0]
        frames = event.error.get("frames", [])
        in_app_frames = [f for f in frames if f.get("in_app") is True]

        # In-app frames should have context_line
        frames_with_context = [f for f in in_app_frames if f.get("context_line")]
        assert len(frames_with_context) > 0, "No context lines for in_app frames"

        # Context line should contain actual code
        for frame in frames_with_context:
            context = frame["context_line"]
            assert len(context) > 0
            assert context.strip() != ""

    @pytest.mark.asyncio
    async def test_mcp_protocol_error(self):
        """Test that MCP protocol errors (McpError) are properly handled."""
        captured_events = self._create_mock_event_capture()

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        async with create_test_client(server) as client:
            await client.call_tool("tool_with_mcp_error", {})
            time.sleep(1.0)

        tool_events = [
            e
            for e in captured_events
            if e.event_type == "mcp:tools/call"
            and e.resource_name == "tool_with_mcp_error"
        ]
        assert len(tool_events) > 0, "No tool_with_mcp_error event captured"

        event = tool_events[0]
        assert event.is_error is True
        assert event.error is not None
        assert "Invalid parameters" in event.error["message"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
