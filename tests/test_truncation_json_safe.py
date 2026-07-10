"""Truncation must never abort (or silently drop an event) on a non-JSON-safe payload.

Regression for agentcat 1.0.1: event payloads holding a raw callable made
``truncate_event``'s ``model_dump_json()`` raise ``Unable to serialize unknown
type: <class 'function'>``, and payloads holding a ``set`` made the downstream
API client drop the event with ``'set' object has no attribute '__dict__'``.
The pipeline must coerce such values to JSON-safe primitives instead.
"""

import json
from unittest.mock import patch

from agentcat.modules import truncation
from agentcat.modules.truncation import truncate_event
from agentcat.types import UnredactedEvent


def _poison_response():
    # Mirrors what tool.model_dump() embeds for FastMCP v3 tools: a callable and a set.
    return {"tools": [{"name": "t", "fn": (lambda: 1), "tags": {"a", "b"}}]}


def test_truncate_event_does_not_log_failure_on_callable_and_set():
    event = UnredactedEvent(event_type="mcp:tools/list", response=_poison_response())
    with patch.object(truncation, "write_to_log") as mock_log:
        result = truncate_event(event)
    failures = [
        c.args[0]
        for c in mock_log.call_args_list
        if c.args and "Truncation failed" in str(c.args[0])
    ]
    assert failures == [], f"truncation aborted: {failures}"
    assert result is not None


def test_truncated_event_is_json_serializable():
    """After truncation the event's payload must contain only JSON-safe primitives."""
    event = UnredactedEvent(event_type="mcp:tools/list", response=_poison_response())
    result = truncate_event(event)
    # The event the API client re-serializes on send must not carry a callable/set.
    json.dumps(result.response)  # would raise if a set/function survived
    result.model_dump_json()     # would raise if pydantic still can't serialize it


def test_make_json_safe_neutralizes_all_types():
    import datetime

    safe = truncation.make_json_safe(
        {"fn": (lambda: 1), "tags": {"x"}, "when": datetime.datetime(2020, 1, 1)}
    )
    text = json.dumps(safe)  # must not raise
    assert "tags" in safe and isinstance(safe["tags"], list)
    assert isinstance(safe["fn"], str)
    assert safe["when"].startswith("2020-01-01")
