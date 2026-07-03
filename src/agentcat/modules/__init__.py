"""AgentCat modules."""

from .compatibility import is_compatible_server, is_official_fastmcp_server
from .context_parameters import (
    add_context_parameter_to_schema,
    add_context_parameter_to_tools,
)
from .internal import get_server_tracking_data, set_server_tracking_data
from .logging import write_to_log
from .tools import handle_report_missing

__all__ = [
    # Compatibility
    "is_compatible_server",
    "is_official_fastmcp_server",
    # Context parameters
    "add_context_parameter_to_schema",
    "add_context_parameter_to_tools",
    # Internal
    "get_server_tracking_data",
    "set_server_tracking_data",
    # Logging
    "write_to_log",
    # Redaction
    # Session
    # Tools
    "handle_report_missing",
]
