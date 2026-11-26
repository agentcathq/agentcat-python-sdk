"""Tests for the sensitive key mask.

`sensitive_keys_vectors.json` is shared by every AgentCat SDK; do not edit it here.
"""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel
from starlette.requests import Request

from agentcat.modules import event_queue as event_queue_module
from agentcat.modules.event_queue import EventQueue
from agentcat.modules.request_extra import params_with_extra
from agentcat.modules.sensitive_keys import is_sensitive_key, mask_sensitive_keys
from agentcat.types import UnredactedEvent

VECTORS = json.loads(
    (Path(__file__).parent / "sensitive_keys_vectors.json").read_text()
)


@pytest.mark.parametrize("case", VECTORS["keys"], ids=lambda case: case["key"])
def test_key_vectors(case):
    assert is_sensitive_key(case["key"]) is case["sensitive"]


@pytest.mark.parametrize("case", VECTORS["trees"], ids=lambda case: case["name"])
def test_tree_vectors(case):
    before = copy.deepcopy(case["input"])
    assert mask_sensitive_keys(case["input"]) == case["expected"]
    assert case["input"] == before


class Account(BaseModel):
    user: str
    api_key: str


def _sent_event(event):
    sender = MagicMock()
    telemetry = MagicMock()
    with patch.object(event_queue_module, "_telemetry_manager", telemetry):
        EventQueue(api_client=sender)._process_event(event)
    exported = telemetry.export.call_args[0][0]
    sent = sender.publish_event.call_args.kwargs["publish_event_request"]
    assert exported is sent
    return sent


def test_pipeline_masks_request_headers_and_typed_values():
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"authorization", b"Bearer abc.def"),
                (b"cookie", b"sid=1"),
                (b"x-api-key", b"k-123"),
                (b"user-agent", b"claude-code/2.1.0"),
                (b"x-anthropic-client", b"claude-code"),
                (b"mcp-session-id", b"0b1c"),
            ],
        }
    )
    event = UnredactedEvent(
        event_type="mcp:tools/call",
        project_id="project-123",
        timestamp=datetime.now(timezone.utc),
        parameters=params_with_extra(
            {"name": "login", "arguments": {"query": "cats"}},
            SimpleNamespace(request=request, request_id=7, meta=None, session=None),
        ),
        response={"structuredContent": Account(user="ann", api_key="k-456")},
    )

    sent = _sent_event(event)

    headers = sent.parameters["extra"]["requestInfo"]["headers"]
    assert headers == {
        "authorization": "[redacted]",
        "cookie": "[redacted]",
        "x-api-key": "[redacted]",
        "user-agent": "claude-code/2.1.0",
        "x-anthropic-client": "claude-code",
        "mcp-session-id": "0b1c",
    }
    assert sent.parameters["arguments"] == {"query": "cats"}
    assert sent.response == {
        "structuredContent": {"user": "ann", "api_key": "[redacted]"}
    }
    # The objects the customer's handler holds keep their values.
    assert request.headers["authorization"] == "Bearer abc.def"
    assert request.headers["x-api-key"] == "k-123"
    assert event.parameters["extra"]["requestInfo"]["headers"]["cookie"] == "sid=1"
    assert event.response["structuredContent"].api_key == "k-456"


def test_pipeline_drops_a_field_that_cannot_be_masked():
    looped: dict = {"authorization": "Bearer abc"}
    looped["self"] = looped
    event = UnredactedEvent(
        event_type="mcp:tools/call",
        project_id="project-123",
        parameters={"loop": looped},
        response={"token": "t-1", "text": "ok"},
    )

    with patch.object(event_queue_module, "write_to_log") as log:
        sent = _sent_event(event)

    assert sent.parameters is None
    assert sent.response == {"token": "[redacted]", "text": "ok"}
    assert any("Masking failed for parameters" in c.args[0] for c in log.call_args_list)
