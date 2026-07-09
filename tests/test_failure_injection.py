"""Adversarial failure-injection tests for the fail-open invariant.

The SDK embeds in customers' MCP servers, so no analytics code path may ever
raise into the customer's request handling, kill the worker pipeline, or
crash the process. Analytics failures must degrade to dropped events plus a
log line.

These tests inject hostile customer callbacks (identify/tags/properties/
redaction functions that raise — sync and async — or return garbage) and
hostile payloads (cyclic structures, NaN, bytes, objects with raising
__repr__/__str__, exceptions with broken __str__) into every layer:

- the request path (low-level server wrappers, FastMCP over a real in-memory
  transport)
- capture_exception on the low-level error path
- publish_custom_event (user-invoked; only ValueError/TypeError by design)
- the worker pipeline (redact -> sanitize -> truncate -> send)
- the PostHog exporter and telemetry manager isolation
"""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from mcp.server import Server as LowLevelServer
from mcp.types import CallToolRequest, CallToolRequestParams, TextContent

import agentcat
from agentcat import AgentCatOptions, track
from agentcat.modules import event_queue as event_queue_module
from agentcat.modules.event_queue import EventQueue, set_event_queue
from agentcat.modules.exceptions import capture_exception
from agentcat.modules.identify import reset_identity_cache
from agentcat.modules.internal import (
    reset_all_tracking_data,
    set_server_tracking_data,
)
from agentcat.modules.overrides.mcp_server import override_lowlevel_mcp_server
from agentcat.types import (
    AgentCatData,
    CustomEventData,
    SessionInfo,
    UnredactedEvent,
    UserIdentity,
)

from .test_utils.client import create_test_client
from .test_utils.todo_server import create_todo_server


# ---------------------------------------------------------------------------
# Hostile objects
# ---------------------------------------------------------------------------


class UnprintableError(Exception):
    """An exception whose __str__ raises (customer code can do this)."""

    def __str__(self):
        raise RuntimeError("__str__ exploded")


class Unboolable:
    """Truthiness raises (e.g. numpy arrays behave this way)."""

    def __bool__(self):
        raise ValueError("truth value is ambiguous")


class RaisingEq:
    """Equality raises (e.g. numpy arrays in bool context)."""

    def __eq__(self, other):
        raise ValueError("cannot compare")

    __hash__ = object.__hash__


class RaisingReprStr:
    """Both __repr__ and __str__ raise."""

    def __repr__(self):
        raise RuntimeError("__repr__ exploded")

    def __str__(self):
        raise RuntimeError("__str__ exploded")


def _cyclic_dict():
    d = {"a": 1}
    d["self"] = d
    return d


# ---------------------------------------------------------------------------
# Low-level server harness (exercises overrides/mcp_server.py wrappers, the
# path with NO call-site guards around identify/tags/publish)
# ---------------------------------------------------------------------------


def _make_request(name="echo", arguments=None) -> CallToolRequest:
    return CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments or {}),
    )


def _setup_lowlevel_server(raw_call_tool_handler=None, **option_kwargs):
    """Build a tracked low-level server. If raw_call_tool_handler is given it
    replaces the SDK-generated handler BEFORE the AgentCat override, so the
    wrapper's exception branch can be exercised directly."""
    server = LowLevelServer("failure-injection-server")

    @server.list_tools()
    async def list_tools():
        return []

    @server.call_tool()
    async def call_tool(name, arguments):
        return [TextContent(type="text", text="ok")]

    if raw_call_tool_handler is not None:
        server.request_handlers[CallToolRequest] = raw_call_tool_handler

    options = AgentCatOptions(
        enable_tracing=True,
        enable_tool_call_context=False,
        enable_report_missing=False,
        **option_kwargs,
    )
    data = AgentCatData(
        project_id="test_project",
        session_id="ses_failure_injection",
        session_info=SessionInfo(),
        last_activity=datetime.now(timezone.utc),
        options=options,
    )
    set_server_tracking_data(server, data)
    override_lowlevel_mcp_server(server, data)
    return server


@pytest.fixture(autouse=True)
def _reset_tracking():
    reset_all_tracking_data()
    reset_identity_cache()
    yield
    reset_all_tracking_data()
    reset_identity_cache()


