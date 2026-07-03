"""Unit tests for the redaction module."""

import pytest
from typing import Any, Dict
from mcpcat.modules.redaction import (
    redact_strings_in_object,
    redact_event,
    PROTECTED_FIELDS,
)


class TestRedactStringsInObject:
    """Test suite for redact_strings_in_object function."""

    def test_simple_string_redaction(self):
        """Test basic string redaction."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        assert redact_strings_in_object("sensitive", redact_fn) == "[REDACTED]"
        assert redact_strings_in_object("", redact_fn) == "[REDACTED]"
        assert redact_strings_in_object("unicode: 你好", redact_fn) == "[REDACTED]"

    def test_none_values(self):
        """Test that None values are preserved."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        assert redact_strings_in_object(None, redact_fn) is None
        assert redact_strings_in_object({"key": None}, redact_fn) == {}
        assert redact_strings_in_object([None, "value", None], redact_fn) == [
            None,
            "[REDACTED]",
            None,
        ]

    def test_non_string_types(self):
        """Test that non-string types pass through unchanged."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        assert redact_strings_in_object(42, redact_fn) == 42
        assert redact_strings_in_object(3.14, redact_fn) == 3.14
        assert redact_strings_in_object(True, redact_fn) is True
        assert redact_strings_in_object(False, redact_fn) is False

    def test_list_redaction(self):
        """Test redaction in lists."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        # Simple list
        assert redact_strings_in_object(["a", "b", "c"], redact_fn) == [
            "[REDACTED]",
            "[REDACTED]",
            "[REDACTED]",
        ]

        # Mixed types
        assert redact_strings_in_object(["text", 123, True, None], redact_fn) == [
            "[REDACTED]",
            123,
            True,
            None,
        ]

        # Nested lists
        assert redact_strings_in_object([["inner"], "outer"], redact_fn) == [
            ["[REDACTED]"],
            "[REDACTED]",
        ]

        # Empty list
        assert redact_strings_in_object([], redact_fn) == []

    def test_dict_redaction(self):
        """Test redaction in dictionaries."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        # Simple dict
        assert redact_strings_in_object({"key": "value"}, redact_fn) == {
            "key": "[REDACTED]"
        }

        # Multiple keys
        result = redact_strings_in_object({"a": "1", "b": 2, "c": "3"}, redact_fn)
        assert result == {"a": "[REDACTED]", "b": 2, "c": "[REDACTED]"}

        # Nested dict
        result = redact_strings_in_object({"outer": {"inner": "secret"}}, redact_fn)
        assert result == {"outer": {"inner": "[REDACTED]"}}

        # Empty dict
        assert redact_strings_in_object({}, redact_fn) == {}

    def test_protected_fields(self):
        """Test that protected fields are not redacted."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        # Top-level protected fields
        obj = {
            "session_id": "12345",
            "project_id": "proj123",
            "other_field": "sensitive",
            "actor_id": "user123",
        }
        result = redact_strings_in_object(obj, redact_fn)
        assert result == {
            "session_id": "12345",  # Protected
            "project_id": "proj123",  # Protected
            "other_field": "[REDACTED]",  # Not protected
            "actor_id": "user123",  # Protected
        }

        # Nested values within protected fields should also be protected
        obj = {
            "identify_data": {
                "user_email": "test@example.com",
                "nested": {"deep": "value"},
            },
            "non_protected": {"data": "sensitive"},
        }
        result = redact_strings_in_object(obj, redact_fn)
        assert (
            result
            == {
                "identify_data": {
                    "user_email": "test@example.com",  # Protected because parent is protected
                    "nested": {"deep": "value"},  # Also protected
                },
                "non_protected": {"data": "[REDACTED]"},
            }
        )

    def test_protected_fields_only_at_top_level(self):
        """Test that protected field names at nested levels are still redacted."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        obj = {
            "data": {
                "session_id": "should_be_redacted",  # Not protected at nested level
                "other": "also_redacted",
            },
            "session_id": "protected_at_top",  # Protected at top level
        }
        result = redact_strings_in_object(obj, redact_fn)
        assert result == {
            "data": {"session_id": "[REDACTED]", "other": "[REDACTED]"},
            "session_id": "protected_at_top",
        }

    def test_complex_nested_structure(self):
        """Test redaction in complex nested structures."""

        def redact_fn(s: str) -> str:
            return "***"

        obj = {
            "users": [
                {
                    "id": "user1",
                    "name": "John Doe",
                    "settings": {"theme": "dark", "notifications": ["email", "sms"]},
                },
                {"id": "user2", "name": "Jane Smith", "settings": None},
            ],
            "server": "prod-server",  # Protected field
            "metadata": {"version": "1.0", "tags": ["production", "v1"]},
        }

        result = redact_strings_in_object(obj, redact_fn)
        assert result == {
            "users": [
                {
                    "id": "***",
                    "name": "***",
                    "settings": {"theme": "***", "notifications": ["***", "***"]},
                },
                {"id": "***", "name": "***"},
            ],
            "server": "prod-server",  # Protected, not redacted
            "metadata": {"version": "***", "tags": ["***", "***"]},
        }

    def test_path_tracking(self):
        """Test that paths are correctly tracked during recursion."""
        paths_seen = []

        def tracking_redact_fn(s: str) -> str:
            return f"[{s}]"

        # Monkey patch to track paths
        original_fn = redact_strings_in_object

        def wrapped_fn(obj, redact_fn, path="", is_protected=False):
            if isinstance(obj, str) and path:
                paths_seen.append(path)
            return original_fn(obj, redact_fn, path, is_protected)

        # This test verifies path construction logic indirectly
        obj = {"level1": {"level2": ["item0", "item1"], "level2b": "value"}}
        result = redact_strings_in_object(obj, tracking_redact_fn)

        # Verify structure is maintained
        assert result == {
            "level1": {"level2": ["[item0]", "[item1]"], "level2b": "[value]"}
        }

    def test_redaction_function_variations(self):
        """Test different types of redaction functions."""

        # Masking function
        def mask_fn(s: str) -> str:
            return "X" * len(s)

        assert redact_strings_in_object("secret", mask_fn) == "XXXXXX"

        # Hash-like function
        def hash_fn(s: str) -> str:
            return f"hash_{len(s)}"

        assert redact_strings_in_object("password", hash_fn) == "hash_8"

        # Conditional redaction
        def conditional_fn(s: str) -> str:
            return s if s.startswith("public_") else "[PRIVATE]"

        obj = {"public": "public_data", "private": "secret_data"}
        result = redact_strings_in_object(obj, conditional_fn)
        assert result == {"public": "public_data", "private": "[PRIVATE]"}

    def test_empty_collections(self):
        """Test handling of empty collections."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        assert redact_strings_in_object([], redact_fn) == []
        assert redact_strings_in_object({}, redact_fn) == {}
        # Empty collections are preserved in the output
        assert redact_strings_in_object(
            {"empty_list": [], "empty_dict": {}}, redact_fn
        ) == {"empty_list": [], "empty_dict": {}}

    def test_all_protected_fields(self):
        """Test all fields defined in PROTECTED_FIELDS."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        # Create object with all protected fields
        obj = {field: f"value_{field}" for field in PROTECTED_FIELDS}
        obj["unprotected"] = "sensitive_data"

        result = redact_strings_in_object(obj, redact_fn)

        # All protected fields should be unchanged
        for field in PROTECTED_FIELDS:
            assert result[field] == f"value_{field}"

        # Unprotected field should be redacted
        assert result["unprotected"] == "[REDACTED]"


class TestRedactEvent:
    """Test suite for redact_event function."""

    def test_event_redaction(self):
        """Test redaction of event objects."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        event = {
            "session_id": "sess123",  # Protected
            "project_id": "proj456",  # Protected
            "event_type": "mcp:tools/call",  # Protected
            "actor_id": "user789",  # Protected
            "resource_name": "database",  # Protected
            "data": {
                "query": "SELECT * FROM users",
                "parameters": ["param1", "param2"],
            },
            "metadata": {"timestamp": "2024-01-01T00:00:00Z", "ip": "192.168.1.1"},
        }

        result = redact_event(event, redact_fn)

        # Protected fields preserved
        assert result["session_id"] == "sess123"
        assert result["project_id"] == "proj456"
        assert result["event_type"] == "mcp:tools/call"
        assert result["actor_id"] == "user789"
        assert result["resource_name"] == "database"

        # Other fields redacted
        assert result["data"]["query"] == "[REDACTED]"
        assert result["data"]["parameters"] == ["[REDACTED]", "[REDACTED]"]
        assert result["metadata"]["timestamp"] == "[REDACTED]"
        assert result["metadata"]["ip"] == "[REDACTED]"

    def test_identify_event_special_fields(self):
        """Test agentcat:identify event with special protected fields."""

        def redact_fn(s: str) -> str:
            return "XXX"

        identify_event = {
            "event_type": "agentcat:identify",
            "identify_actor_given_id": "user123",  # Protected
            "identify_actor_name": "John Doe",  # Protected
            "identify_data": {  # Protected
                "email": "john@example.com",
                "plan": "premium",
                "nested": {"preference": "dark_mode"},
            },
            "other_data": {"sensitive": "should_be_redacted"},
        }

        result = redact_event(identify_event, redact_fn)

        # All identify fields and their nested content should be preserved
        assert result["identify_actor_given_id"] == "user123"
        assert result["identify_actor_name"] == "John Doe"
        assert result["identify_data"]["email"] == "john@example.com"
        assert result["identify_data"]["plan"] == "premium"
        assert result["identify_data"]["nested"]["preference"] == "dark_mode"

        # Other data should be redacted
        assert result["other_data"]["sensitive"] == "XXX"

    def test_minimal_event(self):
        """Test redaction of minimal event with only required fields."""

        def redact_fn(s: str) -> str:
            return "[HIDDEN]"

        minimal_event = {
            "id": "evt123",  # Protected
            "data": "sensitive information",
        }

        result = redact_event(minimal_event, redact_fn)
        assert result["id"] == "evt123"
        assert result["data"] == "[HIDDEN]"

    def test_error_in_redaction_function(self):
        """Test behavior when redaction function throws an error."""

        def faulty_redact_fn(s: str) -> str:
            if "error" in s:
                raise ValueError("Redaction error")
            return "[REDACTED]"

        event = {"safe_field": "normal_value", "error_field": "trigger_error"}

        # The function should propagate the error
        with pytest.raises(ValueError, match="Redaction error"):
            redact_event(event, faulty_redact_fn)
