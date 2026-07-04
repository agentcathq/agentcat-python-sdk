"""Unit tests for context_parameters module."""

import pytest
from copy import deepcopy
from typing import Any

from agentcat.modules.context_parameters import (
    add_context_parameter_to_tools,
    add_context_parameter_to_schema,
)


class TestAddContextParameterToSchema:
    """Unit tests for add_context_parameter_to_schema function."""

    def test_add_context_to_empty_schema(self):
        """Test adding context to an empty schema."""
        schema = {}
        custom_desc = "Test description"

        result = add_context_parameter_to_schema(schema, custom_desc)

        # Verify properties were added
        assert "properties" in result
        assert "context" in result["properties"]
        assert result["properties"]["context"]["type"] == "string"
        assert result["properties"]["context"]["description"] == custom_desc

        # Verify required was added
        assert "required" in result
        assert "context" in result["required"]

        # Verify original wasn't modified
        assert "properties" not in schema

    def test_add_context_to_schema_with_existing_properties(self):
        """Test adding context to schema with existing properties."""
        schema = {
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"}
            },
            "required": ["name"]
        }
        custom_desc = "Why this tool?"

        result = add_context_parameter_to_schema(schema, custom_desc)

        # Verify original properties still exist
        assert "name" in result["properties"]
        assert "age" in result["properties"]

        # Verify context was added
        assert "context" in result["properties"]
        assert result["properties"]["context"]["description"] == custom_desc

        # Verify required array was updated
        assert "name" in result["required"]
        assert "context" in result["required"]
        assert len(result["required"]) == 2

        # Verify original wasn't modified
        assert "context" not in schema["properties"]
        assert "context" not in schema["required"]

    def test_add_context_to_schema_with_no_required(self):
        """Test adding context when schema has no required field."""
        schema = {
            "properties": {
                "optional_field": {"type": "string"}
            }
        }
        custom_desc = "Context for optional schema"

        result = add_context_parameter_to_schema(schema, custom_desc)

        # Verify required array was created with context
        assert "required" in result
        assert result["required"] == ["context"]

        # Verify properties were updated
        assert "optional_field" in result["properties"]
        assert "context" in result["properties"]

    def test_schema_immutability(self):
        """Test that original schema is not modified."""
        original_schema = {
            "properties": {
                "field1": {"type": "string"},
                "field2": {"type": "integer"}
            },
            "required": ["field1"]
        }

        # Deep copy to compare later
        schema_copy = deepcopy(original_schema)

        result = add_context_parameter_to_schema(original_schema, "Test")

        # Original should be unchanged
        assert original_schema == schema_copy

        # Result should be different
        assert result != original_schema
        assert "context" in result["properties"]
        assert "context" not in original_schema["properties"]

    def test_context_already_in_required(self):
        """Test when context is already in required array."""
        schema = {
            "properties": {
                "context": {"type": "string", "description": "Existing context"}
            },
            "required": ["context"]
        }
        custom_desc = "New context description"

        result = add_context_parameter_to_schema(schema, custom_desc)

        # Context should be overwritten with new description
        assert result["properties"]["context"]["description"] == custom_desc

        # Required should still contain context (no duplicate)
        assert result["required"].count("context") == 1

    def test_empty_custom_description(self):
        """Test with empty string custom description."""
        schema = {"properties": {}}

        result = add_context_parameter_to_schema(schema, "")

        assert result["properties"]["context"]["description"] == ""
        assert result["properties"]["context"]["type"] == "string"

    def test_special_characters_in_description(self):
        """Test with special characters in custom description."""
        schema = {}
        special_desc = "Unicode: 🚀 Quotes: \"test\" Newline:\nTab:\t"

        result = add_context_parameter_to_schema(schema, special_desc)

        assert result["properties"]["context"]["description"] == special_desc

    def test_very_long_description(self):
        """Test with very long custom description."""
        schema = {}
        long_desc = "A" * 10000  # 10,000 characters

        result = add_context_parameter_to_schema(schema, long_desc)

        assert result["properties"]["context"]["description"] == long_desc
        assert len(result["properties"]["context"]["description"]) == 10000

    def test_nested_properties_preserved(self):
        """Test that nested/complex properties are preserved."""
        schema = {
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "email": {"type": "string"}
                    }
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            },
            "required": ["user"]
        }

        result = add_context_parameter_to_schema(schema, "Test")

        # Verify nested properties are preserved
        assert "user" in result["properties"]
        assert result["properties"]["user"]["properties"]["name"]["type"] == "string"
        assert "tags" in result["properties"]
        assert result["properties"]["tags"]["type"] == "array"

        # Verify context was added
        assert "context" in result["properties"]
        assert "context" in result["required"]


