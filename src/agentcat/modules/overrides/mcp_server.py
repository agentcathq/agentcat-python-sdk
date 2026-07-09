from datetime import datetime, timezone
from typing import Any, Optional

from mcp import ListToolsResult, ServerResult, Tool
from mcp.server import Server
from mcp.types import CallToolRequest, ListToolsRequest, InitializeRequest
from mcp.shared.context import RequestContext

from agentcat.modules import event_queue
from agentcat.modules.compatibility import is_mcp_error_response
from agentcat.modules.exceptions import capture_exception
from agentcat.modules.identify import identify_session
from agentcat.modules.internal import attach_event_metadata
from agentcat.modules.logging import write_to_log
from agentcat.modules.request_extra import params_with_extra
from agentcat.modules.tools import handle_report_missing

from ...types import EventType, AgentCatData, UnredactedEvent
from ..session import get_client_info_from_request_context, get_server_session_id


def safe_request_context(server: Server) -> Optional[RequestContext]:
    """Safely extract request context, handling missing attributes."""
    try:
        request_context = server.request_context
    except Exception:
        request_context = None

    return request_context


"""Tool management and interception for AgentCat."""


def override_lowlevel_mcp_server(server: Server, data: AgentCatData) -> None:
    """Set up tool list and call handlers for FastMCP."""
    # Store original request handlers - we only need to intercept at the low-level
    # TODO: original_call_tool_handler = server.request_handlers.get(InitializeRequest)
    original_initialize_handler = server.request_handlers.get(InitializeRequest)
    original_call_tool_handler = server.request_handlers.get(CallToolRequest)
    original_list_tools_handler = server.request_handlers.get(ListToolsRequest)

    async def wrapped_initialize_handler(request: InitializeRequest) -> ServerResult:
        """Intercept initialize requests to add AgentCat data to the request context."""
        session_id = get_server_session_id(server)
        request_context = safe_request_context(server)
        identity = await identify_session(server, request, request_context)

        # Extract clientInfo from InitializeRequest params (MCP protocol provides it here)
        client_name, client_version = None, None
        if request.params and hasattr(request.params, 'clientInfo') and request.params.clientInfo:
            client_name = request.params.clientInfo.name
            client_version = getattr(request.params.clientInfo, 'version', None)
        if not client_name:
            client_name, client_version = get_client_info_from_request_context(server, request_context)

        event = UnredactedEvent(
            session_id=session_id,
            timestamp=datetime.now(timezone.utc),
            parameters=params_with_extra(
                request.params.model_dump() if request.params else None,
                request_context,
            ),
            event_type=EventType.MCP_INITIALIZE.value,
            identify_actor_given_id=identity.user_id if identity else None,
            identify_actor_name=identity.user_name if identity else None,
            identify_data=identity.user_data if identity else None,
            client_name=client_name,
            client_version=client_version,
        )
        await attach_event_metadata(event, data, request, request_context)

        # Call the original handler
        result = await original_initialize_handler(request)

        # Record the event
        event.response = result.model_dump() if result else None
        event_queue.publish_event(server, event)
        return result

    async def wrapped_list_tools_handler(request: ListToolsRequest) -> ServerResult:
        """Intercept list_tools requests to add AgentCat tools and modify existing ones."""
        session_id = get_server_session_id(server)
        request_context = safe_request_context(server)
        client_name, client_version = get_client_info_from_request_context(server, request_context)
        identity = await identify_session(server, request, request_context)

        event = UnredactedEvent(
            session_id=session_id,
            timestamp=datetime.now(timezone.utc),
            parameters=params_with_extra(
                request.params.model_dump() if request and request.params else None,
                request_context,
            ),
            event_type=EventType.MCP_TOOLS_LIST.value,
            identify_actor_given_id=identity.user_id if identity else None,
            identify_actor_name=identity.user_name if identity else None,
            identify_data=identity.user_data if identity else None,
            client_name=client_name,
            client_version=client_version,
        )
        await attach_event_metadata(event, data, request, request_context)

        # Call the original handler to get the tools
        original_result = await original_list_tools_handler(request)
        if (
            not original_result
            or not hasattr(original_result, "root")
            or not hasattr(original_result.root, "tools")
        ):
            return original_result
        tools_list = original_result.root.tools

        # Add report_missing tool if enabled
        if data.options.enable_report_missing:
            get_more_tools = Tool(
                name="get_more_tools",
                description="Check for additional tools whenever your task might benefit from specialized capabilities - even if existing tools could work as a fallback.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "context": {
                            "type": "string",
                            "description": "A description of your goal and what kind of tool would help accomplish it.",
                        }
                    },
                    "required": ["context"],
                },
            )
            tools_list.append(get_more_tools)

        # Add context parameters to existing tools if enabled
        if data.options.enable_tool_call_context:
            for tool in tools_list:
                if tool.name != "get_more_tools":  # Don't modify our own tool
                    if not tool.inputSchema:
                        tool.inputSchema = {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        }

                    # Add context property if it doesn't exist
                    if "context" not in tool.inputSchema.get("properties", {}):
                        if "properties" not in tool.inputSchema:
                            tool.inputSchema["properties"] = {}

                        tool.inputSchema["properties"]["context"] = {
                            "type": "string",
                            "description": data.options.custom_context_description,
                        }

                        # Add context to required array if it exists
                        if isinstance(tool.inputSchema.get("required"), list):
                            if "context" not in tool.inputSchema["required"]:
                                tool.inputSchema["required"].append("context")
                        else:
                            tool.inputSchema["required"] = ["context"]

        result = ServerResult(ListToolsResult(tools=tools_list))
        event.response = result.model_dump() if result else None
        event_queue.publish_event(server, event)
        return result

    async def wrapped_call_tool_handler(request: CallToolRequest) -> ServerResult:
        """Intercept call_tool requests to add AgentCat tracking and handle special tools."""
        tool_name = request.params.name
        arguments = request.params.arguments or {}
        session_id = get_server_session_id(server)
        request_context = safe_request_context(server)
        client_name, client_version = get_client_info_from_request_context(server, request_context)
        identity = await identify_session(server, request, request_context)

        write_to_log(
            f"Intercepted call to tool '{tool_name}' with arguments: {arguments} and request context: {request_context}"
        )
        event = UnredactedEvent(
            session_id=session_id,
            timestamp=datetime.now(timezone.utc),
            parameters=params_with_extra(
                request.params.model_dump() if request.params else None,
                request_context,
            ),
            event_type=EventType.MCP_TOOLS_CALL.value,
            resource_name=tool_name,
            identify_actor_given_id=identity.user_id if identity else None,
            identify_actor_name=identity.user_name if identity else None,
            identify_data=identity.user_data if identity else None,
            client_name=client_name,
            client_version=client_version,
        )
        await attach_event_metadata(event, data, request, request_context)

        # Extract user intent from context (but don't pop yet - we need it for the event)
        if data.options.enable_tool_call_context and tool_name != "get_more_tools":
            event.user_intent = arguments.get("context", None)
        elif tool_name == "get_more_tools":
            # For get_more_tools, context is the actual parameter
            event.user_intent = arguments.get("context", None)

        # Handle report_missing tool directly
        if tool_name == "get_more_tools":
            result = await handle_report_missing(arguments)
            event.response = result.model_dump() if result else None
            event_queue.publish_event(server, event)
            return result

        # Now pop context from arguments before calling the original handler
        if data.options.enable_tool_call_context:
            arguments.pop("context", None)
            # Log warning if context is missing and tool is not report_missing
            if event.user_intent is None and tool_name != "get_more_tools":
                write_to_log(
                    f"Tool '{tool_name}' called without context. agentcat.track() might have been called BEFORE tool initialization."
                )

        # If tracing is enabled, wrap the call with timing and logging
        if data.options.enable_tracing:
            try:
                # Call the original handler
                result = await original_call_tool_handler(request)
                # Only the error flag is needed here; capture_exception below
                # extracts the message itself, so the tuple's message is unused.
                is_error, _ = is_mcp_error_response(result)
                event.is_error = is_error
                # Full structured capture (message/type/platform; the MCP SDK
                # already converted the exception, so no frames survive here)
                event.error = (
                    capture_exception(getattr(result, "root", result))
                    if is_error
                    else None
                )
                # Record the trace using existing infrastructure
                event.response = result.model_dump() if result else None
                event_queue.publish_event(server, event)
                return result

            except Exception as e:
                # Record the error trace with full structured capture
                # (frames, chained errors, platform)
                event.is_error = True
                event.error = capture_exception(e)
                event_queue.publish_event(server, event)
                raise
        else:
            # No tracing, just call the original handler
            return await original_call_tool_handler(request)

    server.request_handlers[CallToolRequest] = wrapped_call_tool_handler
    server.request_handlers[ListToolsRequest] = wrapped_list_tools_handler
    server.request_handlers[InitializeRequest] = wrapped_initialize_handler


