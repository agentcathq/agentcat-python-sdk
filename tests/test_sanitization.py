"""Unit tests for the sanitization module."""

import copy

import pytest

from agentcat.modules.sanitization import (
    sanitize_event,
    _scan_for_base64,
    _AUDIO_REDACTED,
    _BINARY_DATA_REDACTED,
    _BLOB_RESOURCE_REDACTED,
    _IMAGE_REDACTED,
    _unsupported_type_redacted,
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


def _large_base64(length: int = 20_000) -> str:
    """Return a valid base64-ish string of at least *length* chars."""
    # Repeating 'QUFB' (base64 for 'AAA') to reach desired length, then pad.
    unit = "QUFB"
    return (unit * ((length // len(unit)) + 1))[:length] + "=="


class TestResponseContentSanitization:
    """Tests 1–9: sanitization of response content blocks."""

    def test_mixed_content_only_non_text_replaced(self):
        """1. Mixed content (text + image + audio) — only non-text replaced."""
        event = _make_event(
            response={
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image", "data": "base64data", "mimeType": "image/png"},
                    {"type": "audio", "data": "audiodata", "mimeType": "audio/wav"},
                ],
            },
        )
        result = sanitize_event(event)
        content = result.response["content"]

        assert content[0] == {"type": "text", "text": "hello"}
        assert content[1] == {"type": "text", "text": _IMAGE_REDACTED}
        assert content[2] == {"type": "text", "text": _AUDIO_REDACTED}

    def test_text_only_unchanged(self):
        """2. Text-only response — unchanged."""
        event = _make_event(
            response={
                "content": [
                    {"type": "text", "text": "just text"},
                ],
            },
        )
        result = sanitize_event(event)
        assert result.response["content"] == [{"type": "text", "text": "just text"}]

    def test_embedded_resource_with_blob_redacted(self):
        """3. EmbeddedResource with blob — redacted."""
        event = _make_event(
            response={
                "content": [
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "file://image.png",
                            "blob": "base64blobdata",
                            "mimeType": "image/png",
                        },
                    },
                ],
            },
        )
        result = sanitize_event(event)
        assert result.response["content"] == [
            {"type": "text", "text": _BLOB_RESOURCE_REDACTED}
        ]

    def test_embedded_resource_with_text_unchanged(self):
        """4. EmbeddedResource with text — unchanged."""
        block = {
            "type": "resource",
            "resource": {
                "uri": "file://readme.txt",
                "text": "This is a readme",
            },
        }
        event = _make_event(response={"content": [block]})
        result = sanitize_event(event)
        assert result.response["content"] == [block]

    def test_unknown_content_type_redacted_with_type_name(self):
        """5. Unknown content type — redacted with type name."""
        event = _make_event(
            response={
                "content": [
                    {"type": "video", "data": "videodata"},
                ],
            },
        )
        result = sanitize_event(event)
        assert result.response["content"] == [
            {"type": "text", "text": _unsupported_type_redacted("video")}
        ]

    def test_resource_link_passed_through(self):
        """6. resource_link — passed through."""
        block = {"type": "resource_link", "uri": "file://data.csv"}
        event = _make_event(response={"content": [block]})
        result = sanitize_event(event)
        assert result.response["content"] == [block]

    def test_response_without_content_array_unchanged(self):
        """7. Response without content array — unchanged."""
        event = _make_event(response={"isError": False})
        result = sanitize_event(event)
        assert result.response == {"isError": False}

    def test_none_response_no_error(self):
        """8. None response — no error."""
        event = _make_event(response=None)
        result = sanitize_event(event)
        assert result.response is None

    def test_large_base64_in_structured_content_redacted(self):
        """9. Large base64 in structuredContent — redacted via param scanner."""
        big = _large_base64()
        event = _make_event(
            response={
                "content": [{"type": "text", "text": "ok"}],
                "structured_content": {"nested": {"data": big}},
            },
        )
        result = sanitize_event(event)
        assert result.response["structured_content"]["nested"]["data"] == _BINARY_DATA_REDACTED
        # text content untouched
        assert result.response["content"][0]["text"] == "ok"


class TestParameterScanning:
    """Tests 10–17: base64 scanning in event parameters."""

    def test_small_strings_unchanged(self):
        """10. Small strings — unchanged."""
        event = _make_event(parameters={"key": "small value"})
        result = sanitize_event(event)
        assert result.parameters == {"key": "small value"}

    def test_large_base64_redacted(self):
        """11. Large base64 (>10KB) — redacted."""
        big = _large_base64()
        event = _make_event(parameters={"file": big})
        result = sanitize_event(event)
        assert result.parameters["file"] == _BINARY_DATA_REDACTED

    def test_large_non_base64_unchanged(self):
        """12. Large non-base64 (>10KB) — unchanged."""
        large_text = "hello world! " * 2000  # ~26 KB, contains spaces
        event = _make_event(parameters={"essay": large_text})
        result = sanitize_event(event)
        assert result.parameters["essay"] == large_text

    def test_deeply_nested_large_base64_found(self):
        """13. Deeply nested large base64 — found and redacted."""
        big = _large_base64()
        event = _make_event(
            parameters={"a": {"b": {"c": {"d": big}}}},
        )
        result = sanitize_event(event)
        assert result.parameters["a"]["b"]["c"]["d"] == _BINARY_DATA_REDACTED

    def test_mixed_types_only_large_base64_redacted(self):
        """14. Mixed types — only large base64 redacted."""
        big = _large_base64()
        event = _make_event(
            parameters={
                "number": 42,
                "flag": True,
                "small": "abc",
                "blob": big,
            },
        )
        result = sanitize_event(event)
        assert result.parameters["number"] == 42
        assert result.parameters["flag"] is True
        assert result.parameters["small"] == "abc"
        assert result.parameters["blob"] == _BINARY_DATA_REDACTED

    def test_large_base64_inside_array_redacted(self):
        """15. Large base64 inside array — redacted."""
        big = _large_base64()
        event = _make_event(parameters={"items": ["keep", big, 99]})
        result = sanitize_event(event)
        assert result.parameters["items"] == ["keep", _BINARY_DATA_REDACTED, 99]

    def test_boundary_10240_redacted_10239_not(self):
        """16. Boundary: 10,240 chars → redacted; 10,239 → not redacted."""
        # Build strings of exact lengths from valid base64 chars
        base_char = "A"  # valid base64 character

        at_threshold = base_char * 10_240
        below_threshold = base_char * 10_239

        # Verify both match the base64 pattern
        assert _scan_for_base64(at_threshold) == _BINARY_DATA_REDACTED
        assert _scan_for_base64(below_threshold) == below_threshold

    def test_none_parameters_no_error(self):
        """17. None parameters — no error."""
        event = _make_event(parameters=None)
        result = sanitize_event(event)
        assert result.parameters is None


class TestResponseWideBase64Scanning:
    """Tests 20–22: base64 scanning across all response fields."""

    def test_camel_case_structured_content_scanned(self):
        """20. structuredContent (camelCase) — large base64 redacted."""
        big = _large_base64()
        event = _make_event(
            response={
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"payload": big},
            },
        )
        result = sanitize_event(event)
        assert result.response["structuredContent"]["payload"] == _BINARY_DATA_REDACTED
        assert result.response["content"][0]["text"] == "ok"

    def test_non_string_response_fields_pass_through(self):
        """21. Non-string fields (booleans, numbers) pass through unchanged."""
        event = _make_event(
            response={
                "content": [{"type": "text", "text": "ok"}],
                "isError": False,
                "is_error": False,
                "someCount": 42,
            },
        )
        result = sanitize_event(event)
        assert result.response["isError"] is False
        assert result.response["is_error"] is False
        assert result.response["someCount"] == 42

    def test_arbitrary_response_field_with_large_base64_scanned(self):
        """22. Arbitrary response field with large base64 — scanned and redacted."""
        big = _large_base64()
        event = _make_event(
            response={
                "content": [{"type": "text", "text": "ok"}],
                "customField": {"deep": {"blob": big}},
            },
        )
        result = sanitize_event(event)
        assert result.response["customField"]["deep"]["blob"] == _BINARY_DATA_REDACTED



