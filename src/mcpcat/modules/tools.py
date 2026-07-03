"""Tool management and interception for AgentCat."""

from typing import Any, TYPE_CHECKING
from mcp.types import CallToolResult, TextContent
from mcpcat.modules.version_detection import has_fastmcp_support

from .logging import write_to_log

# Correct schema for the get_more_tools tool parameter.
# Defined explicitly because Pydantic's TypeAdapter generates a broken schema
# (anyOf: [string, null], default: "") for Annotated[str, Field(description=...)]
# on async closure functions used by Tool.from_function().
GET_MORE_TOOLS_SCHEMA = {
    "type": "object",
    "properties": {
        "context": {
            "type": "string",
            "description": "A description of your goal and what kind of tool would help accomplish it.",
        }
    },
    "required": ["context"],
}

if TYPE_CHECKING or has_fastmcp_support():
    try:
        from mcp.server import FastMCP
    except ImportError:
        FastMCP = None


async def handle_report_missing(arguments: dict[str, Any]) -> CallToolResult:
    """Handle the report_missing tool."""
    # Metadata-only diagnostics: log the context length, never the context text.
    write_to_log(
        f"Missing tool reported (context length: {len(arguments.get('context', ''))})"
    )
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text="Unfortunately, we have shown you the full tool list. We have noted your feedback and will work to improve the tool list in the future.",
            )
        ]
    )
