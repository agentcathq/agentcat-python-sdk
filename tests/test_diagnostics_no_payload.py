"""The privacy guard: diagnostics logs carry metadata only — never payloads.

Strategy mirrors the TS suite: keep the real diagnostics module OFF-network by
setting DISABLE_DIAGNOSTICS=1 (so init_diagnostics won't register its own sink),
then register our own capturing sink to inspect every entry write_to_log emits
while a real tracked server handles real tool calls + identify.
"""

import time

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules import diagnostics
from agentcat.modules.event_queue import EventQueue, set_event_queue
from agentcat.modules.logging import set_diagnostics_sink
from agentcat.types import UserIdentity

from .test_utils.client import create_test_client
from .test_utils.todo_server import create_todo_server

SECRET_ARG = "topsecret-todo-xyz"
SECRET_CONTEXT = "confidential-reason-for-needing-a-tool-uvw"
SECRET_NAME = "Secret Agent Name"
SECRET_DATA_VALUE = "ssn-000-secret"


@pytest.fixture
def captured(monkeypatch):
    # Keep the real diagnostics module off-network; install our own sink.
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "1")
    diagnostics._reset_diagnostics_for_test()

    from unittest.mock import MagicMock

    mock_api_client = MagicMock()
    mock_api_client.publish_event = MagicMock(return_value=None)
    set_event_queue(EventQueue(api_client=mock_api_client))

    seen: list[str] = []
    set_diagnostics_sink(seen.append)
    try:
        yield seen
    finally:
        set_diagnostics_sink(None)
        diagnostics._reset_diagnostics_for_test()
        set_event_queue(EventQueue())


async def test_diagnostics_never_leak_payloads_or_identity(captured):
    def identify_fn(request, context):
        return UserIdentity(
            user_id="actor-1",
            user_name=SECRET_NAME,
            user_data={"ssn": SECRET_DATA_VALUE},
        )

    server = create_todo_server()
    track(
        server,
        "test-project",
        AgentCatOptions(
            enable_report_missing=True,
            enable_tracing=True,
            identify=identify_fn,
        ),
    )

    async with create_test_client(server) as client:
        await client.call_tool("add_todo", {"text": SECRET_ARG})
        await client.call_tool("get_more_tools", {"context": SECRET_CONTEXT})

    # Let the event-queue worker drain so success metadata lines are emitted too.
    time.sleep(1.5)

    all_logs = "\n".join(captured)

    # Setup beacons present + metadata-only.
    assert "AgentCat setup started" in all_logs
    assert "test-project" in all_logs
    assert "AgentCat setup complete" in all_logs
    assert "tracing=True" in all_logs

    # Report-missing logs only the context length, never the context text.
    assert f"context length: {len(SECRET_CONTEXT)}" in all_logs

    # No payload / identity leaks anywhere.
    assert "Event details" not in all_logs
    for secret in (SECRET_ARG, SECRET_CONTEXT, SECRET_NAME, SECRET_DATA_VALUE):
        assert secret not in all_logs, f"diagnostics leaked secret: {secret!r}"
