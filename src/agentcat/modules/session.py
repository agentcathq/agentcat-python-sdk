"""Session management for AgentCat."""

import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone

from mcp.shared.context import RequestContext
from mcp.server import Server

from agentcat.modules.constants import INACTIVITY_TIMEOUT_IN_MINUTES, SESSION_ID_PREFIX
from agentcat.modules.internal import get_server_tracking_data, set_server_tracking_data
from agentcat.modules.logging import write_to_log

from ..thirdparty.ksuid import Ksuid
from ..types import AgentCatData, SessionInfo
from ..utils import generate_prefixed_ksuid

# Fixed epoch used when deriving deterministic session IDs (2024-01-01T00:00:00Z,
# in milliseconds). Must match the TypeScript SDK's session.ts for cross-SDK
# determinism.
_DERIVED_SESSION_EPOCH_MS = 1704067200000
# Maximum timestamp offset added to the epoch (1 year, in milliseconds).
_DERIVED_SESSION_MAX_OFFSET_MS = 365 * 24 * 60 * 60 * 1000


def new_session_id() -> str:
    """Generate a new session ID."""
    return generate_prefixed_ksuid(SESSION_ID_PREFIX)


def derive_session_id_from_mcp_session(
    mcp_session_id: str, project_id: str | None = None
) -> str:
    """Create a deterministic KSUID session ID from an MCP session ID.

    The same inputs always produce the same session ID, enabling correlation
    across server restarts (mirrors the TypeScript SDK's
    deriveSessionIdFromMCPSession).

    Args:
        mcp_session_id: The session ID from the MCP protocol
        project_id: Optional AgentCat project ID to include in the hash

    Returns:
        A KSUID with the "ses" prefix derived deterministically from the inputs
    """
    input_str = f"{mcp_session_id}:{project_id}" if project_id else mcp_session_id

    # Hash the input with SHA-256
    digest = hashlib.sha256(input_str.encode("utf-8")).digest()

    # Derive a deterministic but valid timestamp from the first 4 bytes:
    # fixed 2024-01-01 epoch plus a hash-based offset capped at 1 year.
    timestamp_offset = (
        int.from_bytes(digest[:4], "big") % _DERIVED_SESSION_MAX_OFFSET_MS
    )
    timestamp_ms = _DERIVED_SESSION_EPOCH_MS + timestamp_offset

    # Use the next 16 bytes of the hash as the KSUID payload
    ksuid = Ksuid(
        datetime=datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc),
        payload=digest[4:20],
    )
    return f"{SESSION_ID_PREFIX}_{ksuid}"


def get_agentcat_version() -> str | None:
    """Get the current AgentCat SDK version."""
    try:
        import importlib.metadata

        return importlib.metadata.version("agentcat")
    except Exception:
        return None


def get_headers_from_request_context(
    request_context: RequestContext,
) -> dict[str, str] | None:
    """Safely extract HTTP headers from a request context.

    Args:
        request_context: The request context that may contain a Starlette Request object

    Returns:
        A dictionary of headers if available, None otherwise
    """
    if request_context is None:
        return None

    try:
        # Check if the context has a request object with headers
        if hasattr(request_context, "request") and request_context.request:
            request = request_context.request
            if hasattr(request, "headers"):
                return dict(request.headers)
    except Exception:
        pass

    return None


