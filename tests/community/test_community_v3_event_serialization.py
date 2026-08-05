"""End-to-end: published events must be JSON-serializable (agentcat 1.0.1 data loss).

On 1.0.1 every ``tools/list`` event carried the FastMCP tool's ``fn`` callable
and ``tags`` set, so truncation logged ``Unable to serialize unknown type:
<class 'function'>`` and the event was then dropped by the API client (``'set'
object has no attribute '__dict__'``).

v2 publishes no ``tools/list`` event at all, so the surviving risk moved to the
one event type that remains: a ``tools/call`` response is a FastMCP
``ToolResult``, and ``PublishEventRequest.response`` is
``Optional[Dict[str, Any]]`` — a non-dict there fails pydantic construction and
silently drops the whole event. These tests drive real calls and assert the
captured events survive the send path.
"""

import json

import pytest

from agentcat import AgentCatOptions, track

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
def captured_events(monkeypatch):
    events: list = []
    from agentcat.modules import event_queue

    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


def _call_events(events):
    return [e for e in events if e.event_type == "mcp:tools/call"]


async def test_only_tools_call_events_are_published(captured_events):
    """A handshake plus a listing publishes nothing; v2 has one event type."""
    server = create_community_todo_server()
    track(server, "test_project", AgentCatOptions(enable_tracing=True))

    async with create_community_test_client(server) as client:
        listed = await client.list_tools()
        assert captured_events == [], [e.event_type for e in captured_events]
        # The listing still went through AgentCat: the handles are on the
        # schemas.
        add = next(t for t in listed if t.name == "add_todo")
        assert "session_id" in add.inputSchema["properties"]

        await client.call_tool("add_todo", {"text": "serialize me"})

    assert {e.event_type for e in captured_events} == {"mcp:tools/call"}


async def test_tools_call_event_response_is_json_serializable(captured_events):
    """The captured tools/call event.response must be a fully JSON-safe dict."""
    server = create_community_todo_server()  # FunctionTools carry the fn callable
    track(server, "test_project", AgentCatOptions(enable_tracing=True))

    async with create_community_test_client(server) as client:
        await client.call_tool("add_todo", {"text": "hi"})

    event = _call_events(captured_events)[-1]
    # A non-dict response never reaches the wire — it fails PublishEventRequest
    # construction and the event is dropped whole.
    assert isinstance(event.response, dict)
    # The exact thing the generated API client does before sending.
    json.dumps(event.response)
    assert "fn" not in event.response
    assert "Added todo" in json.dumps(event.response)


async def test_event_survives_api_client_serialization(captured_events):
    """Reproduce the send path: the generated client's sanitizer must not raise."""
    server = create_community_todo_server()
    track(server, "test_project", AgentCatOptions(enable_tracing=True))

    async with create_community_test_client(server) as client:
        await client.call_tool("add_todo", {"text": "sanitize me"})

    event = _call_events(captured_events)[-1]

    from agentcat_api.api_client import ApiClient

    # 'set' object has no attribute '__dict__' was the 1.0.1 drop; must be gone.
    ApiClient.sanitize_for_serialization(ApiClient(), event.response)
    ApiClient.sanitize_for_serialization(ApiClient(), event.parameters)


async def test_an_unserializable_result_drops_only_the_response(captured_events):
    """A result we cannot dump must not take the whole event down with it —
    nor the customer's tool call."""
    from fastmcp.server.middleware import Middleware
    from fastmcp.tools import ToolResult
    from mcp.types import TextContent

    class Unserializable(ToolResult):
        def model_dump(self, *args, **kwargs):
            raise TypeError("cannot serialize")

    class Unserializing(Middleware):
        async def on_call_tool(self, context, call_next):
            return Unserializable(content=[TextContent(type="text", text="opaque")])

    from fastmcp import FastMCP

    server = FastMCP("opaque-server")

    @server.tool(output_schema=None)
    def opaque() -> str:
        """Answered by the middleware below AgentCat."""
        return "unused"

    server.add_middleware(Unserializing())
    track(server, "test_project", AgentCatOptions(enable_tracing=True))

    async with create_community_test_client(server) as client:
        result = await client.call_tool("opaque", {})

    assert result.is_error is False
    assert "opaque" in "".join(c.text for c in result.content if hasattr(c, "text"))
    events = _call_events(captured_events)
    assert len(events) == 1
    assert events[0].response is None
    assert events[0].session_id.startswith("ses_")