class TestRequestPathHostileCallbacks:
    """Hostile identify/tags/properties callbacks must never break a request
    on the unguarded low-level wrapper path."""

    async def _call(self, server):
        handler = server.request_handlers[CallToolRequest]
        return await handler(_make_request())

    @patch("agentcat.modules.identify.event_queue")
    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_sync_identify_raises(self, mock_eq, mock_id_eq):
        def identify(request, extra):
            raise ValueError("identify blew up")

        server = _setup_lowlevel_server(identify=identify)
        result = await self._call(server)
        assert not getattr(result.root, "isError", False)

    @patch("agentcat.modules.identify.event_queue")
    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_async_identify_raises(self, mock_eq, mock_id_eq):
        async def identify(request, extra):
            raise ValueError("async identify blew up")

        server = _setup_lowlevel_server(identify=identify)
        result = await self._call(server)
        assert not getattr(result.root, "isError", False)

    @patch("agentcat.modules.identify.event_queue")
    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_identify_raises_unprintable_exception(self, mock_eq, mock_id_eq):
        """The identify hook raises an exception whose __str__ raises; even
        the SDK's own logging of the failure must not raise."""

        def identify(request, extra):
            raise UnprintableError()

        server = _setup_lowlevel_server(identify=identify)
        result = await self._call(server)
        assert not getattr(result.root, "isError", False)

    @patch("agentcat.modules.identify.event_queue")
    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_identify_returns_unboolable_garbage(self, mock_eq, mock_id_eq):
        def identify(request, extra):
            return Unboolable()

        server = _setup_lowlevel_server(identify=identify)
        result = await self._call(server)
        assert not getattr(result.root, "isError", False)

    @patch("agentcat.modules.identify.event_queue")
    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_identify_user_data_with_raising_eq(self, mock_eq, mock_id_eq):
        """Identity merging handles arbitrary user_data; values whose
        __eq__ raises must not break the second request."""

        def identify(request, extra):
            return UserIdentity(
                user_id="alice", user_name="Alice", user_data={"blob": RaisingEq()}
            )

        server = _setup_lowlevel_server(identify=identify)
        result1 = await self._call(server)
        result2 = await self._call(server)  # merges with the cached identity
        assert not getattr(result1.root, "isError", False)
        assert not getattr(result2.root, "isError", False)

    @patch("agentcat.modules.identify.event_queue")
    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_identify_user_data_cyclic(self, mock_eq, mock_id_eq):
        def identify(request, extra):
            return UserIdentity(
                user_id="alice", user_name="Alice", user_data=_cyclic_dict()
            )

        server = _setup_lowlevel_server(identify=identify)
        result1 = await self._call(server)
        result2 = await self._call(server)
        assert not getattr(result1.root, "isError", False)
        assert not getattr(result2.root, "isError", False)

    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_event_tags_callback_raises(self, mock_eq):
        server = _setup_lowlevel_server(
            event_tags=lambda req, extra: (_ for _ in ()).throw(RuntimeError("tags"))
        )
        result = await self._call(server)
        assert not getattr(result.root, "isError", False)

    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_event_tags_returns_non_dict(self, mock_eq):
        server = _setup_lowlevel_server(event_tags=lambda req, extra: ["not", "dict"])
        result = await self._call(server)
        assert not getattr(result.root, "isError", False)

    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_event_tags_returns_unprintable_keys(self, mock_eq):
        server = _setup_lowlevel_server(
            event_tags=lambda req, extra: {RaisingReprStr(): "value", "ok": "kept"}
        )
        result = await self._call(server)
        assert not getattr(result.root, "isError", False)

    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_event_properties_returns_unboolable(self, mock_eq):
        server = _setup_lowlevel_server(event_properties=lambda req, extra: Unboolable())
        result = await self._call(server)
        assert not getattr(result.root, "isError", False)

    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_async_event_tags_raises(self, mock_eq):
        async def tags(req, extra):
            raise RuntimeError("async tags")

        server = _setup_lowlevel_server(event_tags=tags)
        result = await self._call(server)
        assert not getattr(result.root, "isError", False)

    async def test_publish_pipeline_failure_does_not_break_request(self):
        """If assembling/enqueueing the event blows up inside publish_event,
        the customer's successful tool result must still be returned."""
        server = _setup_lowlevel_server()
        with patch(
            "agentcat.modules.event_queue.get_session_info",
            side_effect=RuntimeError("session info exploded"),
        ):
            handler = server.request_handlers[CallToolRequest]
            result = await handler(_make_request())
        assert not getattr(result.root, "isError", False)


