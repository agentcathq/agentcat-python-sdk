"""Token estimates ride on every tools/call event, computed on the raw
payloads before the queue's redaction and truncation stages run.

Vectors: {"text":"hi there"} is 19 bytes -> 6 tokens; the flavors' `echo`
tool answers "echo:hi there", 13 bytes -> 4. {"text":"secret-value-123456789"}
is 33 bytes -> 10, and the echo of it, "echo:secret-value-123456789", is 27
bytes -> 8.
"""

import pytest

from agentcat import AgentCatOptions, track
from agentcat.modules import event_queue

from .test_utils.flavors import flavors


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    """Collect every event the queue is handed, without touching the network."""
    events: list = []
    monkeypatch.setattr(event_queue.event_queue, "add", events.append)
    return events


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_tool_call_events_carry_token_estimates(flavor, capture):
    built = flavor.build("token-estimates")
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        await flavor.call(client, "echo", {"text": "hi there"})

    event = capture[0]
    assert event.input_tokens == 6
    # Only the content text counts: the structured mirror of the same answer
    # and the era-specific error/structured keys add nothing.
    assert event.output_tokens == 4


def _build_structured_only_server(flavor_id: str):
    """A fresh, untracked server whose lone tool answers with an empty
    ``content`` list and a ``structuredContent``/``structured_content`` of
    ``{"result": "ok"}`` — no auto-mirrored text block.

    Each era's facade normally derives a text content block from a typed
    return value, so this bypasses that conversion the way the era itself
    allows: ``MCPServer`` and the community ``fastmcp`` both pass a
    already-built result object straight through their `convert_result`
    (`mcp.server.mcpserver.utilities.func_metadata.FuncMetadata.convert_result`,
    the community `Tool.convert_result`) instead of re-deriving content
    from it, and the lowlevel `Server`'s `on_call_tool` callback is returned
    to the wire completely unmodified.
    """
    if flavor_id == "mcpserver-v2":
        from mcp.server.mcpserver import MCPServer
        from mcp.types import CallToolResult

        server = MCPServer("structured-only")

        @server.tool()
        async def structured_only() -> CallToolResult:
            return CallToolResult(content=[], structured_content={"result": "ok"})

        return server

    if flavor_id == "lowlevel-v2":
        from mcp import types
        from mcp.server import Server

        async def on_list_tools(ctx, params):
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="structured_only",
                        description="",
                        input_schema={"type": "object", "properties": {}},
                    )
                ]
            )

        async def on_call_tool(ctx, params):
            return types.CallToolResult(content=[], structured_content={"result": "ok"})

        return Server(
            "structured-only", on_list_tools=on_list_tools, on_call_tool=on_call_tool
        )

    if flavor_id.startswith("community-"):
        from fastmcp import FastMCP

        # `fastmcp.tools` re-exports ToolResult in every supported release;
        # the module behind it moved (`fastmcp.tools.tool` through 3.1.x,
        # `fastmcp.tools.base` from 3.2), so import from the package.
        from fastmcp.tools import ToolResult

        server = FastMCP("structured-only")

        @server.tool
        async def structured_only() -> ToolResult:
            return ToolResult(content=[], structured_content={"result": "ok"})

        return server

    return None


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_structured_only_result_counts_the_structured_content(flavor, capture):
    """When a result's `content` list is empty and it carries a structured
    value, `output_tokens` counts that value's compact JSON instead of 0.

    Not every flavor's facade can be made to answer with an empty `content`
    list without going around its typed-return conversion; flavors that
    cannot are skipped here rather than faked, per the design brief.
    """
    server = _build_structured_only_server(flavor.id)
    if server is None:
        pytest.skip(
            f"{flavor.id}: no known way to make this flavor answer with an "
            "empty content list and a structured value"
        )
    track(server, "proj_test", AgentCatOptions())

    async with flavor.client(server) as client:
        await flavor.call(client, "structured_only", {})

    event = capture[0]
    # Confirms the fixture itself, not just the estimate: the event records
    # the customer's undecorated result, an empty content list with the
    # structured value intact — the wire response the client actually sees
    # also carries the SDK's session mint-back text, which must not count.
    assert event.response["content"] == []
    structured = event.response.get("structured_content") or event.response.get(
        "structuredContent"
    )
    assert structured == {"result": "ok"}
    assert event.output_tokens == 5  # {"result":"ok"} = 15 bytes


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_counts_survive_redaction_and_truncation(flavor, capture, monkeypatch):
    sent: list = []
    monkeypatch.setattr(event_queue.event_queue, "_send_event", sent.append)

    built = flavor.build("token-estimates-redacted")
    track(
        built.server,
        "proj_test",
        # Only the secret string is rewritten: a hook that replaced every
        # string would also clobber the content block's `type` discriminator.
        AgentCatOptions(
            redact_sensitive_information=lambda text: (
                "[REDACTED]" if "secret" in text else text
            )
        ),
    )

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        await flavor.call(client, "echo", {"text": "secret-value-123456789"})

    queued = capture[0]
    assert queued.redaction_fn is not None
    # Drive the real pipeline (redact -> sanitize -> truncate -> send) on the
    # captured event, exactly as the worker thread would.
    event_queue.event_queue._process_event(queued)

    assert len(sent) == 1
    published = sent[0]
    assert published.parameters["arguments"]["text"] == "[REDACTED]"
    assert published.response["content"][0]["text"] == "[REDACTED]"
    assert published.input_tokens == 10
    assert published.output_tokens == 8  # "echo:secret-value-123456789" = 27 bytes


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_counts_survive_truncation(flavor, capture, monkeypatch):
    """{"text":"xxx...x"} (60000 x's) is 60011 bytes -> ceil(60011/3.5) = 17146
    exactly. The echo of it, "echo:" + 60000 x's, is 60005 bytes ->
    ceil(60005/3.5) = 17145. The brief's original 40000-x vector (11432 /
    11430) does not reliably push every flavor's serialized event past
    truncation.MAX_EVENT_BYTES (100KB) — some flavors mirror the answer into
    `structuredContent` too and some do not, so only the larger payload
    guarantees size-targeted truncation fires on every flavor. The published
    response text ends up well below the original 60005 characters, and the
    counts still describe the original, untruncated bytes.
    """
    sent: list = []
    monkeypatch.setattr(event_queue.event_queue, "_send_event", sent.append)

    built = flavor.build("token-estimates-truncated")
    track(built.server, "proj_test", AgentCatOptions())

    long_text = "x" * 60000
    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        await flavor.call(client, "echo", {"text": long_text})

    queued = capture[0]
    # Drive the real pipeline (sanitize -> truncate -> send) on the captured
    # event, exactly as the worker thread would.
    event_queue.event_queue._process_event(queued)

    assert len(sent) == 1
    published = sent[0]
    published_text = published.response["content"][0]["text"]
    assert len(published_text) < 60005
    assert published.input_tokens == 17146
    assert published.output_tokens == 17145


@pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
async def test_a_broken_estimator_never_breaks_the_tool_call(flavor, capture, monkeypatch):
    """The estimator runs inside the customer's request. Force it to raise
    and the client must still receive the tool's own answer; the call's
    analytics may be lost, the response never is."""

    def explode(_value):
        raise RuntimeError("estimator bug")

    monkeypatch.setattr("agentcat.modules.callpath.estimate_input_tokens", explode)
    monkeypatch.setattr("agentcat.modules.callpath.estimate_output_tokens", explode)

    built = flavor.build("token-estimates-broken")
    track(built.server, "proj_test", AgentCatOptions())

    async with flavor.client(built.server) as client:
        await flavor.list_tools(client)
        called = await flavor.call(client, "echo", {"text": "hi there"})

    assert not called.is_error
    assert "echo:hi there" in called.text
