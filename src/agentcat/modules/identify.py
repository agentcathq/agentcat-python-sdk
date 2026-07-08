import inspect
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from agentcat.modules import event_queue
from agentcat.modules.internal import get_server_tracking_data
from agentcat.modules.logging import safe_error_string, write_to_log
from agentcat.types import EventType, UnredactedEvent, UserIdentity

# Maximum number of session identities retained in the global cache.
IDENTITY_CACHE_MAX_SIZE = 1000


class IdentityCache:
    """Simple LRU cache for session identities.

    Prevents memory leaks by capping at max_size entries. This cache persists
    across server instance restarts (mirrors the TypeScript SDK's
    IdentityCache in modules/internal.ts).
    """

    def __init__(self, max_size: int = IDENTITY_CACHE_MAX_SIZE):
        self._cache: OrderedDict[str, UserIdentity] = OrderedDict()
        self._max_size = max_size

    def get(self, session_id: str) -> Optional[UserIdentity]:
        identity = self._cache.get(session_id)
        if identity is not None:
            # Move to end (most recently used)
            self._cache.move_to_end(session_id)
        return identity

    def set(self, session_id: str, identity: UserIdentity) -> None:
        if session_id in self._cache:
            # Remove so re-adding places it at the end
            del self._cache[session_id]
        elif len(self._cache) >= self._max_size:
            # Evict least recently used
            self._cache.popitem(last=False)
        self._cache[session_id] = identity

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._cache

    def __len__(self) -> int:
        return len(self._cache)


# Global identity cache shared across all server instances.
# This prevents duplicate identify events when server objects are recreated.
_global_identity_cache = IdentityCache()


def reset_identity_cache() -> None:
    """Reset the global identity cache (mainly for testing)."""
    global _global_identity_cache
    _global_identity_cache = IdentityCache()


def are_identities_equal(a: UserIdentity, b: UserIdentity) -> bool:
    """Deep comparison of two UserIdentity objects."""
    if a.user_id != b.user_id:
        return False
    if a.user_name != b.user_name:
        return False
    return (a.user_data or {}) == (b.user_data or {})


def merge_identities(
    previous: UserIdentity | None, next_identity: UserIdentity
) -> UserIdentity:
    """Merge two UserIdentity objects.

    Overwrites user_id and user_name with the newest values, but merges
    user_data keys across calls (newest values win on key collisions).
    """
    if previous is None:
        return next_identity

    return UserIdentity(
        user_id=next_identity.user_id,
        user_name=next_identity.user_name,
        user_data={
            **(previous.user_data or {}),
            **(next_identity.user_data or {}),
        },
    )


async def identify_session(server, request: any, context: any) -> UserIdentity | None:
    """Run the configured identify hook for a request.

    The identify hook may be a sync or an async callable; coroutine results
    are awaited. The resolved identity is merged with the session's previous
    identity (user_id/user_name overwritten, user_data keys merged) and an
    `agentcat:identify` event is published ONLY when the merged identity
    differs from the cached one — repeated identical identities do not
    republish.

    Returns the merged UserIdentity, or None if no hook is configured, the
    hook raises, or it returns a non-UserIdentity value.
    """
    data = get_server_tracking_data(server)

    if not data or not data.options or not data.options.identify:
        return None

    try:
        identify_result = data.options.identify(request, context)
        if inspect.iscoroutine(identify_result):
            identify_result = await identify_result
        if not identify_result or not isinstance(identify_result, UserIdentity):
            write_to_log(
                "User identification function did not return a valid UserIdentity "
                f"instance. Received type: {type(identify_result).__name__}"
            )
            return None

        session_id = data.session_id

        # Check the global cache (works across server instance restarts)
        previous_identity = _global_identity_cache.get(session_id)

        # Merge identities (overwrite user_id/user_name, merge user_data)
        merged_identity = merge_identities(previous_identity, identify_result)

        # Only publish if the identity has changed
        has_changed = previous_identity is None or not are_identities_equal(
            previous_identity, merged_identity
        )

        _global_identity_cache.set(session_id, merged_identity)

        if has_changed:
            write_to_log(
                f"Identified session {session_id} (actor: {merged_identity.user_id})"
            )
            event = UnredactedEvent(
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                event_type=EventType.AGENTCAT_IDENTIFY.value,
                identify_actor_given_id=merged_identity.user_id,
                identify_actor_name=merged_identity.user_name,
                identify_data=merged_identity.user_data or {},
            )
            event_queue.publish_event(server, event)

        return merged_identity
    except Exception as e:
        # safe_error_string: the exception comes from customer code and may
        # have a broken __str__ — the log line itself must never raise, since
        # several call sites run unguarded on the customer's request path.
        write_to_log(
            f"Error occurred during user identification: {safe_error_string(e)}"
        )
        return None
