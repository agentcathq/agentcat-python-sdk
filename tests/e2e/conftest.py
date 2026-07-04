"""Shared pytest fixtures for the e2e Streamable-HTTP suite.

`capture_queue` mocks the global event queue for the duration of a test and
yields the list that accumulates published events. Restores the real queue on
teardown.
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import MagicMock

import pytest

from agentcat.modules.event_queue import EventQueue, set_event_queue


@pytest.fixture
def capture_queue() -> List[Any]:
    """Replace the global EventQueue with a mock that records every publish.

    Yields the list of captured PublishEventRequest objects. Tests assert on
    its contents after the in-flight HTTP round-trip plus a short settle.
    """
    from agentcat.modules.event_queue import event_queue as original

    captured: List[Any] = []
    mock = MagicMock()

    def capture_event(publish_event_request):
        captured.append(publish_event_request)

    mock.publish_event = MagicMock(side_effect=capture_event)
    set_event_queue(EventQueue(api_client=mock))
    yield captured
    set_event_queue(original)