def get_client_info_from_request_context(
    server: Server, request_context: RequestContext | None
) -> tuple[str | None, str | None]:
    """Extract client information from request context or HTTP headers.

    Returns (client_name, client_version). In stateless mode, extracts per-request
    without caching. In stateful mode, caches on shared session_info.

    This function is designed to be resilient and never fail - any error is logged
    but won't affect the server operation.
    """
    # Handle None request_context (e.g., in stateless HTTP mode outside handlers)
    if request_context is None:
        write_to_log("Request context is None, skipping client info extraction")
        return (None, None)

    try:
        data = get_server_tracking_data(server)
        if not data:
            return (None, None)

        client_name: str | None = None
        client_version: str | None = None

        # In stateful mode, return cached values if already set
        if not data.is_stateless and data.session_info.client_name and data.session_info.client_version:
            return (data.session_info.client_name, data.session_info.client_version)

        try:
            # Try to get from MCP session (stateful mode)
            if hasattr(request_context, "session") and request_context.session:
                client_info = request_context.session.client_params.clientInfo
                if client_info:
                    client_name = client_info.name
                    client_version = client_info.version
                    if not data.is_stateless:
                        data.session_info.client_name = client_name
                        data.session_info.client_version = client_version
                        set_server_tracking_data(server, data)
                    return (client_name, client_version)
        except (AttributeError, TypeError):
            # This is expected in stateless mode, just continue
            pass
        except Exception as e:
            write_to_log(f"Error extracting client info from session: {e}")

        # Fallback: Try to extract from HTTP headers (stateless mode)
        try:
            headers = get_headers_from_request_context(request_context)
            if headers:
                # Parse User-Agent header (format: "ClientName/Version ...")
                user_agent = headers.get("user-agent", "")
                if user_agent:
                    match = re.match(r"^([^/]+)/([^\s]+)", user_agent)
                    if match:
                        client_name = match.group(1)
                        client_version = match.group(2)
                    else:
                        # No neat match, use the whole string as client_name
                        client_name = user_agent

                # Custom MCP headers override User-Agent if present
                if headers.get("x-mcp-client-name"):
                    client_name = headers.get("x-mcp-client-name")
                if headers.get("x-mcp-client-version"):
                    client_version = headers.get("x-mcp-client-version")

                if not data.is_stateless and (client_name or client_version):
                    data.session_info.client_name = client_name
                    data.session_info.client_version = client_version
                    set_server_tracking_data(server, data)

                if client_name or client_version:
                    write_to_log(
                        f"Extracted client info from headers: {client_name} v{client_version}"
                    )
        except Exception as e:
            write_to_log(f"Error extracting client info from headers: {e}")
            # Continue without client info

        return (client_name, client_version)
    except Exception as e:
        # Catch-all for any unexpected errors - log but never fail
        write_to_log(f"Unexpected error in get_client_info_from_request_context: {e}")
        return (None, None)


def get_session_info(server: Server, data: AgentCatData | None = None) -> SessionInfo:
    """Get session information for the current MCP session."""
    session_info = SessionInfo(
        ip_address=None,  # grab from django
        sdk_language=f"Python {sys.version_info.major}.{sys.version_info.minor}",
        agentcat_version=get_agentcat_version(),
        server_name=server.name if hasattr(server, "name") else None,
        server_version=server.version if hasattr(server, "version") else None,
        client_name=data.session_info.client_name
        if data and data.session_info and not data.is_stateless
        else None,
        client_version=data.session_info.client_version
        if data and data.session_info and not data.is_stateless
        else None,
        identify_actor_given_id=None,
        identify_actor_name=None,
        identify_data=None,
    )

    if not data:
        return session_info

    data.session_info = session_info
    set_server_tracking_data(server, data)  # Store updated data
    return data.session_info


def set_last_activity(server: Server) -> None:
    data = get_server_tracking_data(server)

    if not data:
        raise Exception("AgentCat data not initialized for this server")

    data.last_activity = datetime.now(timezone.utc)
    set_server_tracking_data(server, data)


def get_server_session_id(server: Server) -> str | None:
    data = get_server_tracking_data(server)

    if not data:
        raise Exception("AgentCat data not initialized for this server")

    if data.is_stateless:
        return None

    now = datetime.now(timezone.utc)
    timeout = timedelta(minutes=INACTIVITY_TIMEOUT_IN_MINUTES)
    # If last activity timed out
    if now - data.last_activity > timeout:
        data.session_id = new_session_id()
        set_server_tracking_data(server, data)
    set_last_activity(server)

    return data.session_id
