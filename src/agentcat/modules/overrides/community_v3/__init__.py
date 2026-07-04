"""Community FastMCP v3 integration using the middleware system."""

from agentcat.modules.overrides.community_v3.integration import (
    apply_community_v3_integration,
)
from agentcat.modules.overrides.community_v3.middleware import AgentCatMiddleware

__all__ = [
    "AgentCatMiddleware",
    "apply_community_v3_integration",
]