class TestLowLevelErrorPathHostileExceptions:
    """capture_exception runs unguarded on the request path; exotic
    exceptions must not replace the customer's error or lose the event."""

    @patch("agentcat.modules.overrides.mcp_server.event_queue")
    async def test_unprintable_tool_exception_reraised_unchanged(self, mock_eq):
        async def raw_handler(request):
            raise UnprintableError()

        server = _setup_lowlevel_server(raw_call_tool_handler=raw_handler)
        handler = server.request_handlers[CallToolRequest]

        with pytest.raises(UnprintableError):
            await handler(_make_request())

        # The event must still be recorded, with a fallback error message
        events = [c.args[1] for c in mock_eq.publish_event.call_args_list]
        call_events = [e for e in events if e.event_type == "mcp:tools/call"]
        assert call_events, "tools/call error event not published"
        event = call_events[0]
        assert event.is_error is True
        assert isinstance(event.error, dict)
        assert event.error.get("type") == "UnprintableError"
        assert isinstance(event.error.get("message"), str)

    def test_capture_exception_unprintable_str(self):
        error_data = capture_exception(UnprintableError())
        assert error_data["type"] == "UnprintableError"
        assert isinstance(error_data["message"], str)
        assert error_data["platform"] == "python"

    def test_capture_exception_unprintable_cause_chain(self):
        e = ValueError("outer")
        e.__cause__ = UnprintableError()
        e.__suppress_context__ = True
        error_data = capture_exception(e)
        assert error_data["message"] == "outer"
        chained = error_data.get("chained_errors") or []
        assert chained and chained[0]["type"] == "UnprintableError"
        assert isinstance(chained[0]["message"], str)

    def test_capture_exception_cyclic_cause_chain(self):
        a = ValueError("a")
        b = ValueError("b")
        a.__cause__ = b
        a.__suppress_context__ = True
        b.__cause__ = a
        b.__suppress_context__ = True
        error_data = capture_exception(a)  # must terminate, not loop/raise
        assert error_data["message"] == "a"

    def test_capture_exception_non_exception_unprintable_object(self):
        error_data = capture_exception(RaisingReprStr())
        assert isinstance(error_data["message"], str)
        assert error_data["platform"] == "python"

    def test_capture_exception_base_exception_subclass(self):
        error_data = capture_exception(KeyboardInterrupt("interrupted"))
        assert error_data["type"] == "KeyboardInterrupt"


class TestFastMCPTransportHostileCallbacks:
    """End-to-end over a real in-memory transport: hostile identify must not
    break initialize or tool calls on the FastMCP (official) path."""

    async def test_unprintable_identify_over_transport(self):
        mock_api_client = MagicMock()
        test_queue = EventQueue(api_client=mock_api_client)
        original_queue = event_queue_module.event_queue
        set_event_queue(test_queue)
        try:
            server = create_todo_server()

            def identify(request, extra):
                raise UnprintableError()

            track(
                server,
                "test_project",
                AgentCatOptions(identify=identify, enable_report_missing=False),
            )

            async with create_test_client(server) as client:
                result = await client.call_tool(
                    "add_todo", {"text": "still works", "context": "testing"}
                )
                assert not result.isError
        finally:
            set_event_queue(original_queue)


