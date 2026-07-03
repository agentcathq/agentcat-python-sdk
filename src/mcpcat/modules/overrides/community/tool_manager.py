from typing import Any

from mcpcat.modules.compatibility import is_community_fastmcp_server
from mcpcat.modules.internal import (
    get_server_tracking_data,
    get_original_method,
    store_original_method,
    is_tool_tracked,
    register_tool,
    mark_tool_tracked,
)
from mcpcat.modules.logging import write_to_log
from mcpcat.modules.tools import handle_report_missing

from fastmcp import FastMCP


def _ensure_context_parameter(tool: Any, description: str) -> None:
    """Add or overwrite the 'context' parameter in a tool's schema.

    Ensures the tool has a valid parameters dict with a 'context' property
    marked as required.
    """
    if not hasattr(tool, "parameters") or not tool.parameters:
        tool.parameters = {"type": "object", "properties": {}, "required": []}

    if "properties" not in tool.parameters:
        tool.parameters["properties"] = {}

    tool.parameters["properties"]["context"] = {
        "type": "string",
        "description": description,
    }

    if "required" not in tool.parameters:
        tool.parameters["required"] = []

    if isinstance(tool.parameters["required"], list):
        if "context" not in tool.parameters["required"]:
            tool.parameters["required"].append("context")
    else:
        tool.parameters["required"] = ["context"]


def patch_community_fastmcp_tool_manager(server: Any) -> None:
    """Patch the community FastMCP tool manager to add AgentCat tracking.

    This function modifies the tool manager to:
    1. Add context parameter to existing tools
    2. Automatically add context to new tools via add_tool patching
    3. Add get_more_tools if enabled
    """
    # Check that the server is a community FastMCP server
    if not is_community_fastmcp_server(server):
        write_to_log("WARNING: Incompatible community FastMCP server detected. Tracking not properly enabled.")
        return

    # Get tracking data from the low-level server
    data = get_server_tracking_data(server._mcp_server)
    if not data:
        write_to_log("WARNING: Unknown error when tracking community FastMCP. Tracking data for server not initialized.")
        return

    write_to_log(f"Patching community FastMCP tool manager for server {id(server)}")

    # Add get_more_tools if enabled
    if data.options.enable_report_missing:
        try:
            async def get_more_tools(context: str) -> str:
                """Check for additional tools whenever your task might benefit from specialized capabilities."""
                result = await handle_report_missing({"context": context})
                if result.content:
                    return result.content[0].text
                return "No additional tools available"

            server.tool(
                get_more_tools,
                name="get_more_tools",
                description="Check for additional tools whenever your task might benefit from specialized capabilities - even if existing tools could work as a fallback.",
            )

            # Force the correct schema - Pydantic's TypeAdapter can mangle
            # the type on async closures into anyOf: [string, null]
            from mcpcat.modules.tools import GET_MORE_TOOLS_SCHEMA
            if hasattr(server._tool_manager, "_tools") and "get_more_tools" in server._tool_manager._tools:
                server._tool_manager._tools["get_more_tools"].parameters = GET_MORE_TOOLS_SCHEMA

            write_to_log("Added get_more_tools tool to community FastMCP server")
        except Exception as e:
            write_to_log(f"Error adding get_more_tools: {e}")

    # Track existing tools and optionally add context parameter
    if hasattr(server._tool_manager, "_tools"):
        for tool_name, tool in server._tool_manager._tools.items():
            # Track the tool
            if not is_tool_tracked(server._mcp_server, tool_name):
                register_tool(server._mcp_server, tool_name)
                mark_tool_tracked(server._mcp_server, tool_name)
                write_to_log(f"Found existing community FastMCP tool: {tool_name}")

    # Patch existing tools if context injection is enabled
    if data.options.enable_tool_call_context:
        patch_existing_tools(server)
        patch_add_tool_fn(server)


def patch_existing_tools(server: FastMCP) -> None:
    """Modify existing tools to include the context parameter."""
    try:
        data = get_server_tracking_data(server._mcp_server)
        tool_manager = server._tool_manager
        if not hasattr(tool_manager, "_tools"):
            write_to_log("No _tools dictionary found on tool manager")
            return

        for tool_name, tool in tool_manager._tools.items():
            if tool_name == "get_more_tools":
                continue

            _ensure_context_parameter(tool, data.options.custom_context_description)
            write_to_log(f"Added/updated context parameter for existing tool: {tool_name}")

    except Exception as e:
        write_to_log(f"Error patching existing tools: {e}")


def patch_add_tool_fn(server: FastMCP) -> None:
    """Patch the add_tool method to automatically add context parameter to new tools."""
    try:
        tool_manager = server._tool_manager
        tool_manager_id = id(tool_manager)
        method_key = f"community_{tool_manager_id}_add_tool"

        # Store original method if not already stored
        if get_original_method(method_key) is None:
            store_original_method(method_key, tool_manager.add_tool)
            write_to_log(f"Stored original add_tool for community tool_manager {tool_manager_id}")

        original_add_tool = get_original_method(method_key)
        if not original_add_tool:
            write_to_log("Failed to get original add_tool method")
            return

        def patched_add_tool(tool: Any) -> Any:
            """Patched add_tool that adds context parameter to new tools."""
            try:
                # Call original method first
                result = original_add_tool(tool)

                # Track the tool
                tool_name = tool.key if hasattr(tool, "key") else (tool.name if hasattr(tool, "name") else "unknown")
                if not is_tool_tracked(server._mcp_server, tool_name):
                    register_tool(server._mcp_server, tool_name)
                    mark_tool_tracked(server._mcp_server, tool_name)
                    write_to_log(f"Tracked new community FastMCP tool: {tool_name}")

                # Add context parameter if it's not get_more_tools
                if tool_name != "get_more_tools":
                    data = get_server_tracking_data(server._mcp_server)
                    if data and data.options.enable_tool_call_context:
                        _ensure_context_parameter(tool, data.options.custom_context_description)
                        write_to_log(f"Added/updated context parameter for new tool: {tool_name}")

                return result
            except Exception as e:
                write_to_log(f"Error in patched add_tool: {e}")
                # Fall back to original if something goes wrong
                if callable(original_add_tool):
                    return original_add_tool(tool)
                return tool

        # Apply the patch
        tool_manager.add_tool = patched_add_tool
        write_to_log(f"Successfully patched add_tool for community tool_manager {tool_manager_id}")

    except Exception as e:
        write_to_log(f"Error patching add_tool: {e}")