class TestSanitizationIntegration:
    """Tests 18–19: end-to-end integration."""

    def test_full_event_both_response_and_params_sanitized(self):
        """18. Full event with both non-text response and large base64 params — both sanitized."""
        big = _large_base64()
        event = _make_event(
            response={
                "content": [
                    {"type": "text", "text": "result"},
                    {"type": "image", "data": "imgdata", "mimeType": "image/png"},
                ],
            },
            parameters={"input": "normal", "file": big},
        )
        result = sanitize_event(event)

        # Response sanitized
        assert result.response["content"][0] == {"type": "text", "text": "result"}
        assert result.response["content"][1] == {"type": "text", "text": _IMAGE_REDACTED}

        # Parameters sanitized
        assert result.parameters["input"] == "normal"
        assert result.parameters["file"] == _BINARY_DATA_REDACTED

    def test_original_event_not_mutated(self):
        """19. Original event not mutated."""
        big = _large_base64()
        original_response = {
            "content": [
                {"type": "image", "data": "imgdata", "mimeType": "image/png"},
            ],
        }
        original_params = {"blob": big}

        event = _make_event(
            response=copy.deepcopy(original_response),
            parameters=copy.deepcopy(original_params),
        )

        # Keep a reference to original data
        original_response_snapshot = copy.deepcopy(event.response)
        original_params_snapshot = copy.deepcopy(event.parameters)

        result = sanitize_event(event)

        # Result should be sanitized
        assert result.response["content"][0]["text"] == _IMAGE_REDACTED
        assert result.parameters["blob"] == _BINARY_DATA_REDACTED

        # Original event should be untouched
        assert event.response == original_response_snapshot
        assert event.parameters == original_params_snapshot