class TestPublishCustomEventContract:
    """publish_custom_event may raise ValueError/TypeError on bad args (TS
    parity) but nothing else; hostile payloads are deferred to the worker."""

    def _tracked_mock_queue(self):
        return patch.object(event_queue_module, "event_queue", MagicMock())

    def test_weird_session_id_strings(self):
        weird = ["über-session🚀", "a" * 10_000, "line\nbreak\ttab", " ", "0"]
        with self._tracked_mock_queue() as mock_queue:
            for session in weird:
                agentcat.publish_custom_event(session, "proj_test")
            assert mock_queue.add.call_count == len(weird)

    def test_non_serializable_parameters_enqueued_not_raised(self):
        data = CustomEventData(
            resource_name="custom-action",
            parameters={
                "obj": object(),
                "nan": float("nan"),
                "inf": float("inf"),
                "bytes": b"\xff\xfe\x00",
                "dt": datetime.now(timezone.utc),
                "raising_repr": RaisingReprStr(),
            },
        )
        with self._tracked_mock_queue() as mock_queue:
            agentcat.publish_custom_event("session-x", "proj_test", data)
            assert mock_queue.add.call_count == 1

    def test_cyclic_parameters_enqueued_not_raised(self):
        data = CustomEventData(parameters=_cyclic_dict())
        with self._tracked_mock_queue() as mock_queue:
            agentcat.publish_custom_event("session-y", "proj_test", data)
            assert mock_queue.add.call_count == 1

    def test_non_dict_tags_dropped_not_raised(self):
        data = CustomEventData(tags="not-a-dict")
        with self._tracked_mock_queue() as mock_queue:
            agentcat.publish_custom_event("session-z", "proj_test", data)
            event = mock_queue.add.call_args.args[0]
            assert event.tags is None

    def test_bad_args_raise_by_design(self):
        with pytest.raises(ValueError):
            agentcat.publish_custom_event("session", "")
        with pytest.raises(TypeError):
            agentcat.publish_custom_event(42, "proj_test")
        with pytest.raises(ValueError):
            # An untracked server object
            agentcat.publish_custom_event(object(), "proj_test")

    def test_custom_event_type_accepted_by_model(self):
        event = UnredactedEvent(event_type="agentcat:custom")
        assert event.event_type == "agentcat:custom"


# ---------------------------------------------------------------------------
# Worker pipeline: one poisonous event must never kill the worker
# ---------------------------------------------------------------------------


def _make_worker_queue():
    captured = []
    mock_api = MagicMock()
    mock_api.publish_event = MagicMock(
        side_effect=lambda publish_event_request: captured.append(
            publish_event_request
        )
    )
    return EventQueue(api_client=mock_api), captured


def _wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def _event(resource_name, parameters=None, redaction_fn=None):
    return UnredactedEvent(
        session_id="ses_worker_test",
        project_id="proj_worker",
        event_type="mcp:tools/call",
        resource_name=resource_name,
        timestamp=datetime.now(timezone.utc),
        parameters=parameters,
        redaction_fn=redaction_fn,
    )


class TestWorkerPipelineSurvival:
    def _run(self, events, expect_delivered, timeout=8.0):
        queue, captured = _make_worker_queue()
        try:
            for event in events:
                queue.add(event)
            assert _wait_for(
                lambda: {
                    e.resource_name for e in captured
                } >= set(expect_delivered),
                timeout=timeout,
            ), (
                f"expected {expect_delivered} delivered; got "
                f"{[e.resource_name for e in captured]}"
            )
            return captured
        finally:
            queue._shutdown = True
            queue._shutdown_event.set()
            queue.executor.shutdown(wait=False, cancel_futures=True)

    def test_poisonous_payload_then_healthy(self):
        poison = _event(
            "poison",
            parameters={
                "cycle": _cyclic_dict(),
                "nan": float("nan"),
                "bytes": b"\xff\xfe",
                "raising_repr": RaisingReprStr(),
            },
        )
        healthy = _event("healthy")
        self._run([poison, healthy], expect_delivered={"healthy"})

    def test_sync_redaction_raises_drops_event_worker_survives(self):
        def bad_redact(text):
            raise RuntimeError("redaction blew up")

        poisoned = _event("poisoned", {"secret": "x"}, redaction_fn=bad_redact)
        healthy = _event("healthy", {"secret": "x"}, redaction_fn=lambda s: "REDACTED")
        captured = self._run([poisoned, healthy], expect_delivered={"healthy"})
        assert all(e.resource_name != "poisoned" for e in captured)

    def test_unprintable_redaction_error_drops_event_worker_survives(self):
        def bad_redact(text):
            raise UnprintableError()

        poisoned = _event("poisoned", {"secret": "x"}, redaction_fn=bad_redact)
        healthy = _event("healthy")
        captured = self._run([poisoned, healthy], expect_delivered={"healthy"})
        assert all(e.resource_name != "poisoned" for e in captured)

    def test_async_redaction_fn_resolved_on_worker(self):
        async def redact(text):
            return "ASYNC_REDACTED"

        event = _event("async-redacted", {"secret": "topsecret"}, redaction_fn=redact)
        captured = self._run([event], expect_delivered={"async-redacted"})
        delivered = [e for e in captured if e.resource_name == "async-redacted"][0]
        assert delivered.parameters == {"secret": "ASYNC_REDACTED"}

    def test_async_redaction_raises_drops_event_worker_survives(self):
        async def bad_redact(text):
            raise RuntimeError("async redaction blew up")

        poisoned = _event("poisoned", {"secret": "x"}, redaction_fn=bad_redact)
        healthy = _event("healthy")
        captured = self._run([poisoned, healthy], expect_delivered={"healthy"})
        assert all(e.resource_name != "poisoned" for e in captured)

    def test_redaction_returning_non_string_never_crashes_worker(self):
        poisoned = _event("poisoned", {"secret": "x"}, redaction_fn=lambda s: object())
        healthy = _event("healthy")
        self._run([poisoned, healthy], expect_delivered={"healthy"})


