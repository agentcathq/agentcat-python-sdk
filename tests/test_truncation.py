"""Unit tests for the truncation module."""

import time
from unittest.mock import MagicMock, patch

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.event_queue import EventQueue, set_event_queue
from agentcat.modules.truncation import (
    _truncate_value,
    truncate_event,
    MAX_STRING_BYTES,
    MAX_DEPTH,
    MAX_BREADTH,
    MAX_EVENT_BYTES,
    MIN_DEPTH,
    TRUNCATABLE_FIELDS,
)
from agentcat.types import UnredactedEvent


def _make_event(**overrides) -> UnredactedEvent:
    """Helper to build a minimal valid event with optional overrides."""
    defaults = {
        "event_type": "mcp:tools/call",
        "resource_name": "test_tool",
        "session_id": "test-session-id",
    }
    defaults.update(overrides)
    return UnredactedEvent(**defaults)


class TestStringTruncation:
    """String values over MAX_STRING_BYTES are truncated."""

    def test_short_string_unchanged(self):
        assert _truncate_value("hello") == "hello"

    def test_string_at_limit_unchanged(self):
        s = "a" * MAX_STRING_BYTES
        assert _truncate_value(s) == s

    def test_string_over_limit_truncated_with_marker(self):
        original = "a" * (MAX_STRING_BYTES + 500)
        result = _truncate_value(original)
        byte_size = len(original.encode("utf-8"))
        expected_suffix = f"[string truncated by AgentCat from {byte_size} bytes]"
        assert result.endswith(expected_suffix)
        assert len(result.encode("utf-8")) < len(original.encode("utf-8"))

    def test_utf8_multibyte_truncated_by_bytes_no_broken_codepoints(self):
        # Each emoji is 4 bytes. 2560 emojis = 10,240 bytes = exactly at limit
        s = "\U0001f600" * 2561  # 10,244 bytes — just over limit
        result = _truncate_value(s)
        byte_size = len(s.encode("utf-8"))
        assert f"[string truncated by AgentCat from {byte_size} bytes]" in result
        # Verify valid UTF-8 — would raise if broken
        result.encode("utf-8")


class TestDepthLimiting:
    """Structures nested beyond MAX_DEPTH are replaced with a marker."""

    def test_at_max_depth_passes_through(self):
        # Build nested dict exactly MAX_DEPTH levels deep
        value = "leaf"
        for _ in range(MAX_DEPTH):
            value = {"nested": value}
        result = _truncate_value(value)
        # Should reach the leaf string
        inner = result
        for _ in range(MAX_DEPTH):
            inner = inner["nested"]
        assert inner == "leaf"

    def test_exceeds_max_depth_replaced_with_marker(self):
        # Build nested dict MAX_DEPTH + 2 levels deep
        value = "leaf"
        for _ in range(MAX_DEPTH + 2):
            value = {"nested": value}
        result = _truncate_value(value)
        # Walk to depth MAX_DEPTH — dict at limit is preserved
        inner = result
        for _ in range(MAX_DEPTH):
            inner = inner["nested"]
        # The dict at the limit is kept, but its nested children are markers
        assert isinstance(inner, dict)
        assert inner["nested"] == f"[nested content truncated by AgentCat at depth {MAX_DEPTH}]"

    def test_max_depth_zero_preserves_top_level_mapping(self):
        value = {
            "event_type": "mcp:tools/call",
            "parameters": {"nested": {"x": "y"}},
        }
        result = _truncate_value(value, max_depth=0)
        assert isinstance(result, dict)
        assert result["event_type"] == "mcp:tools/call"
        assert result["parameters"] == "[nested content truncated by AgentCat at depth 0]"


class TestBreadthLimiting:
    """Dicts/lists with more than MAX_BREADTH items are trimmed."""

    def test_dict_at_breadth_limit_unchanged(self):
        d = {f"key_{i}": i for i in range(MAX_BREADTH)}
        result = _truncate_value(d)
        assert len(result) == MAX_BREADTH

    def test_dict_over_breadth_limit_trimmed_with_marker(self):
        d = {f"key_{i}": i for i in range(MAX_BREADTH + 5)}
        result = _truncate_value(d)
        assert len(result) == MAX_BREADTH + 1  # MAX_BREADTH items + 1 marker
        assert "__truncated__" in result
        assert "5 more items truncated by AgentCat" in result["__truncated__"]

    def test_list_at_breadth_limit_unchanged(self):
        lst = list(range(MAX_BREADTH))
        result = _truncate_value(lst)
        assert len(result) == MAX_BREADTH

    def test_list_over_breadth_limit_trimmed_with_marker(self):
        lst = list(range(MAX_BREADTH + 30))
        result = _truncate_value(lst)
        assert len(result) == MAX_BREADTH + 1  # MAX_BREADTH items + 1 marker string
        assert "30 more items truncated by AgentCat" in result[-1]


