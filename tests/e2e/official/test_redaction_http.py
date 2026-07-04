"""Redaction over real-wire payloads.

KNOWN BUG (xfail-tracked): `agentcat.modules.redaction.redact_event` only
recurses into `dict` and `list` types, not Pydantic `UnredactedEvent` objects.
The event_queue worker invokes `redact_event(event, ...)` where `event` is an
`UnredactedEvent`; the call returns the input unchanged, so customer-supplied
redact functions never actually run on the live event-publish path.

Tests below are marked xfail so they:
1. Codify the intended behavior.
2. Serve as a regression target — when the bug is fixed, they should be
   un-xfailed (the strict=False xfail still passes if the test starts working).
"""

from __future__ import annotations

import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agentcat.modules.internal import get_server_tracking_data


pytestmark = pytest.mark.e2e


def _set_redact(server, fn) -> None:
    data = get_server_tracking_data(server)
    assert data is not None
    data.options.redact_sensitive_information = fn


@pytest.mark.xfail(
    reason="redact_event does not recurse into Pydantic UnredactedEvent; "
    "redaction never fires on real events. Track as separate fix.",
    strict=False,
)
@pytest.mark.asyncio
async def test_redact_function_runs_on_real_event_payload(
    official_http_server, capture_queue
):
    url, server = official_http_server

    def redact(s: str) -> str:
        return s.replace("secret-todo-text", "[REDACTED]")

    _set_redact(server, redact)
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool(
                    "add_todo",
                    {"text": "secret-todo-text", "context": "redact"},
                )

        time.sleep(0.5)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert call_events
        params = call_events[0].parameters or {}
        text = (params.get("arguments") or {}).get("text", "")
        assert "secret-todo-text" not in text
        assert "[REDACTED]" in text
    finally:
        _set_redact(server, None)


@pytest.mark.xfail(
    reason="redact_event does not recurse into Pydantic UnredactedEvent; "
    "redaction never fires on real events. Track as separate fix.",
    strict=False,
)
@pytest.mark.asyncio
async def test_redaction_can_scrub_authorization_header_in_extra(
    official_http_server, capture_queue
):
    url, server = official_http_server

    def redact(s: str) -> str:
        if isinstance(s, str) and s.startswith("Bearer "):
            return "Bearer [REDACTED]"
        return s

    _set_redact(server, redact)
    try:
        async with streamablehttp_client(
            url, headers={"Authorization": "Bearer super-secret-token-xyz"}
        ) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool(
                    "add_todo", {"text": "auth", "context": "auth"}
                )

        time.sleep(0.5)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert call_events
        headers = (
            (call_events[0].parameters or {})
            .get("extra", {})
            .get("requestInfo", {})
            .get("headers", {})
        )
        auth = headers.get("authorization")
        assert auth is not None
        assert "super-secret-token-xyz" not in auth
        assert auth == "Bearer [REDACTED]"
    finally:
        _set_redact(server, None)


@pytest.mark.xfail(
    reason="redact_event does not invoke the user's redact fn on Pydantic "
    "events, so redact-fn-raise never fires; the 'drop event on raise' path "
    "is unreachable until the redact_event recursion bug is fixed.",
    strict=False,
)
@pytest.mark.asyncio
async def test_redaction_failure_drops_event(official_http_server, capture_queue):
    url, server = official_http_server

    def redact(_s: str) -> str:
        raise RuntimeError("redaction exploded")

    _set_redact(server, redact)
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool(
                    "add_todo", {"text": "drop", "context": "drop"}
                )

        time.sleep(1.0)
        call_events = [e for e in capture_queue if e.event_type == "mcp:tools/call"]
        assert not call_events, (
            f"redaction failure must drop event, got {len(call_events)}"
        )
    finally:
        _set_redact(server, None)
