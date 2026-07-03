"""AgentCat Middleware for Community FastMCP v3.

This module provides a middleware implementation that integrates AgentCat
tracking capabilities with the FastMCP v3 middleware system.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import mcp.types as mt

from mcpcat.modules import event_queue
from mcpcat.modules.exceptions import (
    capture_exception,
    clear_captured_error,
    get_captured_error,
    store_captured_error,
)
from mcpcat.modules.identify import identify_session
from mcpcat.modules.internal import attach_event_metadata, mark_tool_tracked, register_tool
from mcpcat.modules.logging import write_to_log
from mcpcat.modules.request_extra import params_with_extra
from mcpcat.modules.session import (
    get_client_info_from_request_context,
    get_server_session_id,
)
from mcpcat.types import EventType, AgentCatData, UnredactedEvent

if TYPE_CHECKING:
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.tool import Tool, ToolResult


class AgentCatMiddleware:
    """Middleware for AgentCat tracking in FastMCP v3.

    This middleware intercepts tool calls, list_tools, and initialize events
    to provide analytics tracking for AgentCat.

    Attributes:
        agentcat_data: The AgentCat tracking data configuration.
        server: The FastMCP server instance.
    """

    def __init__(self, agentcat_data: AgentCatData, server: Any) -> None:
        """Initialize the AgentCat middleware.

        Args:
            agentcat_data: AgentCat tracking configuration.
            server: The FastMCP v3 server instance.
        """
        self.agentcat_data = agentcat_data
        self.server = server

    async def __call__(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        """Main entry point that orchestrates the pipeline."""
        from functools import partial

        handler = call_next

        # Dispatch based on method
        method = context.method
        if method == "initialize":
            handler = partial(self.on_initialize, call_next=handler)
        elif method == "tools/call":
            handler = partial(self.on_call_tool, call_next=handler)
        elif method == "tools/list":
            handler = partial(self.on_list_tools, call_next=handler)

        return await handler(context)

    async def on_initialize(
        self,
        context: MiddlewareContext[mt.InitializeRequest],
        call_next: CallNext[mt.InitializeRequest, mt.InitializeResult | None],
    ) -> mt.InitializeResult | None:
        """Track initialize events and capture client info.

        Args:
            context: The middleware context containing the initialize request.
            call_next: Function to call the next handler in the chain.

        Returns:
            The initialize result from the next handler.
        """
        session_id = self._get_session_id()
        params = context.message.params

        # Extract client info from initialize params (MCP protocol provides clientInfo here)
        client_name, client_version = None, None
        if params and hasattr(params, "clientInfo") and params.clientInfo:
            client_info = params.clientInfo
            if hasattr(client_info, "name") and client_info.name:
                client_name = client_info.name
            if hasattr(client_info, "version") and client_info.version:
                client_version = client_info.version

        # Handle session identification
        # Note: Use self.server (FastMCP) not self.server._mcp_server because
        # tracking data is stored with the FastMCP server as the key for v3
        request_context = self._get_request_context(context)
        try:
            if not client_name:
                client_name, client_version = get_client_info_from_request_context(self.server, request_context)
            else:
                get_client_info_from_request_context(self.server, request_context)
            identity = identify_session(self.server, context.message, request_context)
        except Exception as e:
            identity = None
            write_to_log(f"Non-critical error in session handling: {e}")

        event = UnredactedEvent(
            session_id=session_id,
            timestamp=datetime.now(timezone.utc),
            parameters=params_with_extra(
                params.model_dump() if params else None,
                request_context,
                context.fastmcp_context,
            ),
            event_type=EventType.MCP_INITIALIZE.value,
            identify_actor_given_id=identity.user_id if identity else None,
            identify_actor_name=identity.user_name if identity else None,
            identify_data=identity.user_data if identity else None,
            client_name=client_name,
            client_version=client_version,
        )
        await attach_event_metadata(event, self.agentcat_data, context.message, request_context)

        try:
            result = await call_next(context)
            event.response = result.model_dump() if result else None
            return result
        except Exception as e:
            event.is_error = True
            event.error = capture_exception(e)
            raise
        finally:
            self._publish_event(event, "initialize")

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Track tool call events and handle context parameter extraction.

        Args:
            context: The middleware context containing the tool call request.
            call_next: Function to call the next handler in the chain.

        Returns:
            The tool result from the next handler.
        """
        message = context.message
        tool_name = message.name
        arguments = dict(message.arguments or {})
        session_id = self._get_session_id()

        # Handle session identification
        # Note: Use self.server (FastMCP) not self.server._mcp_server because
        # tracking data is stored with the FastMCP server as the key for v3
        request_context = self._get_request_context(context)
        try:
            client_name, client_version = get_client_info_from_request_context(self.server, request_context)
            identity = identify_session(self.server, context.message, request_context)
        except Exception as e:
            client_name, client_version = None, None
            identity = None
            write_to_log(f"Non-critical error in session handling: {e}")

        register_tool(self.server, tool_name)
        mark_tool_tracked(self.server, tool_name)

        # Extract user intent and determine if we should remove context from arguments
        user_intent = None
        should_remove_context = (
            self.agentcat_data.options.enable_tool_call_context
            and tool_name != "get_more_tools"
        )

        if tool_name == "get_more_tools":
            user_intent = arguments.get("context")
        elif should_remove_context:
            user_intent = arguments.pop("context", None)

        event = UnredactedEvent(
            session_id=session_id,
            timestamp=datetime.now(timezone.utc),
            parameters=params_with_extra(
                {"name": tool_name, "arguments": arguments},
                request_context,
                context.fastmcp_context,
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
        await attach_event_metadata(event, self.agentcat_data, context.message, request_context)

        # Create modified context without context parameter if needed
        call_context = context
        if should_remove_context and "context" in (message.arguments or {}):
            modified_args = {
                k: v for k, v in (message.arguments or {}).items() if k != "context"
            }
            modified_message = mt.CallToolRequestParams(
                name=tool_name,
                arguments=modified_args or None,
            )
            call_context = context.copy(message=modified_message)

        clear_captured_error()

        try:
            result = await call_next(call_context)

            if hasattr(result, "is_error") and result.is_error:
                event.is_error = True
                captured = get_captured_error()
                event.error = capture_exception(captured if captured else result)
            else:
                event.is_error = False

            event.response = self._serialize_result(result)
            return result

        except Exception as e:
            write_to_log(f"Error in on_call_tool: {e}")
            event.is_error = True
            store_captured_error(e)
            event.error = capture_exception(e)
            raise

        finally:
            self._publish_event(event, "tool call")

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Inject context parameter and track list_tools events.

        Args:
            context: The middleware context containing the list tools request.
            call_next: Function to call the next handler in the chain.

        Returns:
            The list of tools, potentially modified with context parameter.
        """
        session_id = self._get_session_id()

        # Handle session identification
        # Note: Use self.server (FastMCP) not self.server._mcp_server because
        # tracking data is stored with the FastMCP server as the key for v3
        request_context = self._get_request_context(context)
        try:
            client_name, client_version = get_client_info_from_request_context(self.server, request_context)
            identity = identify_session(self.server, context.message, request_context)
        except Exception as e:
            client_name, client_version = None, None
            identity = None
            write_to_log(f"Non-critical error in session handling: {e}")

        params = getattr(context.message, "params", None)

        event = UnredactedEvent(
            session_id=session_id,
            timestamp=datetime.now(timezone.utc),
            parameters=params_with_extra(
                params.model_dump() if params else None,
                request_context,
                context.fastmcp_context,
            ),
            event_type=EventType.MCP_TOOLS_LIST.value,
            identify_actor_given_id=identity.user_id if identity else None,
            identify_actor_name=identity.user_name if identity else None,
            identify_data=identity.user_data if identity else None,
            client_name=client_name,
            client_version=client_version,
        )
        await attach_event_metadata(event, self.agentcat_data, context.message, request_context)

        try:
            tools = list(await call_next(context))

            for tool in tools:
                register_tool(self.server, tool.name)
                mark_tool_tracked(self.server, tool.name)

            if self.agentcat_data.options.enable_tool_call_context:
                tools = self._inject_context_into_tools(tools)

            event.response = {"tools": [self._tool_to_dict(t) for t in tools]}
            return tools

        except Exception as e:
            event.is_error = True
            event.error = capture_exception(e)
            raise

        finally:
            self._publish_event(event, "list_tools")

    def _get_session_id(self) -> str:
        """Get the session ID for tracking.

        Returns:
            The session ID string.
        """
        try:
            return get_server_session_id(self.server)
        except Exception as e:
            write_to_log(f"Error getting session ID: {e}")
            return self.agentcat_data.session_id

    def _get_request_context(self, context: MiddlewareContext[Any]) -> Any:
        """Get the MCP request context from middleware context.

        Args:
            context: The middleware context.

        Returns:
            The MCP request context, or None if not available.
        """
        if context.fastmcp_context:
            return context.fastmcp_context.request_context
        return None

    def _publish_event(self, event: UnredactedEvent, event_name: str) -> None:
        """Publish an event if tracing is enabled.

        Args:
            event: The event to publish.
            event_name: Human-readable name for error logging.
        """
        if not self.agentcat_data.options.enable_tracing:
            return

        try:
            event_queue.publish_event(self.server, event)
        except Exception as e:
            write_to_log(f"Error publishing {event_name} event: {e}")

    def _serialize_result(self, result: Any) -> dict[str, Any]:
        """Serialize a tool result to a dictionary.

        Args:
            result: The result to serialize.

        Returns:
            Dictionary representation of the result.
        """
        if hasattr(result, "model_dump"):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        return {"content": str(result)}

    def _inject_context_into_tools(self, tools: list[Tool]) -> list[Tool]:
        """Inject context parameter into tool schemas.

        Args:
            tools: List of tools to modify.

        Returns:
            List of tools with context parameter injected.
        """
        context_description = self.agentcat_data.options.custom_context_description
        modified_tools = []

        for tool in tools:
            if tool.name == "get_more_tools":
                modified_tools.append(tool)
                continue

            try:
                tool_copy = copy.deepcopy(tool)
            except Exception as e:
                write_to_log(f"Error copying tool {tool.name}: {e}")
                modified_tools.append(tool)
                continue

            params = self._ensure_parameters_schema(tool_copy)
            self._add_context_property(params, context_description)
            self._add_to_required(params, "context")

            modified_tools.append(tool_copy)

        return modified_tools

    def _ensure_parameters_schema(self, tool: Tool) -> dict[str, Any]:
        """Ensure tool has a valid parameters schema and return it.

        Args:
            tool: The tool to check/modify.

        Returns:
            The parameters dict (created if necessary).
        """
        if not hasattr(tool, "parameters") or tool.parameters is None:
            tool.parameters = {"type": "object", "properties": {}, "required": []}

        params = tool.parameters
        if "properties" not in params:
            params["properties"] = {}

        return params

    def _add_context_property(
        self, params: dict[str, Any], description: str
    ) -> None:
        """Add or update the context property in a parameters schema.

        Args:
            params: The parameters dict to modify.
            description: The description for the context property.
        """
        properties = params["properties"]

        if "context" not in properties:
            properties["context"] = {"type": "string", "description": description}
        elif not properties["context"].get("description"):
            properties["context"]["description"] = description

    def _add_to_required(self, params: dict[str, Any], field: str) -> None:
        """Add a field to the required array if not already present.

        Args:
            params: The parameters dict to modify.
            field: The field name to add to required.
        """
        if "required" not in params:
            params["required"] = []

        required = params["required"]
        if isinstance(required, list) and field not in required:
            required.append(field)

    def _tool_to_dict(self, tool: Tool) -> dict[str, Any]:
        """Convert a tool to a dictionary for event response.

        Args:
            tool: The tool to convert.

        Returns:
            Dictionary representation of the tool.
        """
        try:
            if hasattr(tool, "model_dump"):
                return tool.model_dump()
            elif hasattr(tool, "to_mcp_tool"):
                mcp_tool = tool.to_mcp_tool()
                return mcp_tool.model_dump() if hasattr(mcp_tool, "model_dump") else {}
            else:
                return {
                    "name": getattr(tool, "name", "unknown"),
                    "description": getattr(tool, "description", ""),
                }
        except Exception as e:
            write_to_log(f"Error converting tool to dict: {e}")
            return {"name": getattr(tool, "name", "unknown")}