class TestCircularReferences:
    """Circular references are detected and replaced with a marker."""

    def test_self_referencing_dict_replaced(self):
        d: dict = {"key": "value"}
        d["self"] = d
        result = _truncate_value(d)
        assert result["key"] == "value"
        assert result["self"] == "[circular reference]"

    def test_same_object_at_two_positions_not_falsely_flagged(self):
        shared = {"data": "hello"}
        parent = {"a": shared, "b": shared}
        result = _truncate_value(parent)
        # Both should resolve to the actual value, not circular marker
        assert result["a"] == {"data": "hello"}
        assert result["b"] == {"data": "hello"}


class TestTruncateEventFastPath:
    """Events under MAX_EVENT_BYTES are returned unchanged."""

    def test_small_event_returned_unchanged(self):
        event = _make_event(parameters={"key": "small"})
        result = truncate_event(event)
        assert result is event  # Same object — no copy made

    def test_none_returns_none(self):
        assert truncate_event(None) is None


class TestTruncateEventOversized:
    """Events over MAX_EVENT_BYTES are truncated."""

    def test_large_string_in_parameters_truncated(self):
        big = "x" * 200_000  # ~200 KB string
        event = _make_event(parameters={"data": big})
        result = truncate_event(event)
        # Result should be a different object
        assert result is not event
        # The big string should be truncated
        assert len(result.parameters["data"]) < len(big)
        assert "truncated by AgentCat" in result.parameters["data"]

    def test_large_string_in_response_truncated(self):
        big = "x" * 200_000
        event = _make_event(response={"output": big})
        result = truncate_event(event)
        assert "truncated by AgentCat" in result.response["output"]

    def test_large_string_in_error_truncated(self):
        big = "x" * 200_000
        event = _make_event(error={"message": "fail", "stack": big})
        result = truncate_event(event)
        assert "truncated by AgentCat" in result.error["stack"]

    def test_large_identify_data_truncated(self):
        big = "x" * 200_000
        event = _make_event(identify_data={"bio": big})
        result = truncate_event(event)
        assert "truncated by AgentCat" in result.identify_data["bio"]

    def test_original_event_not_mutated(self):
        big = "x" * 200_000
        event = _make_event(parameters={"data": big})
        original_data = event.parameters["data"]
        truncate_event(event)
        assert event.parameters["data"] == original_data


class TestSizeGuarantee:
    """Truncated events are guaranteed to be <= MAX_EVENT_BYTES."""

    def test_single_large_string_under_limit(self):
        big = "x" * 200_000
        event = _make_event(parameters={"data": big})
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_many_large_strings_under_limit(self):
        """20 strings of 10 KB each = 200 KB of strings before truncation."""
        params = {f"key_{i}": "x" * 20_000 for i in range(20)}
        event = _make_event(parameters=params)
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_deeply_nested_wide_structure_under_limit(self):
        """Deeply nested + wide structure that exceeds 100 KB."""
        value = {f"k{i}": "x" * 5_000 for i in range(15)}
        for _ in range(6):
            value = {f"level_{i}": value for i in range(5)}
        event = _make_event(parameters=value)
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_depth_reduces_progressively(self):
        """Verify depth reduction kicks in when first pass isn't enough."""
        # Build a structure that's over 100 KB even after depth=5 truncation
        # 20 keys * 10 KB string = 200 KB at each level, nested 5 deep
        level = {f"k{i}": "x" * 10_000 for i in range(20)}
        for _ in range(5):
            level = {"nested": level, "extra": "x" * 10_000}
        event = _make_event(parameters=level)
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_1mb_single_string_under_limit(self):
        """A single 1 MB string is truncated to fit."""
        big = "x" * 1_048_576
        event = _make_event(parameters={"data": big})
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_multiple_1mb_strings_under_limit(self):
        """Multiple 1 MB strings across fields all fit after truncation."""
        big = "x" * 1_048_576
        event = _make_event(
            user_intent=big,
            parameters={"a": big, "b": big},
            response={"out": big},
        )
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_extreme_breadth_1000_keys_under_limit(self):
        """1000 keys with moderate values exercises breadth reduction."""
        params = {f"key_{i}": "x" * 500 for i in range(1000)}
        event = _make_event(parameters=params)
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES


