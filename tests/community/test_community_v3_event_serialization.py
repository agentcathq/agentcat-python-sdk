"""End-to-end: tools/list events must be JSON-serializable (agentcat 1.0.1 data loss).

On 1.0.1 every ``tools/list`` event carried the FastMCP tool's ``fn`` callable and
``tags`` set (via ``_tool_to_dict`` -> ``tool.model_dump()``), so truncation logged
``Unable to serialize unknown type: <class 'function'>`` and the event was then
dropped by the API client (``'set' object has no attribute '__dict__'``). These
tests drive a real ``tools/list`` and assert the captured event survives.
"""

import json
import time
from unittest.mock import MagicMock

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules.event_queue import EventQueue, set_event_queue

from ..test_utils.community_client import (
    HAS_COMMUNITY_CLIENT,
    create_community_test_client,
)
from ..test_utils.community_todo_server import (
    HAS_COMMUNITY_FASTMCP,
    create_community_todo_server,
)

pytestmark = pytest.mark.skipif(
    not (HAS_COMMUNITY_FASTMCP and HAS_COMMUNITY_CLIENT),
    reason="Community FastMCP not available",
)


@pytest.fixture
def captured_events():
    from agentcat.modules.event_queue import event_queue as original_queue

    events: list = []
    mock_api_client = MagicMock()
    mock_api_client.publish_event = MagicMock(
        side_effect=lambda publish_event_request: events.append(publish_event_request)
    )
    set_event_queue(EventQueue(api_client=mock_api_client))
    try:
        yield events
    finally:
        set_event_queue(original_queue)


def _list_event(events):
    matches = [e for e in events if e.event_type == "mcp:tools/list"]
    return matches[-1] if matches else None


@pytest.mark.asyncio
async def test_tools_list_event_is_json_serializable(captured_events):
    """The captured tools/list event.response must be fully JSON-safe."""
    server = create_community_todo_server()  # FunctionTools carry the fn callable
    track(server, "test_project", AgentCatOptions(enable_tracing=True))

    async with create_community_test_client(server) as client:
        await client.list_tools()
        time.sleep(1.0)

    event = _list_event(captured_events)
    assert event is not None, "no tools/list event captured"
    # The exact thing the generated API client does before sending — must not raise.
    json.dumps(event.response)

    # And the payload is the clean MCP shape (from to_mcp_tool), not a raw
    # tool.model_dump() with an embedded callable ``fn``.
    tool = event.response["tools"][0]
    assert "fn" not in tool
    assert "inputSchema" in tool


@pytest.mark.asyncio
async def test_tools_list_event_survives_api_client_serialization(captured_events):
    """Reproduce the send path: the generated client's sanitizer must not raise."""
    server = create_community_todo_server()
    track(server, "test_project", AgentCatOptions(enable_tracing=True))

    async with create_community_test_client(server) as client:
        await client.list_tools()
        time.sleep(1.0)

    event = _list_event(captured_events)
    assert event is not None

    from agentcat_api.api_client import ApiClient

    # 'set' object has no attribute '__dict__' was the 1.0.1 drop; must be gone now.
    ApiClient.sanitize_for_serialization(ApiClient(), event.response)
