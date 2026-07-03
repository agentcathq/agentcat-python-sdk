from datetime import datetime, timezone

from agentcat.modules import event_queue
from agentcat.modules.internal import get_server_tracking_data
from agentcat.modules.logging import write_to_log
from agentcat.types import EventType, UnredactedEvent, UserIdentity


def identify_session(server, request: any, context: any) -> UserIdentity | None:
    """Run the configured identify hook and publish an `agentcat:identify` event.

    Returns the resulting UserIdentity, or None if no hook is configured, the
    hook raises, or it returns a non-UserIdentity value.
    """
    data = get_server_tracking_data(server)

    if not data or not data.options or not data.options.identify:
        return None

    try:
        identify_result = data.options.identify(request, context)
        if not identify_result or not isinstance(identify_result, UserIdentity):
            write_to_log(
                "User identification function did not return a valid UserIdentity "
                f"instance. Received type: {type(identify_result).__name__}"
            )
            return None

        event = UnredactedEvent(
            session_id=data.session_id,
            timestamp=datetime.now(timezone.utc),
            event_type=EventType.AGENTCAT_IDENTIFY.value,
            identify_actor_given_id=identify_result.user_id,
            identify_actor_name=identify_result.user_name,
            identify_data=identify_result.user_data or {},
        )
        event_queue.publish_event(server, event)

        return identify_result
    except Exception as e:
        write_to_log(f"Error occurred during user identification: {e}")
        return None