class TestRedactionLoopFallback:
    """redact_event resolves async redaction fns with asyncio.run on worker
    threads; when a loop is already running in the calling thread it must
    fall back to a helper thread and complete without deadlocking."""

    async def test_async_redaction_inside_running_loop_no_deadlock(self):
        from agentcat.modules.redaction import redact_event

        async def redact(text):
            return "R"

        event = _event("loop-fallback", {"secret": "y"})
        # Called from within a running event loop (this async test) — takes
        # the helper-thread fallback path and must return promptly.
        result = redact_event(event, redact)
        assert result.parameters == {"secret": "R"}

    async def test_async_redaction_raises_inside_running_loop(self):
        from agentcat.modules.redaction import redact_event

        async def redact(text):
            raise RuntimeError("boom")

        event = _event("loop-fallback-err", {"secret": "y"})
        with pytest.raises(RuntimeError):
            # Propagating is correct here: the worker's caller catches it and
            # drops the event. The requirement is no deadlock/hang.
            redact_event(event, redact)


# ---------------------------------------------------------------------------
# PostHog exporter + telemetry manager isolation
# ---------------------------------------------------------------------------


class TestPostHogExporterRobustness:
    def _exporter(self):
        from agentcat.modules.exporters.posthog import PostHogExporter

        return PostHogExporter({"type": "posthog", "api_key": "phc_test"})

    def test_network_error_does_not_raise(self):
        exporter = self._exporter()
        exporter.session = MagicMock()
        exporter.session.post.side_effect = ConnectionError("network down")
        exporter.export(_event("net-fail"))  # must not raise

    def test_serialization_error_does_not_raise(self):
        exporter = self._exporter()
        exporter.session = MagicMock()
        exporter.session.post.side_effect = TypeError("not JSON serializable")
        event = _event("bad-json", parameters={"obj": object()})
        event.id = "evt_test"
        exporter.export(event)  # must not raise

    def test_to_uuidv7_garbage_inputs(self):
        import re

        from agentcat.modules.exporters.posthog import to_uuidv7

        uuid_re = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
        for garbage in ["", "no-prefix", "ses_$$$$", None, "ses_" + "x" * 500]:
            assert uuid_re.match(to_uuidv7(garbage)), f"bad uuid for {garbage!r}"

    def test_error_event_with_hostile_error_dict(self):
        exporter = self._exporter()
        exporter.session = MagicMock()
        event = _event("err-tool")
        event.id = "evt_err"
        event.is_error = True
        event.error = {"message": "boom", "weird": RaisingReprStr()}
        exporter.export(event)  # must not raise

    def test_telemetry_manager_isolates_exporter_failures(self):
        from agentcat.modules.telemetry import TelemetryManager

        manager = TelemetryManager({})
        failing = MagicMock()
        failing.export.side_effect = RuntimeError("exporter down")
        working = MagicMock()
        manager.exporters = {"bad": failing, "good": working}

        event = _event("isolated")
        event.id = "evt_iso"
        manager.export(event)  # must not raise
        assert working.export.call_count == 1
