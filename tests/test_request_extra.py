"""Tests for HTTP request `extra` capture on events.

Mirrors the TypeScript SDK behavior: `event.parameters.extra.requestInfo.headers`
is populated for HTTP-based transports (Streamable HTTP, SSE) and absent for
stdio. Other request-scoped metadata (request_id, mcp-session-id, JSON-RPC
`_meta`) is also included when available.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.event_queue import EventQueue, set_event_queue
from agentcat.modules.request_extra import (
    extract_request_extra,
    params_with_extra,
)

from .test_utils.client import create_test_client
from .test_utils.todo_server import create_todo_server


def _http_request_context(headers: dict, request_id="req-123", session=None, meta=None):
    """Build a mock RequestContext that mimics an HTTP-transport request."""
    request = SimpleNamespace(headers=headers)
    return SimpleNamespace(
        request=request,
        request_id=request_id,
        session=session,
        meta=meta,
    )


def _stdio_request_context(request_id="req-456"):
    """Build a mock RequestContext that mimics a stdio-transport request."""
    return SimpleNamespace(
        request=None,
        request_id=request_id,
        session=None,
        meta=None,
    )


class TestExtractRequestExtra:
    """Unit tests for the extract_request_extra helper."""

    def test_http_headers_populate_request_info(self):
        ctx = _http_request_context(
            headers={"x-demo": "abc", "user-agent": "Cursor/2.6.22"},
        )
        extra = extract_request_extra(ctx)
        assert "requestInfo" in extra
        assert extra["requestInfo"]["headers"]["x-demo"] == "abc"
        assert extra["requestInfo"]["headers"]["user-agent"] == "Cursor/2.6.22"

    def test_request_id_included(self):
        ctx = _http_request_context(headers={}, request_id="req-xyz")
        extra = extract_request_extra(ctx)
        assert extra["requestId"] == "req-xyz"

    def test_session_id_from_header(self):
        ctx = _http_request_context(
            headers={"mcp-session-id": "sess-from-header"},
        )
        extra = extract_request_extra(ctx)
        assert extra["sessionId"] == "sess-from-header"

    def test_session_id_from_session_object(self):
        session = SimpleNamespace(session_id="sess-from-object")
        ctx = _http_request_context(headers={}, session=session)
        extra = extract_request_extra(ctx)
        assert extra["sessionId"] == "sess-from-object"

    def test_session_id_header_wins_over_session_object(self):
        session = SimpleNamespace(session_id="sess-from-object")
        ctx = _http_request_context(
            headers={"mcp-session-id": "sess-from-header"},
            session=session,
        )
        extra = extract_request_extra(ctx)
        assert extra["sessionId"] == "sess-from-header"

    def test_meta_dict_passthrough(self):
        ctx = _http_request_context(
            headers={},
            meta={"progressToken": "tok-1", "client_id": "cid-1"},
        )
        extra = extract_request_extra(ctx)
        assert extra["meta"] == {"progressToken": "tok-1", "client_id": "cid-1"}

    def test_meta_pydantic_like_object_dumped(self):
        meta_obj = SimpleNamespace(model_dump=lambda: {"progressToken": "tok-2"})
        ctx = _http_request_context(headers={}, meta=meta_obj)
        extra = extract_request_extra(ctx)
        assert extra["meta"] == {"progressToken": "tok-2"}

    def test_stdio_omits_request_info(self):
        ctx = _stdio_request_context()
        extra = extract_request_extra(ctx)
        assert "requestInfo" not in extra
        assert extra.get("requestId") == "req-456"

    def test_none_context_yields_empty(self):
        assert extract_request_extra(None) == {}

    def test_fastmcp_session_id_fallback(self):
        ctx = _stdio_request_context()
        fastmcp_ctx = SimpleNamespace(session_id="fmcp-sess")
        extra = extract_request_extra(ctx, fastmcp_ctx)
        assert extra["sessionId"] == "fmcp-sess"

    def test_helper_never_raises(self):
        """A misbehaving context must not propagate exceptions."""

        class Boom:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        # Should not raise
        result = extract_request_extra(Boom())
        assert isinstance(result, dict)

    def test_multi_valued_headers_preserved_as_list(self):
        """Starlette-style raw headers with duplicates should yield list[str]."""

        class FakeStarletteHeaders:
            # Mimic Starlette's `Headers.raw` shape: list of (bytes, bytes).
            raw = [
                (b"x-forwarded-for", b"10.0.0.1"),
                (b"x-forwarded-for", b"10.0.0.2"),
                (b"set-cookie", b"a=1"),
                (b"set-cookie", b"b=2"),
                (b"set-cookie", b"c=3"),
                (b"x-single", b"only"),
            ]

            def get(self, key, default=None):
                # Used for mcp-session-id lookup; return None
                return default

        request = SimpleNamespace(headers=FakeStarletteHeaders())
        ctx = SimpleNamespace(
            request=request, request_id="req-multi", session=None, meta=None
        )

        extra = extract_request_extra(ctx)
        headers = extra["requestInfo"]["headers"]
        assert headers["x-forwarded-for"] == ["10.0.0.1", "10.0.0.2"]
        assert headers["set-cookie"] == ["a=1", "b=2", "c=3"]
        assert headers["x-single"] == "only"

    def test_fastmcp_get_http_request_fallback(self, monkeypatch):
        """When request_context.request is None but a FastMCP context is supplied,
        the helper should fall back to fastmcp.server.dependencies.get_http_request().
        """
        import sys
        import types

        fake_request = SimpleNamespace(
            headers={"x-fastmcp-fallback": "yes", "mcp-session-id": "fmcp-sess"}
        )
        fake_module = types.ModuleType("fastmcp.server.dependencies")
        fake_module.get_http_request = lambda: fake_request
        # Ensure parent packages exist so `from fastmcp.server.dependencies import ...` resolves.
        for parent in ("fastmcp", "fastmcp.server"):
            if parent not in sys.modules:
                monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
        monkeypatch.setitem(
            sys.modules, "fastmcp.server.dependencies", fake_module
        )

        request_context = SimpleNamespace(
            request=None, request_id="req-init", session=None, meta=None
        )
        fastmcp_context = SimpleNamespace(session_id=None)

        extra = extract_request_extra(request_context, fastmcp_context)
        headers = extra["requestInfo"]["headers"]
        assert headers.get("x-fastmcp-fallback") == "yes"
        assert extra["sessionId"] == "fmcp-sess"

    def test_fastmcp_fallback_swallows_runtime_error(self, monkeypatch):
        """get_http_request raises RuntimeError under stdio — must be swallowed."""
        import sys
        import types

        fake_module = types.ModuleType("fastmcp.server.dependencies")

        def _raise():
            raise RuntimeError("No active HTTP request found.")

        fake_module.get_http_request = _raise
        for parent in ("fastmcp", "fastmcp.server"):
            if parent not in sys.modules:
                monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
        monkeypatch.setitem(
            sys.modules, "fastmcp.server.dependencies", fake_module
        )

        request_context = SimpleNamespace(
            request=None, request_id="req-stdio", session=None, meta=None
        )
        extra = extract_request_extra(request_context, SimpleNamespace())
        assert "requestInfo" not in extra
        assert extra["requestId"] == "req-stdio"


class TestParamsWithExtra:
    """params_with_extra is the public emit-site helper used by all overrides."""

    def test_merges_dump_with_extra(self):
        ctx = SimpleNamespace(
            request=SimpleNamespace(headers={"x-h": "v"}),
            request_id="r1",
            session=None,
            meta=None,
        )
        merged = params_with_extra({"name": "tool", "arguments": {"a": 1}}, ctx)
        assert merged["name"] == "tool"
        assert merged["arguments"] == {"a": 1}
        assert merged["extra"]["requestInfo"]["headers"]["x-h"] == "v"

    def test_omits_extra_key_when_no_context(self):
        merged = params_with_extra({"name": "tool"}, None)
        assert merged == {"name": "tool"}
        assert "extra" not in merged

    def test_handles_none_dump(self):
        merged = params_with_extra(None, None)
        assert merged == {}

    def test_does_not_mutate_caller_dump(self):
        original = {"name": "tool"}
        ctx = SimpleNamespace(
            request=SimpleNamespace(headers={"x": "y"}),
            request_id=None,
            session=None,
            meta=None,
        )
        merged = params_with_extra(original, ctx)
        assert "extra" in merged
        assert "extra" not in original


class TestExtraOnPublishedEvents:
    """End-to-end: extra is attached to the event payload that the queue publishes."""

    @pytest.fixture(autouse=True)
    def restore_queue(self):
        from agentcat.modules.event_queue import event_queue as original_queue
        yield
        set_event_queue(original_queue)

    @pytest.mark.asyncio
    async def test_stdio_path_omits_request_info(self):
        """In-memory client (stdio-equivalent) should not produce requestInfo."""
        mock_api_client = MagicMock()
        captured_events: list = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)
        set_event_queue(EventQueue(api_client=mock_api_client))

        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tracing=True))

        async with create_test_client(server) as client:
            await client.call_tool(
                "add_todo", {"text": "hello", "context": "stdio test"}
            )
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert tool_events, f"expected a tools/call event, got {[e.event_type for e in captured_events]}"
        params = tool_events[0].parameters or {}
        extra = params.get("extra") or {}
        assert "requestInfo" not in extra, (
            f"stdio transport should not populate requestInfo, got: {extra}"
        )

    @pytest.mark.asyncio
    async def test_http_simulated_path_populates_headers(self, monkeypatch):
        """When the request_context exposes a Starlette-like request, headers ride along."""
        mock_api_client = MagicMock()
        captured_events: list = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)
        set_event_queue(EventQueue(api_client=mock_api_client))

        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tracing=True))

        # Fake an HTTP request_context for the duration of the call. Patch BOTH
        # the module symbol and the import sites so monkey-patched call sites pick it up.
        fake_ctx = _http_request_context(
            headers={
                "x-demo-header": "demo-value",
                "user-agent": "Cursor/2.6.22",
                "mcp-session-id": "sess-abc",
            },
            request_id="req-tools-call",
            meta={"progressToken": "tok-7"},
        )

        from agentcat.modules.overrides import mcp_server as mcp_server_mod
        from agentcat.modules.overrides.official import monkey_patch as official_mp

        monkeypatch.setattr(
            mcp_server_mod, "safe_request_context", lambda _server: fake_ctx
        )
        monkeypatch.setattr(
            official_mp, "safe_request_context", lambda _server: fake_ctx
        )

        async with create_test_client(server) as client:
            await client.call_tool(
                "add_todo", {"text": "world", "context": "http test"}
            )
            time.sleep(1.0)

        tool_events = [e for e in captured_events if e.event_type == "mcp:tools/call"]
        assert tool_events, "expected a tools/call event"
        params = tool_events[0].parameters or {}
        extra = params.get("extra")
        assert extra is not None, f"expected extra dict, got params={params}"

        request_info = extra.get("requestInfo")
        assert request_info is not None, f"expected requestInfo, got extra={extra}"
        headers = request_info.get("headers", {})
        assert headers.get("x-demo-header") == "demo-value"
        assert headers.get("user-agent") == "Cursor/2.6.22"
        assert headers.get("mcp-session-id") == "sess-abc"
        assert extra.get("requestId") == "req-tools-call"
        assert extra.get("sessionId") == "sess-abc"
        assert extra.get("meta") == {"progressToken": "tok-7"}

    @pytest.mark.asyncio
    async def test_list_tools_event_includes_extra(self, monkeypatch):
        """tools/list events should also carry parameters.extra when HTTP-shaped."""
        mock_api_client = MagicMock()
        captured_events: list = []

        def capture_event(publish_event_request):
            captured_events.append(publish_event_request)

        mock_api_client.publish_event = MagicMock(side_effect=capture_event)
        set_event_queue(EventQueue(api_client=mock_api_client))

        server = create_todo_server()
        track(server, "test_project", AgentCatOptions(enable_tracing=True))

        fake_ctx = _http_request_context(
            headers={"x-list-header": "list-value"},
            request_id="req-list",
        )
        from agentcat.modules.overrides import mcp_server as mcp_server_mod

        monkeypatch.setattr(
            mcp_server_mod, "safe_request_context", lambda _server: fake_ctx
        )

        async with create_test_client(server) as client:
            await client.list_tools()
            time.sleep(1.0)

        list_events = [e for e in captured_events if e.event_type == "mcp:tools/list"]
        assert list_events, (
            f"expected a tools/list event, got {[e.event_type for e in captured_events]}"
        )
        params = list_events[0].parameters or {}
        extra = params.get("extra") or {}
        headers = (extra.get("requestInfo") or {}).get("headers") or {}
        assert headers.get("x-list-header") == "list-value", (
            f"expected list_tools event extra to include header, got params={params}"
        )