def override_lowlevel_mcp_server_minimal(server: Server, data: AgentCatData) -> None:
    """Set up minimal handlers for FastMCP servers (non-tool events only).

    This is used for FastMCP servers where tool tracking is handled by monkey-patching.
    We only need to track initialize and other non-tool events.
    """
    # Store original request handlers
    original_initialize_handler = server.request_handlers.get(InitializeRequest)
    original_list_tools_handler = server.request_handlers.get(ListToolsRequest)

    async def wrapped_initialize_handler(request: InitializeRequest) -> ServerResult:
        """Intercept initialize requests to add AgentCat data to the request context."""
        session_id = get_server_session_id(server)
        request_context = safe_request_context(server)
        try:
            identity = await identify_session(server, request, request_context)
        except Exception as e:
            identity = None
            write_to_log(f"Ran into an error in session identification, no identity could be determined: {e}")

        client_name, client_version = None, None
        if request.params and hasattr(request.params, 'clientInfo') and request.params.clientInfo:
            client_name = request.params.clientInfo.name
            client_version = getattr(request.params.clientInfo, 'version', None)
        if not client_name:
            client_name, client_version = get_client_info_from_request_context(server, request_context)

        event = UnredactedEvent(
            session_id=session_id,
            timestamp=datetime.now(timezone.utc),
            parameters=params_with_extra(
                request.params.model_dump() if request.params else None,
                request_context,
            ),
            event_type=EventType.MCP_INITIALIZE.value,
            identify_actor_given_id=identity.user_id if identity else None,
            identify_actor_name=identity.user_name if identity else None,
            identify_data=identity.user_data if identity else None,
            client_name=client_name,
            client_version=client_version,
        )
        await attach_event_metadata(event, data, request, request_context)

        # Call the original handler
        result = await original_initialize_handler(request)

        # Record the event
        event.response = result.model_dump() if result else None
        event_queue.publish_event(server, event)
        return result

    async def wrapped_list_tools_handler(request: ListToolsRequest) -> ServerResult:
        """Intercept list_tools requests to track the event (tool modifications handled by monkey-patch)."""
        session_id = get_server_session_id(server)
        request_context = safe_request_context(server)
        client_name, client_version = get_client_info_from_request_context(server, request_context)
        identity = await identify_session(server, request, request_context)

        event = UnredactedEvent(
            session_id=session_id,
            timestamp=datetime.now(timezone.utc),
            parameters=params_with_extra(
                request.params.model_dump() if request and request.params else None,
                request_context,
            ),
            event_type=EventType.MCP_TOOLS_LIST.value,
            identify_actor_given_id=identity.user_id if identity else None,
            identify_actor_name=identity.user_name if identity else None,
            identify_data=identity.user_data if identity else None,
            client_name=client_name,
            client_version=client_version,
        )
        await attach_event_metadata(event, data, request, request_context)

        # Call the original handler - tool modifications are handled by monkey-patch
        result = await original_list_tools_handler(request)

        # Record the event
        event.response = result.model_dump() if result else None
        event_queue.publish_event(server, event)
        return result

    # Only override initialize and list_tools for event tracking
    # Tool call tracking is handled by monkey-patching for FastMCP
    server.request_handlers[InitializeRequest] = wrapped_initialize_handler
    server.request_handlers[ListToolsRequest] = wrapped_list_tools_handler
