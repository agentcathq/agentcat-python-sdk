"""Tests for identify: async hooks, change detection, and identity merging.

Mirrors the TypeScript SDK's handleIdentify (src/modules/internal.ts): the
identify hook runs per-request but an `agentcat:identify` event is published
only when the (merged) identity actually changed for the session.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from agentcat.modules.identify import (
    IdentityCache,
    are_identities_equal,
    identify_session,
    merge_identities,
    reset_identity_cache,
)
from agentcat.modules.internal import (
    reset_all_tracking_data,
    set_server_tracking_data,
)
from agentcat.types import AgentCatData, AgentCatOptions, SessionInfo, UserIdentity

from .test_utils.todo_server import create_todo_server


def _identify_events(mock_event_queue):
    """Extract events passed to publish_event from the mocked event_queue."""
    return [
        call.args[1]
        for call in mock_event_queue.publish_event.call_args_list
        if call.args[1].event_type == "agentcat:identify"
    ]


class TestIdentifySession:
    """Change-detection and merge behavior of identify_session."""

    def setup_method(self):
        reset_all_tracking_data()
        reset_identity_cache()
        self.server = create_todo_server()

    def teardown_method(self):
        reset_all_tracking_data()

    def _setup_data(self, identify, session_id="ses_identify_test"):
        options = AgentCatOptions(identify=identify)
        data = AgentCatData(
            project_id="test_project",
            session_id=session_id,
            session_info=SessionInfo(),
            last_activity=datetime.now(timezone.utc),
            options=options,
        )
        set_server_tracking_data(self.server, data)
        return data

    @patch("agentcat.modules.identify.event_queue")
    async def test_identical_identity_publishes_single_event(self, mock_event_queue):
        """Repeated identical identities → identify hook runs each time but
        only ONE agentcat:identify event is published."""
        identify_fn = MagicMock(
            side_effect=lambda req, ctx: UserIdentity(
                user_id="alice", user_name="Alice", user_data={"plan": "pro"}
            )
        )
        self._setup_data(identify_fn)

        for _ in range(3):
            result = await identify_session(self.server, MagicMock(), MagicMock())
            assert result is not None
            assert result.user_id == "alice"

        assert identify_fn.call_count == 3
        assert len(_identify_events(mock_event_queue)) == 1

    @patch("agentcat.modules.identify.event_queue")
    async def test_changed_identity_publishes_new_event(self, mock_event_queue):
        """A changed identity → a new agentcat:identify event."""
        identities = iter(
            [
                UserIdentity(user_id="alice", user_name="Alice", user_data=None),
                UserIdentity(user_id="alice", user_name="Alice", user_data=None),
                UserIdentity(user_id="bob", user_name="Bob", user_data=None),
            ]
        )
        self._setup_data(lambda req, ctx: next(identities))

        await identify_session(self.server, MagicMock(), MagicMock())
        await identify_session(self.server, MagicMock(), MagicMock())
        result = await identify_session(self.server, MagicMock(), MagicMock())

        events = _identify_events(mock_event_queue)
        assert len(events) == 2
        assert events[0].identify_actor_given_id == "alice"
        assert events[1].identify_actor_given_id == "bob"
        assert result.user_id == "bob"

    @patch("agentcat.modules.identify.event_queue")
    async def test_user_data_merges_across_calls(self, mock_event_queue):
        """user_id/user_name are overwritten; user_data keys are merged with
        the newest values winning on collision."""
        identities = iter(
            [
                UserIdentity(
                    user_id="alice",
                    user_name="Alice",
                    user_data={"plan": "free", "region": "us"},
                ),
                UserIdentity(
                    user_id="alice",
                    user_name="Alice A.",
                    user_data={"plan": "pro"},
                ),
            ]
        )
        self._setup_data(lambda req, ctx: next(identities))

        await identify_session(self.server, MagicMock(), MagicMock())
        result = await identify_session(self.server, MagicMock(), MagicMock())

        assert result.user_id == "alice"
        assert result.user_name == "Alice A."
        assert result.user_data == {"plan": "pro", "region": "us"}

        events = _identify_events(mock_event_queue)
        assert len(events) == 2
        assert events[1].identify_actor_name == "Alice A."
        assert events[1].identify_data == {"plan": "pro", "region": "us"}

    @patch("agentcat.modules.identify.event_queue")
    async def test_merged_but_unchanged_identity_does_not_republish(
        self, mock_event_queue
    ):
        """A subset of previously-seen user_data merges into an identical
        identity → no new event."""
        identities = iter(
            [
                UserIdentity(
                    user_id="alice", user_name="Alice", user_data={"a": "1", "b": "2"}
                ),
                UserIdentity(user_id="alice", user_name="Alice", user_data={"a": "1"}),
            ]
        )
        self._setup_data(lambda req, ctx: next(identities))

        await identify_session(self.server, MagicMock(), MagicMock())
        result = await identify_session(self.server, MagicMock(), MagicMock())

        assert result.user_data == {"a": "1", "b": "2"}
        assert len(_identify_events(mock_event_queue)) == 1

    @patch("agentcat.modules.identify.event_queue")
    async def test_async_identify_callback(self, mock_event_queue):
        """AgentCatOptions.identify may be an async callable (P4 parity with
        TypeScript's async identify)."""
        calls = []

        async def identify(request, context):
            calls.append(request)
            return UserIdentity(
                user_id="async-user", user_name="Async User", user_data={"k": "v"}
            )

        self._setup_data(identify)

        result = await identify_session(self.server, MagicMock(), MagicMock())

        assert calls, "async identify hook never awaited"
        assert isinstance(result, UserIdentity)
        assert result.user_id == "async-user"

        events = _identify_events(mock_event_queue)
        assert len(events) == 1
        assert events[0].identify_actor_given_id == "async-user"
        assert events[0].identify_data == {"k": "v"}

    @patch("agentcat.modules.identify.event_queue")
    async def test_async_identify_exception_returns_none(self, mock_event_queue):
        async def identify(request, context):
            raise RuntimeError("async identify exploded")

        self._setup_data(identify)

        result = await identify_session(self.server, MagicMock(), MagicMock())

        assert result is None
        assert not _identify_events(mock_event_queue)

    @patch("agentcat.modules.identify.event_queue")
    async def test_new_session_republishes_identity(self, mock_event_queue):
        """Identity cache is keyed per session — a new session ID publishes
        again even for an identical identity."""
        identify_fn = lambda req, ctx: UserIdentity(  # noqa: E731
            user_id="alice", user_name="Alice", user_data=None
        )
        data = self._setup_data(identify_fn, session_id="ses_first")
        await identify_session(self.server, MagicMock(), MagicMock())

        data.session_id = "ses_second"
        set_server_tracking_data(self.server, data)
        await identify_session(self.server, MagicMock(), MagicMock())

        assert len(_identify_events(mock_event_queue)) == 2


class TestIdentityHelpers:
    """Unit tests for merge/equality helpers and the LRU cache."""

    def test_are_identities_equal(self):
        a = UserIdentity(user_id="u", user_name="n", user_data={"k": "v"})
        b = UserIdentity(user_id="u", user_name="n", user_data={"k": "v"})
        assert are_identities_equal(a, b)

        assert not are_identities_equal(
            a, UserIdentity(user_id="x", user_name="n", user_data={"k": "v"})
        )
        assert not are_identities_equal(
            a, UserIdentity(user_id="u", user_name="x", user_data={"k": "v"})
        )
        assert not are_identities_equal(
            a, UserIdentity(user_id="u", user_name="n", user_data={"k": "other"})
        )
        # None and {} user_data are equivalent
        assert are_identities_equal(
            UserIdentity(user_id="u", user_name="n", user_data=None),
            UserIdentity(user_id="u", user_name="n", user_data={}),
        )

    def test_merge_identities_no_previous(self):
        nxt = UserIdentity(user_id="u", user_name="n", user_data={"k": "v"})
        assert merge_identities(None, nxt) is nxt

    def test_merge_identities_overwrites_and_merges(self):
        prev = UserIdentity(
            user_id="old", user_name="Old", user_data={"a": "1", "b": "2"}
        )
        nxt = UserIdentity(user_id="new", user_name="New", user_data={"b": "3"})
        merged = merge_identities(prev, nxt)
        assert merged.user_id == "new"
        assert merged.user_name == "New"
        assert merged.user_data == {"a": "1", "b": "3"}

    def test_identity_cache_lru_eviction(self):
        cache = IdentityCache(max_size=2)
        alice = UserIdentity(user_id="alice", user_name=None, user_data=None)
        bob = UserIdentity(user_id="bob", user_name=None, user_data=None)
        carol = UserIdentity(user_id="carol", user_name=None, user_data=None)

        cache.set("s1", alice)
        cache.set("s2", bob)
        # Touch s1 so s2 becomes least recently used
        assert cache.get("s1") is alice
        cache.set("s3", carol)

        assert len(cache) == 2
        assert cache.get("s2") is None
        assert cache.get("s1") is alice
        assert cache.get("s3") is carol

    def test_identity_cache_update_existing(self):
        cache = IdentityCache(max_size=2)
        v1 = UserIdentity(user_id="v1", user_name=None, user_data=None)
        v2 = UserIdentity(user_id="v2", user_name=None, user_data=None)
        cache.set("s1", v1)
        cache.set("s1", v2)
        assert len(cache) == 1
        assert cache.get("s1") is v2
