"""MCPCat - Analytics Tool for MCP Servers."""

import os
import warnings
from datetime import datetime, timezone
from importlib.metadata import version
from typing import Any

__version__ = version("mcpcat")

from mcpcat.modules.overrides.mcp_server import override_lowlevel_mcp_server
from mcpcat.modules.session import get_session_info, new_session_id

from .modules.compatibility import (
    COMPATIBILITY_ERROR_MESSAGE,
    is_community_fastmcp_v2,
    is_community_fastmcp_v3,
    is_compatible_server,
    is_official_fastmcp_server,
)
from .modules.diagnostics import init_diagnostics
from .modules.internal import set_server_tracking_data
from .modules.logging import set_debug_mode, write_to_log
from .types import (
    EventPropertiesFunction,
    EventTagsFunction,
    IdentifyFunction,
    MCPCatData,
    MCPCatOptions,
    RedactionFunction,
    UserIdentity,
)


def _detect_stateless(server) -> bool:
    """Auto-detect stateless mode from FastMCP server settings.

    Best-effort: community FastMCP v3 deprecated per-instance .settings
    in favor of global fastmcp.settings, but the global isn't per-server.
    The deprecated shim is the only per-instance API available.
    MCPCatOptions(stateless=True) is the recommended explicit path.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = server.settings.stateless_http
            if result:
                write_to_log(
                    "Auto-detected stateless HTTP mode from your FastMCP server's .settings. "
                    "If this is incorrect, please pass stateless=False to MCPCatOptions and file a bug report."
                )
            return result
    except (AttributeError, RuntimeError):
        return False


def track(
    server: Any, project_id: str | None = None, options: MCPCatOptions | None = None
) -> Any:
    """
    Initialize MCPCat tracking with optional telemetry export.

    Args:
        server: MCP server instance to track
        project_id: MCPCat project ID (optional if using telemetry-only mode)
        options: Configuration options including telemetry exporters

    Returns:
        The server instance with tracking enabled

    Raises:
        ValueError: If neither project_id nor exporters are provided
        TypeError: If server is not a compatible MCP server instance

    Example:
        Attach custom metadata to every auto-captured event using
        `event_tags` (string key-value pairs, validated) and
        `event_properties` (flexible JSON). See
        https://docs.mcpcat.io/sdk/event-tags-properties.

        >>> import os, mcpcat
        >>> mcpcat.track(server, "proj_abc123", mcpcat.MCPCatOptions(
        ...     event_tags=lambda req, ctx: {"env": os.environ.get("APP_ENV", "dev")},
        ...     event_properties=lambda req, ctx: {"feature_flags": ["dark_mode"]},
        ... ))
    """
    if options is None:
        options = MCPCatOptions()

    set_debug_mode(options.debug_mode)

    # Initialize internal diagnostics before anything can fail, so even an
    # invalid setup still emits a failure beacon. Never throws into the host.
    init_diagnostics(project_id, disabled=options.disable_diagnostics)

    # Wrap the whole setup so any failure emits a diagnostic. Config-contract
    # errors (ValueError/TypeError) still propagate; tracking-application errors
    # are logged but never break the host (server is still returned).
    try:
        if not project_id and not options.exporters:
            raise ValueError(
                "Either project_id or exporters must be provided. "
                "Use project_id for MCPCat, exporters for telemetry-only mode, or both."
            )

        if not is_compatible_server(server):
            raise TypeError(COMPATIBILITY_ERROR_MESSAGE)

        is_community_v3 = is_community_fastmcp_v3(server)
        is_community_v2 = is_community_fastmcp_v2(server)
        is_official_fastmcp = is_official_fastmcp_server(server)
        is_fastmcp_v2 = is_official_fastmcp or is_community_v2

        # Determine where to store tracking data:
        # - v2 FastMCP servers use server._mcp_server
        # - v3 and low-level servers use the server itself
        if is_fastmcp_v2:
            lowlevel_server = server._mcp_server
        else:
            lowlevel_server = server

        # Metadata-only setup-started beacon (INFO — no fail/error/Warning).
        server_kind = (
            "fastmcp-v2"
            if is_fastmcp_v2
            else "fastmcp-v3"
            if is_community_v3
            else "lowlevel"
        )
        write_to_log(
            f"MCPCat setup started | project {project_id or '(telemetry-only)'} | "
            f"server {server_kind}"
        )

        if options.exporters:
            from mcpcat.modules.event_queue import set_telemetry_manager
            from mcpcat.modules.telemetry import TelemetryManager

            telemetry_manager = TelemetryManager(options.exporters)
            set_telemetry_manager(telemetry_manager)
            write_to_log(
                f"Telemetry initialized with {len(options.exporters)} exporter(s)"
            )

        session_id = new_session_id()
        session_info = get_session_info(lowlevel_server)
        data = MCPCatData(
            session_id=session_id,
            project_id=project_id,
            last_activity=datetime.now(timezone.utc),
            session_info=session_info,
            options=options,
            is_stateless=options.stateless if options.stateless is not None else _detect_stateless(server),
        )
        set_server_tracking_data(lowlevel_server, data)

        # Resolve API base URL: option > new env var > legacy env var > default
        api_base_url = (
            options.api_base_url
            or os.environ.get("AGENTCAT_API_URL")
            or os.environ.get("MCPCAT_API_URL")
        )
        if api_base_url:
            from mcpcat.modules.event_queue import event_queue
            event_queue.configure(api_base_url)

        if not data.tracker_initialized:
            data.tracker_initialized = True
            write_to_log(
                f"Dynamic tracking initialized for server {id(lowlevel_server)}"
            )

        _apply_server_tracking(
            server, lowlevel_server, data,
            is_community_v3, is_official_fastmcp, is_community_v2
        )

        if project_id:
            write_to_log(
                f"MCPCat initialized with dynamic tracking for session "
                f"{session_id} on project {project_id}"
            )
        else:
            write_to_log(
                f"MCPCat initialized in telemetry-only mode for session {session_id}"
            )

        # Metadata-only setup-complete beacon (INFO). A start-without-complete
        # (or the ERROR diagnostics below) signals a failed setup.
        write_to_log(
            f"MCPCat setup complete | project {project_id or '(telemetry-only)'} | "
            f"tracing={options.enable_tracing} "
            f"context={options.enable_tool_call_context} "
            f"report_missing={options.enable_report_missing} "
            f"exporters={len(options.exporters) if options.exporters else 0}"
        )

    except (ValueError, TypeError) as e:
        # Config-contract failures: emit a failure diagnostic, then propagate so
        # callers still see the error (preserves existing public behavior).
        write_to_log(f"Warning: Failed to track server - {e}")
        raise
    except Exception as e:
        write_to_log(f"Error initializing MCPCat: {e}")

    return server


def _apply_server_tracking(
    server: Any,
    lowlevel_server: Any,
    data: MCPCatData,
    is_community_v3: bool,
    is_official_fastmcp: bool,
    is_community_v2: bool,
) -> None:
    """Apply the appropriate tracking method based on server type."""
    if is_community_v3:
        from mcpcat.modules.overrides.community_v3.integration import (
            apply_community_v3_integration,
        )

        apply_community_v3_integration(server, data)
        write_to_log(
            f"Applied Community FastMCP v3 middleware for server {id(server)}"
        )

    elif is_official_fastmcp:
        from mcpcat.modules.overrides.mcp_server import (
            override_lowlevel_mcp_server_minimal,
        )
        from mcpcat.modules.overrides.official.monkey_patch import (
            apply_official_fastmcp_patches,
        )

        apply_official_fastmcp_patches(server, data)
        override_lowlevel_mcp_server_minimal(lowlevel_server, data)

    elif is_community_v2:
        from mcpcat.modules.overrides.community.monkey_patch import (
            patch_community_fastmcp,
        )

        patch_community_fastmcp(server)
        write_to_log(f"Applied Community FastMCP v2 patches for server {id(server)}")

    else:
        override_lowlevel_mcp_server(lowlevel_server, data)


__all__ = [
    # Main API
    "track",
    # Configuration
    "MCPCatOptions",
    # Types for identify functionality
    "UserIdentity",
    "IdentifyFunction",
    # Type for redaction functionality
    "RedactionFunction",
    # Types for event metadata callbacks
    "EventTagsFunction",
    "EventPropertiesFunction",
]
