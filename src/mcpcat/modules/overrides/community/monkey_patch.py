"""Monkey-patching implementation for community FastMCP servers.

This module patches community FastMCP servers to intercept tool operations
and add AgentCat tracking capabilities.
"""

from datetime import datetime, timezone
from typing import Any

from mcp.types import CallToolRequest
from mcp import ServerResult

from mcpcat.modules import event_queue
from mcpcat.modules.compatibility import is_mcp_error_response
from mcpcat.modules.exceptions import capture_exception
from mcpcat.modules.identify import identify_session
from mcpcat.modules.internal import attach_event_metadata, get_server_tracking_data
from mcpcat.modules.logging import write_to_log
from mcpcat.modules.request_extra import params_with_extra
from mcpcat.modules.session import (
    get_client_info_from_request_context,
    get_server_session_id,
)
from mcpcat.types import EventType, UnredactedEvent

from ..mcp_server import override_lowlevel_mcp_server_minimal, safe_request_context


def patch_community_fastmcp(server: Any) -> None:
    """Main entry point for patching community FastMCP servers.

    This function:
    1. Patches the tool manager to add context parameters to tools
    2. Overrides the call_tool handler for tracking and context removal
    3. Sets up minimal overrides for other MCP events
    """
    try:
        # First, patch the tool manager for context injection and tool tracking
        from .tool_manager import patch_community_fastmcp_tool_manager
        patch_community_fastmcp_tool_manager(server)

        # Get the low-level MCP server
        lowlevel_server = server._mcp_server
        data = get_server_tracking_data(lowlevel_server)

        if not data:
            write_to_log("No tracking data found for community FastMCP server")
            return

        # Patch _get_cached_tool_definition to remove context from validation
        original_get_cached_tool = lowlevel_server._get_cached_tool_definition

        async def patched_get_cached_tool_definition(tool_name: str):
            """Get tool definition with context removed for validation."""
            # Get the original tool definition
            tool = await original_get_cached_tool(tool_name)

            if tool and data.options.enable_tool_call_context and tool_name != "get_more_tools":
                # Create a copy of the tool to avoid modifying the cache
                import copy
                tool_copy = copy.deepcopy(tool)

                # Remove context from the schema for validation
                if hasattr(tool_copy, "inputSchema") and tool_copy.inputSchema:
                    if "properties" in tool_copy.inputSchema:
                        if "context" in tool_copy.inputSchema["properties"]:
                            # Remove context from properties
                            del tool_copy.inputSchema["properties"]["context"]

                            # Remove context from required if present
                            if "required" in tool_copy.inputSchema and isinstance(tool_copy.inputSchema["required"], list):
                                if "context" in tool_copy.inputSchema["required"]:
                                    tool_copy.inputSchema["required"].remove("context")

                    write_to_log(f"Removed context from validation schema for tool {tool_name}")
                    return tool_copy

            return tool

        # Apply the patched method
        lowlevel_server._get_cached_tool_definition = patched_get_cached_tool_definition
        write_to_log("Patched _get_cached_tool_definition for community FastMCP")

        # Override the call_tool handler to handle context removal and tracking
        original_call_tool_handler = lowlevel_server.request_handlers.get(CallToolRequest)

        if not original_call_tool_handler:
            write_to_log("No original call_tool handler found")
            return

        async def wrapped_call_tool_handler(request: CallToolRequest) -> ServerResult:
            """Intercept call_tool requests to handle context and tracking."""
            tool_name = request.params.name
            arguments = dict(request.params.arguments) if request.params.arguments else {}

            # Get request context for session tracking
            request_context = safe_request_context(lowlevel_server)
            session_id = get_server_session_id(lowlevel_server)

            # Handle session identification
            try:
                client_name, client_version = get_client_info_from_request_context(lowlevel_server, request_context)
                identity = identify_session(lowlevel_server, request, request_context)
            except Exception as e:
                client_name, client_version = None, None
                identity = None
                write_to_log(f"Non-critical error in session handling: {e}")

            # Extract user intent from context parameter
            user_intent = None
            if tool_name == "get_more_tools":
                # get_more_tools has its own context parameter that serves as user intent
                user_intent = arguments.get("context", None)
            elif data.options.enable_tool_call_context:
                # For other tools, extract user intent when context injection is enabled
                user_intent = arguments.get("context", None)

            # Create tracking event
            event = UnredactedEvent(
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                parameters=params_with_extra(
                    {"name": tool_name, "arguments": arguments},
                    request_context,
                ),
                event_type=EventType.MCP_TOOLS_CALL.value,
                resource_name=tool_name,
                user_intent=user_intent,
                identify_actor_given_id=identity.user_id if identity else None,
                identify_actor_name=identity.user_name if identity else None,
                identify_data=identity.user_data if identity else None,
                client_name=client_name,
                client_version=client_version,
            )
            await attach_event_metadata(event, data, request, request_context)

            try:
                # Handle get_more_tools specially - don't intercept for community FastMCP
                # Let it go through the normal tool handler which will return a string
                if tool_name == "get_more_tools":
                    # Just track the event but let the tool execute normally
                    # The tool function itself returns a string which is what community FastMCP expects
                    pass  # Fall through to call original handler
                elif data.options.enable_tool_call_context:
                    # Remove context from arguments before calling other tools
                    # Create a new request with modified arguments
                    modified_args = arguments.copy()
                    modified_args.pop("context", None)

                    # Modify the request in place since we can't create a new one easily
                    request.params.arguments = modified_args

                # Call original handler with potentially modified request
                result = await original_call_tool_handler(request)

                # Check for errors
                is_error, error_message = is_mcp_error_response(result)
                event.is_error = is_error
                # Use full exception capture if there's an error
                if is_error:
                    event.error = capture_exception(result)
                else:
                    event.error = None
                event.response = result.model_dump() if result else None

                return result

            except Exception as e:
                write_to_log(f"Error in wrapped_call_tool_handler: {e}")
                event.is_error = True
                # Use full exception capture with stack trace
                try:
                    event.error = capture_exception(e)
                except Exception as capture_err:
                    # Fallback to simple error if capture fails
                    write_to_log(f"Error capturing exception: {capture_err}")
                    event.error = {
                        "message": str(e),
                        "type": type(e).__name__,
                        "platform": "python",
                    }
                raise
            finally:
                # Always publish event if tracing is enabled
                if data.options.enable_tracing:
                    try:
                        event_queue.publish_event(lowlevel_server, event)
                    except Exception as e:
                        write_to_log(f"Error publishing event: {e}")

        # Apply the wrapped handler
        lowlevel_server.request_handlers[CallToolRequest] = wrapped_call_tool_handler
        write_to_log(f"Successfully patched call_tool handler for community FastMCP server {id(server)}")

        # Use minimal override for other events (initialize, list_tools)
        # This handles event tracking for non-tool operations
        override_lowlevel_mcp_server_minimal(lowlevel_server, data)
        write_to_log(f"Applied minimal overrides for community FastMCP server {id(server)}")

    except Exception as e:
        write_to_log(f"Error patching community FastMCP server: {e}")
