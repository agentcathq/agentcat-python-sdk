"""Per-request "extra" extraction.

Builds a serializable dict that mirrors the TypeScript SDK's
`parameters.extra` shape so customers can reach
`event.parameters.extra.requestInfo.headers` on every transport-aware event.

HTTP / SSE / Streamable HTTP transports populate `requestInfo.headers` from the
Starlette `Request`. stdio transports omit `requestInfo` entirely (parity with
the TS SDK, which only sets `requestInfo` on HTTP-based transports).
"""

from __future__ import annotations

from typing import Any, Optional

from agentcat.modules.logging import write_to_log


def _headers_to_dict(request: Any) -> Optional[dict]:
    """Read a Starlette-style multi-mapping into a JSON-friendly dict.

    Multi-valued headers (e.g. `Set-Cookie`, `X-Forwarded-For`) are preserved
    as `list[str]`; single-valued headers stay as `str`. Mirrors the TS SDK's
    `IsomorphicHeaders = Record<string, string | string[]>`.
    """
    try:
        headers = getattr(request, "headers", None)
        if headers is None:
            return None

        # Starlette `Headers` exposes `.raw` as list[tuple[bytes, bytes]] which
        # preserves duplicates; everything else falls back to a flat dict.
        raw = getattr(headers, "raw", None)
        if raw and isinstance(raw, list):
            collected: dict[str, Any] = {}
            for key_b, value_b in raw:
                try:
                    key = key_b.decode("latin-1") if isinstance(key_b, (bytes, bytearray)) else str(key_b)
                    value = value_b.decode("latin-1") if isinstance(value_b, (bytes, bytearray)) else str(value_b)
                except Exception:
                    continue
                key_lower = key.lower()
                existing = collected.get(key_lower)
                if existing is None:
                    collected[key_lower] = value
                elif isinstance(existing, list):
                    existing.append(value)
                else:
                    collected[key_lower] = [existing, value]
            return collected

        return dict(headers)
    except Exception as e:
        write_to_log(f"extract_request_extra: header read failed: {e}")
        return None


def _meta_to_dict(meta: Any) -> Optional[dict]:
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta
    for attr in ("model_dump", "dict"):
        fn = getattr(meta, attr, None)
        if callable(fn):
            try:
                dumped = fn()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                pass
    try:
        return dict(meta)
    except Exception:
        return None


def _get_request_object(request_context: Any, fastmcp_context: Any | None) -> Any:
    """Locate a Starlette-style Request object across transports.

    Order:
      1. `request_context.request` (official mcp SDK & FastMCP v3 propagated path)
      2. FastMCP's own `_current_http_request` ContextVar via
         `fastmcp.server.dependencies.get_http_request`. Used when the MCP
         RequestContext has not been built yet (e.g. FastMCP v3 `on_initialize`
         middleware fires before `request_context.request` is populated).

    Returns None for stdio or any failure mode.
    """
    try:
        request = getattr(request_context, "request", None)
    except Exception:
        request = None

    if request is not None:
        return request

    if fastmcp_context is not None:
        try:
            from fastmcp.server.dependencies import get_http_request  # type: ignore

            return get_http_request()
        except Exception:
            return None

    return None


def extract_request_extra(
    request_context: Any,
    fastmcp_context: Any | None = None,
) -> dict:
    """Build a JSON-serializable `extra` dict for the current request.

    Returns a dict with up to these keys (all optional, omitted when absent):
      - requestInfo: { headers: dict[str, str | list[str]] }   # HTTP transports
      - requestId:   JSON-RPC request id (str/int)
      - sessionId:   MCP session id (mcp-session-id header or session.session_id)
      - meta:        JSON-RPC `_meta` dict (progressToken, client_id, ...)

    Never raises. On any failure returns `{}`.

    Args:
        request_context: An `mcp.shared.context.RequestContext` (or compatible),
            typically obtained from `server.request_context` or
            `MiddlewareContext.fastmcp_context.request_context`.
        fastmcp_context: Optional FastMCP `Context` wrapper, used to fall back
            to `ctx.session_id` and to FastMCP's `get_http_request()` ContextVar
            when the MCP `request_context` hasn't been populated yet.
    """
    extra: dict[str, Any] = {}

    if request_context is None and fastmcp_context is None:
        return extra

    request = _get_request_object(request_context, fastmcp_context)

    if request is not None:
        headers = _headers_to_dict(request)
        if headers is not None:
            extra["requestInfo"] = {"headers": headers}

    try:
        request_id = getattr(request_context, "request_id", None)
        if request_id is not None:
            extra["requestId"] = request_id
    except Exception:
        pass

    try:
        meta = getattr(request_context, "meta", None)
        meta_dict = _meta_to_dict(meta)
        if meta_dict:
            extra["meta"] = meta_dict
    except Exception:
        pass

    session_id = None
    if request is not None:
        try:
            headers = getattr(request, "headers", None)
            if headers is not None:
                session_id = headers.get("mcp-session-id")
        except Exception:
            session_id = None

    if not session_id:
        try:
            session = getattr(request_context, "session", None)
            session_id = getattr(session, "session_id", None) if session else None
        except Exception:
            session_id = None

    if not session_id and fastmcp_context is not None:
        try:
            session_id = getattr(fastmcp_context, "session_id", None)
        except Exception:
            session_id = None

    if session_id:
        extra["sessionId"] = session_id

    return extra


def params_with_extra(
    params_dump: dict | None,
    request_context: Any,
    fastmcp_context: Any | None = None,
) -> dict:
    """Merge an MCP request's params dict with the per-request `extra` dict.

    Mirrors the TypeScript SDK so `event.parameters.extra.requestInfo.headers`
    is populated for HTTP/SSE/Streamable-HTTP and absent for stdio. Used by
    every event-emit site so behavior stays uniform across transport adapters.
    """
    base = dict(params_dump) if params_dump else {}
    extra = extract_request_extra(request_context, fastmcp_context)
    if extra:
        base["extra"] = extra
    return base
