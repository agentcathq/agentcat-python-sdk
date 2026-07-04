"""Tests for tag validation."""

from unittest.mock import patch

import pytest

from agentcat.modules.validation import (
    MAX_TAG_ENTRIES,
    MAX_TAG_KEY_LENGTH,
    MAX_TAG_VALUE_LENGTH,
    validate_tags,
)


@pytest.fixture(autouse=True)
def mock_log():
    with patch("agentcat.modules.validation.write_to_log") as mock:
        yield mock


class TestValidateTags:
    def test_passes_through_valid_tags_unchanged(self):
        tags = {"env": "production", "trace_id": "abc-123", "region": "us-east-1"}
        assert validate_tags(tags) == tags

    def test_returns_none_for_empty_dict(self):
        assert validate_tags({}) is None

    def test_drops_keys_with_invalid_characters(self, mock_log):
        tags = {"valid_key": "value", "invalid!key": "value", "good.key": "value"}
        assert validate_tags(tags) == {"valid_key": "value", "good.key": "value"}
        assert any("invalid!key" in call.args[0] for call in mock_log.call_args_list)

    def test_drops_keys_longer_than_max(self, mock_log):
        long_key = "a" * (MAX_TAG_KEY_LENGTH + 1)
        tags = {long_key: "value", "short": "value"}
        assert validate_tags(tags) == {"short": "value"}
        assert any("exceeds max length" in call.args[0] for call in mock_log.call_args_list)

    def test_drops_values_longer_than_max(self, mock_log):
        long_value = "a" * (MAX_TAG_VALUE_LENGTH + 1)
        tags = {"key1": long_value, "key2": "short"}
        assert validate_tags(tags) == {"key2": "short"}
        assert any("exceeds max length" in call.args[0] for call in mock_log.call_args_list)

    def test_drops_values_containing_newlines(self, mock_log):
        tags = {"key1": "has\nnewline", "key2": "clean"}
        assert validate_tags(tags) == {"key2": "clean"}
        assert any("newline" in call.args[0] for call in mock_log.call_args_list)

    def test_drops_non_string_values(self, mock_log):
        tags = {"key1": 123, "key2": "valid"}
        assert validate_tags(tags) == {"key2": "valid"}
        assert any("non-string" in call.args[0] for call in mock_log.call_args_list)

    def test_drops_empty_string_keys(self):
        tags = {"": "empty-key-value", "valid": "value"}
        assert validate_tags(tags) == {"valid": "value"}

    def test_keeps_only_first_50_entries_when_exceeding_limit(self, mock_log):
        tags = {f"key{i:03d}": f"value{i}" for i in range(60)}
        result = validate_tags(tags)
        assert result is not None
        assert len(result) == MAX_TAG_ENTRIES
        assert any("Dropping 10" in call.args[0] for call in mock_log.call_args_list)

    def test_returns_none_when_all_entries_invalid(self):
        tags = {"!!!": "value", "###": "value"}
        assert validate_tags(tags) is None

    def test_allows_special_chars_in_keys(self):
        tags = {
            "my.tag": "value",
            "my:tag": "value",
            "my-tag": "value",
            "my tag": "value",
            "$ai_trace_id": "trace-1",
            "my$key": "value",
        }
        assert validate_tags(tags) == tags
