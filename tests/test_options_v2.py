from agentcat.types import AgentCatOptions, CustomEventData

from .test_utils import sid


def test_v2_option_defaults():
    o = AgentCatOptions()
    assert o.enable_agent_tracking is False
    assert o.resolve_session_id is None


def test_custom_event_data_keys():
    d: CustomEventData = {"session_id": sid("x"), "is_error": False, "tags": {"a": "b"}}
    assert d["session_id"] == sid("x")