class TestAddContextParameterToTools:
    """Unit tests for add_context_parameter_to_tools function."""

    def test_empty_tools_list(self):
        """Test with empty tools list."""
        tools = []
        result = add_context_parameter_to_tools(tools, "Test description")

        assert result == []

    def test_single_tool_with_input_schema(self):
        """Test with a single tool that has inputSchema."""
        tools = [
            {
                "name": "test_tool",
                "description": "A test tool",
                "inputSchema": {
                    "properties": {
                        "param": {"type": "string"}
                    },
                    "required": ["param"]
                }
            }
        ]
        custom_desc = "Tool context"

        result = add_context_parameter_to_tools(tools, custom_desc)

        assert len(result) == 1
        assert "context" in result[0]["inputSchema"]["properties"]
        assert result[0]["inputSchema"]["properties"]["context"]["description"] == custom_desc
        assert "context" in result[0]["inputSchema"]["required"]

        # Original tools list should be unchanged
        assert "context" not in tools[0]["inputSchema"]["properties"]

    def test_tool_without_input_schema(self):
        """Test with a tool that has no inputSchema."""
        tools = [
            {
                "name": "simple_tool",
                "description": "A simple tool"
            }
        ]

        result = add_context_parameter_to_tools(tools, "Test")

        # Tool should be copied but not modified (no inputSchema)
        assert len(result) == 1
        assert result[0]["name"] == "simple_tool"
        assert "inputSchema" not in result[0]

    def test_multiple_tools(self):
        """Test with multiple tools of different types."""
        tools = [
            {
                "name": "tool1",
                "inputSchema": {"properties": {}}
            },
            {
                "name": "tool2",
                "inputSchema": {
                    "properties": {"field": {"type": "string"}},
                    "required": ["field"]
                }
            },
            {
                "name": "tool3",
                "description": "No schema"
            }
        ]
        custom_desc = "Multi-tool context"

        result = add_context_parameter_to_tools(tools, custom_desc)

        assert len(result) == 3

        # Tool 1: empty properties
        assert "context" in result[0]["inputSchema"]["properties"]
        assert result[0]["inputSchema"]["properties"]["context"]["description"] == custom_desc

        # Tool 2: existing properties
        assert "field" in result[1]["inputSchema"]["properties"]
        assert "context" in result[1]["inputSchema"]["properties"]
        assert result[1]["inputSchema"]["properties"]["context"]["description"] == custom_desc

        # Tool 3: no inputSchema
        assert "inputSchema" not in result[2]

    def test_tools_immutability(self):
        """Test that original tools list is not modified."""
        original_tools = [
            {
                "name": "test_tool",
                "inputSchema": {
                    "properties": {"param": {"type": "string"}}
                }
            }
        ]

        tools_copy = deepcopy(original_tools)

        result = add_context_parameter_to_tools(original_tools, "Test")

        # Original should be unchanged
        assert original_tools == tools_copy

        # Result should be different
        assert result != original_tools
        assert "context" in result[0]["inputSchema"]["properties"]
        assert "context" not in original_tools[0]["inputSchema"]["properties"]

    def test_tool_with_complex_schema(self):
        """Test with a tool that has a complex schema."""
        tools = [
            {
                "name": "complex_tool",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "config": {
                            "type": "object",
                            "properties": {
                                "enabled": {"type": "boolean"},
                                "level": {"type": "integer", "minimum": 0, "maximum": 10}
                            }
                        },
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "value": {"type": "number"}
                                }
                            }
                        }
                    },
                    "required": ["config"],
                    "additionalProperties": False
                }
            }
        ]

        result = add_context_parameter_to_tools(tools, "Complex context")

        # Verify complex properties are preserved
        assert "config" in result[0]["inputSchema"]["properties"]
        assert "items" in result[0]["inputSchema"]["properties"]
        assert result[0]["inputSchema"]["additionalProperties"] == False

        # Verify context was added
        assert "context" in result[0]["inputSchema"]["properties"]
        assert "context" in result[0]["inputSchema"]["required"]

    def test_special_tool_fields_preserved(self):
        """Test that other tool fields are preserved."""
        tools = [
            {
                "name": "full_tool",
                "description": "A complete tool",
                "version": "1.0.0",
                "deprecated": False,
                "inputSchema": {"properties": {}},
                "outputSchema": {"type": "string"},
                "metadata": {"author": "test"}
            }
        ]

        result = add_context_parameter_to_tools(tools, "Test")

        # All fields should be preserved
        assert result[0]["name"] == "full_tool"
        assert result[0]["description"] == "A complete tool"
        assert result[0]["version"] == "1.0.0"
        assert result[0]["deprecated"] == False
        assert result[0]["outputSchema"] == {"type": "string"}
        assert result[0]["metadata"] == {"author": "test"}

        # And context should be added
        assert "context" in result[0]["inputSchema"]["properties"]

    def test_unicode_in_tool_names_and_descriptions(self):
        """Test tools with Unicode characters in various fields."""
        tools = [
            {
                "name": "emoji_tool_🚀",
                "description": "Tool with emojis 🎉",
                "inputSchema": {
                    "properties": {
                        "field_with_emoji_🌟": {"type": "string"}
                    }
                }
            }
        ]
        custom_desc = "Context with emoji 🤔"

        result = add_context_parameter_to_tools(tools, custom_desc)

        # Unicode should be preserved everywhere
        assert result[0]["name"] == "emoji_tool_🚀"
        assert result[0]["description"] == "Tool with emojis 🎉"
        assert "field_with_emoji_🌟" in result[0]["inputSchema"]["properties"]
        assert result[0]["inputSchema"]["properties"]["context"]["description"] == custom_desc