class TestTruncateEventErrorHandling:
    """Truncation failures return the original event."""

    def test_exception_during_truncation_returns_original(self):
        big = "x" * 200_000
        event = _make_event(parameters={"data": big})
        with patch(
            "agentcat.modules.truncation._truncate_value",
            side_effect=RuntimeError("boom"),
        ):
            result = truncate_event(event)
        # Should return original event, not crash
        assert result is event


class TestPipelineIntegration:
    """Truncation runs after sanitization in the event pipeline."""

    def test_truncation_is_imported_in_event_queue(self):
        """Verify truncate_event is used in event_queue module."""
        import inspect
        from agentcat.modules.event_queue import EventQueue
        source = inspect.getsource(EventQueue._process_event)
        assert "truncate_event" in source


class TestTruncationWithTodoServer:
    """Integration tests: oversized tool calls through the real todo server are truncated."""

    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        from agentcat.modules.event_queue import event_queue as original_queue
        yield
        set_event_queue(original_queue)

    def _capture_setup(self):
        mock_api_client = MagicMock()
        captured_events = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)
        test_queue = EventQueue(api_client=mock_api_client)
        set_event_queue(test_queue)
        return captured_events

    @pytest.mark.asyncio
    async def test_oversized_parameter_is_truncated(self):
        """A tool call with a >100 KB parameter string is truncated in the captured event."""
        from .test_utils.client import create_test_client
        from .test_utils.todo_server import create_todo_server

        captured_events = self._capture_setup()

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        # Use varied text so the sanitizer doesn't flag it as binary data
        chunk = "The quick brown fox jumps over the lazy dog. "
        oversized_text = chunk * (200_000 // len(chunk) + 1)  # ~200 KB

        async with create_test_client(server) as client:
            await client.call_tool("add_todo", {"text": oversized_text})
            time.sleep(1.0)

        tool_events = [
            e for e in captured_events if e.event_type == "mcp:tools/call"
        ]
        assert len(tool_events) > 0, "No tool call event captured"

        event = tool_events[0]

        # The parameter string should have been truncated
        captured_text = event.parameters["arguments"]["text"]
        assert len(captured_text) < len(oversized_text)
        assert "truncated by AgentCat" in captured_text

        # Whole event must fit within the size limit
        event_bytes = len(event.model_dump_json().encode("utf-8"))
        assert event_bytes <= MAX_EVENT_BYTES

    @pytest.mark.asyncio
    async def test_oversized_response_is_truncated(self):
        """A tool that returns a >100 KB response has its event response truncated."""
        from .test_utils.client import create_test_client
        from .test_utils.todo_server import create_todo_server

        captured_events = self._capture_setup()

        server = create_todo_server()
        options = AgentCatOptions(enable_tracing=True)
        track(server, "test_project", options)

        # Add many todos so list_todos returns a large response
        async with create_test_client(server) as client:
            for i in range(500):
                await client.call_tool("add_todo", {"text": f"Todo item number {i} with padding {'z' * 200}"})

            # list_todos returns all of them in one string
            await client.call_tool("list_todos")
            time.sleep(1.0)

        list_events = [
            e
            for e in captured_events
            if e.event_type == "mcp:tools/call" and e.resource_name == "list_todos"
        ]
        assert len(list_events) > 0, "No list_todos event captured"

        event = list_events[0]
        event_bytes = len(event.model_dump_json().encode("utf-8"))
        assert event_bytes <= MAX_EVENT_BYTES


class TestMegabyteStrings:
    """1 MB strings in various fields are truncated to fit under the limit."""

    ONE_MB = "x" * 1_048_576  # 1 MB

    def test_1mb_user_intent(self):
        event = _make_event(user_intent=self.ONE_MB)
        result = truncate_event(event)
        assert result is not event
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES
        assert "truncated by AgentCat" in result.user_intent

    def test_1mb_in_parameters(self):
        event = _make_event(parameters={"context": self.ONE_MB})
        result = truncate_event(event)
        assert result is not event
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES
        assert "truncated by AgentCat" in result.parameters["context"]

    def test_1mb_in_response(self):
        event = _make_event(response={"output": self.ONE_MB})
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES
        assert "truncated by AgentCat" in result.response["output"]

    def test_1mb_in_error(self):
        event = _make_event(error={"message": "fail", "stack": self.ONE_MB})
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES
        assert "truncated by AgentCat" in result.error["stack"]

    def test_1mb_in_all_fields_simultaneously(self):
        event = _make_event(
            user_intent=self.ONE_MB,
            parameters={"context": self.ONE_MB},
            response={"output": self.ONE_MB},
            error={"message": "fail", "stack": self.ONE_MB},
        )
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES


class TestManyKeysRegression:
    """Regression tests for the depth=0 crash bug.

    Events with many moderate-sized keys used to cause depth to reach 0,
    which replaced dict-typed fields with string markers and crashed
    model_validate(). The fix keeps depth >= MIN_DEPTH and uses breadth
    reduction as a fallback.
    """

    def test_500_keys_x_50kb_stays_under_limit(self):
        """500 keys * 50 KB = ~25 MB raw — exercises aggressive truncation."""
        params = {f"key_{i}": "x" * 50_000 for i in range(500)}
        event = _make_event(parameters=params)
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_200_keys_x_1kb_stays_under_limit(self):
        """200 keys * 1 KB = 200 KB — just over the limit, previously crashed."""
        params = {f"key_{i}": "x" * 1_000 for i in range(200)}
        event = _make_event(parameters=params)
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_200_keys_x_10kb_stays_under_limit(self):
        """200 keys * 10 KB = 2 MB — needs multiple passes."""
        params = {f"key_{i}": "x" * 10_000 for i in range(200)}
        event = _make_event(parameters=params)
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_dict_fields_remain_dicts_after_truncation(self):
        """Verify parameters/response/error/identify_data stay as dicts, not strings."""
        params = {f"key_{i}": "x" * 1_000 for i in range(200)}
        event = _make_event(
            parameters=params,
            response={"output": "x" * 50_000},
            error={"message": "fail", "stack": "x" * 50_000},
            identify_data={"bio": "x" * 50_000},
        )
        result = truncate_event(event)
        assert isinstance(result.parameters, dict), "parameters should remain a dict"
        assert isinstance(result.response, dict), "response should remain a dict"
        assert isinstance(result.error, dict), "error should remain a dict"
        assert isinstance(result.identify_data, dict), "identify_data should remain a dict"
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_many_keys_across_multiple_dict_fields(self):
        """Many keys spread across parameters + response + error."""
        params = {f"p_{i}": "x" * 2_000 for i in range(100)}
        resp = {f"r_{i}": "x" * 2_000 for i in range(100)}
        err = {f"e_{i}": "x" * 2_000 for i in range(100)}
        event = _make_event(parameters=params, response=resp, error=err)
        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES
        assert isinstance(result.parameters, dict)
        assert isinstance(result.response, dict)
        assert isinstance(result.error, dict)

    def test_top_level_fields_not_dropped_under_extreme_key_pressure(self):
        """Top-level event metadata should survive aggressive truncation."""
        long_key = "k" * 20_000
        params = {f"{long_key}{i}": "x" for i in range(20)}
        event = _make_event(
            event_type="mcp:tools/call",
            resource_name="test_tool",
            session_id="test-session-id",
            parameters=params,
        )

        result = truncate_event(event)
        result_bytes = len(result.model_dump_json().encode("utf-8"))

        assert result_bytes <= MAX_EVENT_BYTES
        assert result.event_type == "mcp:tools/call"
        assert result.resource_name == "test_tool"
        assert isinstance(result.parameters, dict)
        assert len(result.parameters) > 0


class TestMetadataProtection:
    """Top-level metadata fields must never be truncated, even under extreme payload pressure."""

    def test_metadata_fields_preserved_when_payload_forces_extreme_truncation(self):
        """Top-level metadata strings must never be truncated, even with huge payloads."""
        event = _make_event(
            event_type="mcp:tools/call",
            resource_name="my_important_tool",
            session_id="sess-12345",
            actor_id="actor-67890",
            user_intent="short intent",
            parameters={"data": "x" * 1_048_576},  # 1 MB forces aggressive truncation
        )
        result = truncate_event(event)

        # Metadata fields must be EXACTLY preserved
        assert result.event_type == "mcp:tools/call"
        assert result.resource_name == "my_important_tool"
        assert result.session_id == "sess-12345"
        assert result.actor_id == "actor-67890"

        # user_intent IS truncatable, but "short intent" is small enough to survive
        assert result.user_intent == "short intent"

        # Payload was truncated
        assert "truncated by AgentCat" in result.parameters["data"]

        # Still under size limit
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES

    def test_large_user_intent_truncated_but_metadata_preserved(self):
        """Large user_intent is truncated while metadata stays intact."""
        event = _make_event(
            event_type="mcp:tools/call",
            resource_name="my_tool",
            user_intent="x" * 200_000,
            parameters={"key": "value"},
        )
        result = truncate_event(event)
        assert result.event_type == "mcp:tools/call"
        assert result.resource_name == "my_tool"
        assert "truncated by AgentCat" in result.user_intent
        result_bytes = len(result.model_dump_json().encode("utf-8"))
        assert result_bytes <= MAX_EVENT_BYTES
