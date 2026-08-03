"""The get_more_tools descriptor and handler.

Agent-facing copy here is byte-identical to the TypeScript SDK's
`src/modules/tools.ts` and guarded by tests/test_constants_copy.py.

`mcp` is imported inside the handler, never at module scope, so this module
loads under either SDK major.
"""

from typing import TYPE_CHECKING, Any

from .logging import write_to_log

if TYPE_CHECKING:
    from mcp.types import CallToolResult

GET_MORE_TOOLS_DESCRIPTION = (
    "Check for additional tools whenever your task might benefit from "
    "specialized capabilities - even if existing tools could work as a fallback."
)

# Correct schema for the get_more_tools tool parameter.
# Defined explicitly because Pydantic's TypeAdapter generates a broken schema
# (anyOf: [string, null], default: "") for Annotated[str, Field(description=...)]
# on async closure functions used by Tool.from_function().
GET_MORE_TOOLS_SCHEMA = {
    "type": "object",
    "properties": {
        "context": {
            "type": "string",
            "description": "A description of your goal and what kind of tool would help accomplish it.",  # noqa: E501
        }
    },
    "required": ["context"],
}

REPORT_MISSING_RESPONSE_TEXT = (
    "Unfortunately, we have shown you the full tool list. We have noted your "
    "feedback and will work to improve the tool list in the future."
)


async def handle_report_missing(arguments: dict[str, Any]) -> "CallToolResult":
    """Answer a get_more_tools call. Never sees the tool list; always the same."""
    from mcp.types import CallToolResult, TextContent

    # Metadata-only diagnostics: the context length, never the context text.
    context = arguments.get("context") if isinstance(arguments, dict) else None
    write_to_log(
        f"Missing tool reported (context length: "
        f"{len(context) if isinstance(context, str) else 0})"
    )
    return CallToolResult(
        content=[TextContent(type="text", text=REPORT_MISSING_RESPONSE_TEXT)]
    )
