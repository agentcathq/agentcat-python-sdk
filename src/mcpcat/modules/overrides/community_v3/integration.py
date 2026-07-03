"""Integration module for Community FastMCP v3.

This module provides the function to apply AgentCat tracking to
FastMCP v3 servers using the middleware system.
"""

from typing import Annotated, Any

from pydantic import Field

from mcpcat.modules.logging import write_to_log
from mcpcat.modules.overrides.community_v3.middleware import AgentCatMiddleware
from mcpcat.types import AgentCatData


def apply_community_v3_integration(server: Any, agentcat_data: AgentCatData) -> None:
    """Apply AgentCat tracking to a Community FastMCP v3 server.

    This function:
    1. Creates an AgentCatMiddleware instance
    2. Inserts it at the beginning of the middleware chain (position 0)
    3. Registers get_more_tools tool if enabled

    Args:
        server: A Community FastMCP v3 server instance.
        agentcat_data: AgentCat tracking configuration.
    """
    try:
        # Create middleware instance
        middleware = AgentCatMiddleware(agentcat_data, server)

        # Insert at beginning of middleware chain (position 0)
        # This ensures AgentCat sees all requests first
        server.middleware.insert(0, middleware)
        write_to_log(
            f"Inserted AgentCatMiddleware at position 0 for server {id(server)}"
        )

        # Register get_more_tools if enabled
        if agentcat_data.options.enable_report_missing:
            _register_get_more_tools_v3(server, agentcat_data)

        write_to_log(
            f"Successfully applied Community FastMCP v3 integration "
            f"for server {id(server)}"
        )

    except Exception as e:
        write_to_log(f"Error applying Community FastMCP v3 integration: {e}")
        raise


def _register_get_more_tools_v3(server: Any, agentcat_data: AgentCatData) -> None:
    """Register the get_more_tools tool for FastMCP v3.

    Args:
        server: A Community FastMCP v3 server instance.
        agentcat_data: AgentCat tracking configuration.
    """
    from fastmcp.tools.tool import Tool

    from mcpcat.modules.tools import handle_report_missing

    # Define the get_more_tools function
    async def get_more_tools(
        context: Annotated[
            str,
            Field(
                description="A description of your goal and what kind of tool would help accomplish it."
            ),
        ],
    ) -> str:
        """Check for additional tools when your task might benefit from them."""
        result = await handle_report_missing({"context": context})

        if result.content and hasattr(result.content[0], "text"):
            return result.content[0].text
        return "No additional tools available."

    try:
        tool = Tool.from_function(
            get_more_tools,
            name="get_more_tools",
            description=(
                "Check for additional tools whenever your task might benefit from "
                "specialized capabilities - even if existing tools could work as a "
                "fallback."
            ),
        )
        server.add_tool(tool)
        write_to_log("Registered get_more_tools using server.add_tool()")

    except Exception as e:
        write_to_log(f"Error registering get_more_tools: {e}")
