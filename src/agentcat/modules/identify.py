from typing import Any

from agentcat.modules.logging import write_to_log
from agentcat.types import AgentCatData, UserIdentity


def resolve_identity(
    data: AgentCatData | None, request: Any, extra: Any
) -> UserIdentity | None:
    """Run the configured identify hook and return its UserIdentity.

    v1 published a standalone `agentcat:identify` event and cached the result
    for the connection's lifetime. v2 publishes one event per tool call and
    stamps the actor onto it, so identity resolution is pure and runs on every
    call. Never raises — a customer hook that blows up yields an anonymous
    call, not a failed one.
    """
    if not data or not data.options or not data.options.identify:
        return None

    try:
        result = data.options.identify(request, extra)
    except Exception as e:
        write_to_log(f"Error occurred during user identification: {e}")
        return None

    if not result or not isinstance(result, UserIdentity):
        write_to_log(
            "User identification function did not return a valid UserIdentity "
            f"instance. Received type: {type(result).__name__}"
        )
        return None
    return result
