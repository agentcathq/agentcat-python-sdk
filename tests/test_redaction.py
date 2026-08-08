"""Unit tests for the redaction module."""

import pytest
from typing import Any, Dict
from unittest.mock import patch
from agentcat.modules.redaction import (
    redact_strings_in_object,
    redact_event,
    apply_event_redaction,
    PROTECTED_FIELDS,
    RESTORED_FIELDS,
)


class TestRedactStringsInObject:
    """Test suite for redact_strings_in_object function."""

    def test_simple_string_redaction(self):
        """Test basic string redaction."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        assert redact_strings_in_object("sensitive", redact_fn) == "[REDACTED]"
        assert redact_strings_in_object("", redact_fn) == "[REDACTED]"
        assert redact_strings_in_object("unicode: 你好", redact_fn) == "[REDACTED]"

    def test_none_values(self):
        """Test that None values are preserved."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        assert redact_strings_in_object(None, redact_fn) is None
        assert redact_strings_in_object({"key": None}, redact_fn) == {}
        assert redact_strings_in_object([None, "value", None], redact_fn) == [
            None,
            "[REDACTED]",
            None,
        ]

    def test_non_string_types(self):
        """Test that non-string types pass through unchanged."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        assert redact_strings_in_object(42, redact_fn) == 42
        assert redact_strings_in_object(3.14, redact_fn) == 3.14
        assert redact_strings_in_object(True, redact_fn) is True
        assert redact_strings_in_object(False, redact_fn) is False

    def test_list_redaction(self):
        """Test redaction in lists."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        # Simple list
        assert redact_strings_in_object(["a", "b", "c"], redact_fn) == [
            "[REDACTED]",
            "[REDACTED]",
            "[REDACTED]",
        ]

        # Mixed types
        assert redact_strings_in_object(["text", 123, True, None], redact_fn) == [
            "[REDACTED]",
            123,
            True,
            None,
        ]

        # Nested lists
        assert redact_strings_in_object([["inner"], "outer"], redact_fn) == [
            ["[REDACTED]"],
            "[REDACTED]",
        ]

        # Empty list
        assert redact_strings_in_object([], redact_fn) == []

    def test_dict_redaction(self):
        """Test redaction in dictionaries."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        # Simple dict
        assert redact_strings_in_object({"key": "value"}, redact_fn) == {
            "key": "[REDACTED]"
        }

        # Multiple keys
        result = redact_strings_in_object({"a": "1", "b": 2, "c": "3"}, redact_fn)
        assert result == {"a": "[REDACTED]", "b": 2, "c": "[REDACTED]"}

        # Nested dict
        result = redact_strings_in_object({"outer": {"inner": "secret"}}, redact_fn)
        assert result == {"outer": {"inner": "[REDACTED]"}}

        # Empty dict
        assert redact_strings_in_object({}, redact_fn) == {}

    def test_protected_fields(self):
        """Test that protected fields are not redacted."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        # Top-level protected fields
        obj = {
            "session_id": "12345",
            "project_id": "proj123",
            "other_field": "sensitive",
            "actor_id": "user123",
        }
        result = redact_strings_in_object(obj, redact_fn)
        assert result == {
            "session_id": "12345",  # Protected
            "project_id": "proj123",  # Protected
            "other_field": "[REDACTED]",  # Not protected
            "actor_id": "user123",  # Protected
        }

        # Nested values within protected fields should also be protected
        obj = {
            "identify_data": {
                "user_email": "test@example.com",
                "nested": {"deep": "value"},
            },
            "non_protected": {"data": "sensitive"},
        }
        result = redact_strings_in_object(obj, redact_fn)
        assert (
            result
            == {
                "identify_data": {
                    "user_email": "test@example.com",  # Protected because parent is protected
                    "nested": {"deep": "value"},  # Also protected
                },
                "non_protected": {"data": "[REDACTED]"},
            }
        )

    def test_protected_fields_only_at_top_level(self):
        """Test that protected field names at nested levels are still redacted."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        obj = {
            "data": {
                "session_id": "should_be_redacted",  # Not protected at nested level
                "other": "also_redacted",
            },
            "session_id": "protected_at_top",  # Protected at top level
        }
        result = redact_strings_in_object(obj, redact_fn)
        assert result == {
            "data": {"session_id": "[REDACTED]", "other": "[REDACTED]"},
            "session_id": "protected_at_top",
        }

    def test_complex_nested_structure(self):
        """Test redaction in complex nested structures."""

        def redact_fn(s: str) -> str:
            return "***"

        obj = {
            "users": [
                {
                    "id": "user1",
                    "name": "John Doe",
                    "settings": {"theme": "dark", "notifications": ["email", "sms"]},
                },
                {"id": "user2", "name": "Jane Smith", "settings": None},
            ],
            "server": "prod-server",  # Protected field
            "metadata": {"version": "1.0", "tags": ["production", "v1"]},
        }

        result = redact_strings_in_object(obj, redact_fn)
        assert result == {
            "users": [
                {
                    "id": "***",
                    "name": "***",
                    "settings": {"theme": "***", "notifications": ["***", "***"]},
                },
                {"id": "***", "name": "***"},
            ],
            "server": "prod-server",  # Protected, not redacted
            "metadata": {"version": "***", "tags": ["***", "***"]},
        }

    def test_path_tracking(self):
        """Test that paths are correctly tracked during recursion."""
        paths_seen = []

        def tracking_redact_fn(s: str) -> str:
            return f"[{s}]"

        # Monkey patch to track paths
        original_fn = redact_strings_in_object

        def wrapped_fn(obj, redact_fn, path="", is_protected=False):
            if isinstance(obj, str) and path:
                paths_seen.append(path)
            return original_fn(obj, redact_fn, path, is_protected)

        # This test verifies path construction logic indirectly
        obj = {"level1": {"level2": ["item0", "item1"], "level2b": "value"}}
        result = redact_strings_in_object(obj, tracking_redact_fn)

        # Verify structure is maintained
        assert result == {
            "level1": {"level2": ["[item0]", "[item1]"], "level2b": "[value]"}
        }

    def test_redaction_function_variations(self):
        """Test different types of redaction functions."""

        # Masking function
        def mask_fn(s: str) -> str:
            return "X" * len(s)

        assert redact_strings_in_object("secret", mask_fn) == "XXXXXX"

        # Hash-like function
        def hash_fn(s: str) -> str:
            return f"hash_{len(s)}"

        assert redact_strings_in_object("password", hash_fn) == "hash_8"

        # Conditional redaction
        def conditional_fn(s: str) -> str:
            return s if s.startswith("public_") else "[PRIVATE]"

        obj = {"public": "public_data", "private": "secret_data"}
        result = redact_strings_in_object(obj, conditional_fn)
        assert result == {"public": "public_data", "private": "[PRIVATE]"}

    def test_empty_collections(self):
        """Test handling of empty collections."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        assert redact_strings_in_object([], redact_fn) == []
        assert redact_strings_in_object({}, redact_fn) == {}
        # Empty collections are preserved in the output
        assert redact_strings_in_object(
            {"empty_list": [], "empty_dict": {}}, redact_fn
        ) == {"empty_list": [], "empty_dict": {}}

    def test_all_protected_fields(self):
        """Test all fields defined in PROTECTED_FIELDS."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        # Create object with all protected fields
        obj = {field: f"value_{field}" for field in PROTECTED_FIELDS}
        obj["unprotected"] = "sensitive_data"

        result = redact_strings_in_object(obj, redact_fn)

        # All protected fields should be unchanged
        for field in PROTECTED_FIELDS:
            assert result[field] == f"value_{field}"

        # Unprotected field should be redacted
        assert result["unprotected"] == "[REDACTED]"


class TestRedactEvent:
    """Test suite for redact_event function."""

    def test_event_redaction(self):
        """Test redaction of event objects."""

        def redact_fn(s: str) -> str:
            return "[REDACTED]"

        event = {
            "session_id": "sess123",  # Protected
            "project_id": "proj456",  # Protected
            "event_type": "mcp:tools/call",  # Protected
            "actor_id": "user789",  # Protected
            "resource_name": "database",  # Protected
            "data": {
                "query": "SELECT * FROM users",
                "parameters": ["param1", "param2"],
            },
            "metadata": {"timestamp": "2024-01-01T00:00:00Z", "ip": "192.168.1.1"},
        }

        result = redact_event(event, redact_fn)

        # Protected fields preserved
        assert result["session_id"] == "sess123"
        assert result["project_id"] == "proj456"
        assert result["event_type"] == "mcp:tools/call"
        assert result["actor_id"] == "user789"
        assert result["resource_name"] == "database"

        # Other fields redacted
        assert result["data"]["query"] == "[REDACTED]"
        assert result["data"]["parameters"] == ["[REDACTED]", "[REDACTED]"]
        assert result["metadata"]["timestamp"] == "[REDACTED]"
        assert result["metadata"]["ip"] == "[REDACTED]"

    def test_actor_fields_are_never_redacted(self):
        """The actor fields are protected wherever they ride.

        v1 carried them on a standalone `agentcat:identify` event; v2 stamps
        them onto every tools/call event. Either way redaction leaves them
        alone, so the dashboard can still name the actor."""

        def redact_fn(s: str) -> str:
            return "XXX"

        identify_event = {
            "event_type": "mcp:tools/call",
            "identify_actor_given_id": "user123",  # Protected
            "identify_actor_name": "John Doe",  # Protected
            "identify_data": {  # Protected
                "email": "john@example.com",
                "plan": "premium",
                "nested": {"preference": "dark_mode"},
            },
            "other_data": {"sensitive": "should_be_redacted"},
        }

        result = redact_event(identify_event, redact_fn)

        # All identify fields and their nested content should be preserved
        assert result["identify_actor_given_id"] == "user123"
        assert result["identify_actor_name"] == "John Doe"
        assert result["identify_data"]["email"] == "john@example.com"
        assert result["identify_data"]["plan"] == "premium"
        assert result["identify_data"]["nested"]["preference"] == "dark_mode"

        # Other data should be redacted
        assert result["other_data"]["sensitive"] == "XXX"

    def test_minimal_event(self):
        """Test redaction of minimal event with only required fields."""

        def redact_fn(s: str) -> str:
            return "[HIDDEN]"

        minimal_event = {
            "id": "evt123",  # Protected
            "data": "sensitive information",
        }

        result = redact_event(minimal_event, redact_fn)
        assert result["id"] == "evt123"
        assert result["data"] == "[HIDDEN]"

    def test_error_in_redaction_function(self):
        """Test behavior when redaction function throws an error."""

        def faulty_redact_fn(s: str) -> str:
            if "error" in s:
                raise ValueError("Redaction error")
            return "[REDACTED]"

        event = {"safe_field": "normal_value", "error_field": "trigger_error"}

        # The function should propagate the error
        with pytest.raises(ValueError, match="Redaction error"):
            redact_event(event, faulty_redact_fn)


class TestRedactEventOnTheRealEventModel:
    """The shape the publish path actually holds.

    `redact_strings_in_object` walks `str` / `list` / `dict` and returns
    everything else untouched — and `event_queue._process_event` hands
    `redact_event` a pydantic `UnredactedEvent`. For the whole of the v2 branch
    that meant the documented `redact_sensitive_information` hook was a no-op on
    every real event while the README advertised it as a security control. The
    dict-shaped cases above never caught it; these are the ones that would.
    """

    @staticmethod
    def _event(**overrides):
        from agentcat.types import UnredactedEvent

        fields = {
            "session_id": "ses_keepme",
            "id": "evt_keepme",
            "project_id": "proj_keepme",
            "event_type": "mcp:tools/call",
            "resource_name": "add_todo",
            "user_intent": "find the SECRET",
            "parameters": {"arguments": {"text": "SECRET body"}},
            "response": {"content": [{"type": "text", "text": "SECRET answer"}]},
            "client_name": "SECRET client",
            "client_version": "SECRET client version",
            "server_name": "SECRET server",
            "server_version": "SECRET server version",
            "identify_actor_given_id": "SECRET actor",
            "identify_data": {"email": "SECRET@example.com"},
            "tags": {"env": "SECRET tag"},
            "duration": 12,
        }
        fields.update(overrides)
        return UnredactedEvent(**fields)

    def test_the_hook_actually_runs_on_a_pydantic_event(self):
        def redact_fn(s: str) -> str:
            return s.replace("SECRET", "[REDACTED]")

        event = self._event()
        result = redact_event(event, redact_fn)

        assert result.user_intent == "find the [REDACTED]"
        assert result.parameters == {"arguments": {"text": "[REDACTED] body"}}
        assert result.response == {
            "content": [{"type": "text", "text": "[REDACTED] answer"}]
        }
        # client/server identity fields are protected (see PROTECTED_FIELDS)
        # — they must survive untouched, same as the other protected fields
        # below.
        assert result.client_name == "SECRET client"
        assert result.client_version == "SECRET client version"
        assert result.server_name == "SECRET server"
        assert result.server_version == "SECRET server version"
        # ...and the original is untouched, so a failure downstream cannot
        # publish a half-redacted object.
        assert event.parameters == {"arguments": {"text": "SECRET body"}}

    def test_protected_fields_survive_on_the_model_too(self):
        def redact_fn(s: str) -> str:
            return "XXX"

        result = redact_event(self._event(), redact_fn)
        assert result.session_id == "ses_keepme"
        assert result.id == "evt_keepme"
        assert result.project_id == "proj_keepme"
        assert result.event_type == "mcp:tools/call"
        assert result.resource_name == "add_todo"
        assert result.client_name == "SECRET client"
        assert result.client_version == "SECRET client version"
        assert result.server_name == "SECRET server"
        assert result.server_version == "SECRET server version"
        assert result.identify_actor_given_id == "SECRET actor"
        assert result.identify_data == {"email": "SECRET@example.com"}
        assert result.tags == {"env": "SECRET tag"}

    def test_the_result_is_still_a_publishable_event(self):
        """`model_copy`, not a rebuild: unredacted fields keep their values and
        non-string fields keep their types, so the queue can serialize it."""
        result = redact_event(self._event(), lambda s: "XXX")
        assert type(result) is type(self._event())
        assert result.duration == 12
        assert result.redaction_fn is None
        assert "XXX" in result.model_dump_json()

    def test_a_raising_hook_propagates_so_the_queue_drops_the_event(self):
        def boom(_s: str) -> str:
            raise RuntimeError("redaction exploded")

        with pytest.raises(RuntimeError, match="redaction exploded"):
            redact_event(self._event(), boom)

    def test_an_async_hook_is_driven_to_completion(self):
        """`RedactionFunction` permits an async hook and the publish path is a
        worker thread with no loop. Un-awaited, every redacted string would
        reach the wire as `<coroutine object ...>` — redaction that looks like
        it worked."""

        async def redact_fn(s: str) -> str:
            return s.replace("SECRET", "[REDACTED]")

        result = redact_event(self._event(), redact_fn)
        assert result.user_intent == "find the [REDACTED]"
        assert "coroutine" not in result.model_dump_json()


class TestApplyEventRedaction:
    """Test suite for apply_event_redaction — the whole-event redaction hook.

    Mirrors the TypeScript SDK's applyEventRedaction/redactEvent-option tests
    and the Go SDK's ApplyEventRedaction tests, since this hook exists to
    close a parity gap: Python previously had only the string-level
    redact_sensitive_information hook.
    """

    @staticmethod
    def _event(**overrides):
        from agentcat.types import UnredactedEvent

        fields = {
            "session_id": "ses_keepme",
            "id": "evt_keepme",
            "project_id": "proj_keepme",
            "event_type": "mcp:tools/call",
            "resource_name": "get_credentials",
            "user_intent": "raw intent",
            "parameters": {"secret": "raw-value"},
            "response": {"content": [{"type": "text", "text": "raw response"}]},
        }
        fields.update(overrides)
        return UnredactedEvent(**fields)

    def test_hook_sees_raw_unredacted_values(self):
        seen = {}

        def hook(event):
            seen["parameters"] = event.parameters
            seen["user_intent"] = event.user_intent
            return event

        apply_event_redaction(self._event(), hook)
        assert seen["parameters"] == {"secret": "raw-value"}
        assert seen["user_intent"] == "raw intent"

    def test_hook_can_modify_the_event(self):
        def hook(event):
            event.response = None
            return event

        result = apply_event_redaction(self._event(), hook)
        assert result is not None
        assert result.response is None

    def test_apply_event_redaction_leaves_the_original_event_unmutated(self):
        """The hook must be handed a copy, never the caller's own object —
        the publish worker (and any other in-flight reference) still holds
        the original, so a hook that mutates the event it receives (as in
        test_hook_can_modify_the_event above) must not leave that mutation
        visible there."""

        def hook(event):
            event.response = None
            return event

        original = self._event()
        apply_event_redaction(original, hook)

        assert original.response == {
            "content": [{"type": "text", "text": "raw response"}]
        }

    def test_hook_returning_none_drops_the_event(self):
        def drop_get_credentials(event):
            if event.resource_name == "get_credentials":
                return None
            return event

        result = apply_event_redaction(self._event(), drop_get_credentials)
        assert result is None

    def test_restored_fields_survive_forgery_attempts(self):
        """A hook cannot forge or erase what AgentCat itself assigned."""

        def forge(event):
            event.id = "forged-id"
            event.session_id = "forged-session"
            event.project_id = "forged-project"
            # A different but still-valid enum member, so the assignment
            # itself doesn't raise before restoration gets a chance to run.
            event.event_type = "agentcat:custom"
            event.timestamp = None
            return event

        original = self._event()
        result = apply_event_redaction(original, forge)

        assert result.id == original.id
        assert result.session_id == original.session_id
        assert result.project_id == original.project_id
        assert result.event_type == original.event_type
        assert result.timestamp == original.timestamp
        assert RESTORED_FIELDS == {
            "id",
            "session_id",
            "project_id",
            "event_type",
            "timestamp",
            "actor_id",
            "identify_actor_given_id",
            "identify_actor_name",
            "identify_data",
            "tags",
            "properties",
            "client_name",
            "client_version",
            "server_name",
            "server_version",
        }

    def test_restored_fields_survive_actor_and_tag_forgery(self):
        """A hook cannot reassign the Actor an event is attributed to, or
        forge the tags/properties a customer's own callbacks attached."""

        def forge(event):
            event.actor_id = "actor_forged"
            event.identify_actor_given_id = "forged-actor"
            event.identify_actor_name = "Forged Actor"
            event.identify_data = {"email": "attacker@evil.com"}
            event.tags = {"env": "forged"}
            event.properties = {"forged": True}
            return event

        original = self._event(
            actor_id="actor_real",
            identify_actor_given_id="real-actor-42",
            identify_actor_name="Real Actor",
            identify_data={"email": "real@example.com"},
            tags={"env": "prod"},
            properties={"real": True},
        )
        result = apply_event_redaction(original, forge)

        assert result.actor_id == original.actor_id
        assert result.identify_actor_given_id == original.identify_actor_given_id
        assert result.identify_actor_name == original.identify_actor_name
        assert result.identify_data == original.identify_data
        assert result.tags == original.tags
        assert result.properties == original.properties

    def test_restored_fields_survive_client_and_server_forgery(self):
        """A hook cannot reassign which MCP client/server an event came from."""

        def forge(event):
            event.client_name = "forged-client"
            event.client_version = "0.0.0-forged"
            event.server_name = "forged-server"
            event.server_version = "0.0.0-forged"
            return event

        original = self._event(
            client_name="real-client",
            client_version="1.2.3",
            server_name="real-server",
            server_version="4.5.6",
        )
        result = apply_event_redaction(original, forge)

        assert result.client_name == original.client_name
        assert result.client_version == original.client_version
        assert result.server_name == original.server_name
        assert result.server_version == original.server_version

    def test_restored_dict_fields_are_copied_not_aliased(self):
        """The restored tags/properties/identify_data must be independent
        copies — mutating the result must not corrupt the original event a
        publish worker or another in-flight event may still hold."""

        original = self._event(
            tags={"env": "prod"},
            properties={"plan": "premium"},
            identify_data={"email": "real@example.com"},
        )
        result = apply_event_redaction(original, lambda event: event)

        assert result.tags is not original.tags
        assert result.properties is not original.properties
        assert result.identify_data is not original.identify_data

        result.tags["env"] = "MUTATED"
        result.properties["plan"] = "MUTATED"
        result.identify_data["email"] = "MUTATED"

        assert original.tags == {"env": "prod"}
        assert original.properties == {"plan": "premium"}
        assert original.identify_data == {"email": "real@example.com"}

    def test_a_raising_hook_propagates_so_the_queue_drops_the_event(self):
        def boom(_event):
            raise RuntimeError("event redaction exploded")

        with pytest.raises(RuntimeError, match="event redaction exploded"):
            apply_event_redaction(self._event(), boom)

    def test_hook_returning_a_non_event_value_raises(self):
        """Documented fail-loud contract (see apply_event_redaction's and
        RedactEventFunction's docstrings): the pydantic-Event return type is
        enforced by type hints only, not at runtime, so a hook that ignores
        its type hint and returns a plain dict must raise rather than
        silently degrade to some fallback behavior."""

        def hook(event):
            return {**event.model_dump(), "response": None}

        with pytest.raises(AttributeError):
            apply_event_redaction(self._event(), hook)

    def test_hook_returning_a_fresh_partial_event_nulls_unset_fields(self):
        """Documented footgun (see apply_event_redaction's docstring): the
        hook's return value replaces the event's fields wholesale — a full
        model_dump(), not a diff against the original — so a hook that
        returns a freshly-built Event with only some fields set, instead of
        mutating and returning the one it was handed, silently nulls out
        everything it didn't set."""
        from agentcat.types import Event as EventModel

        def hook(event):
            return EventModel(
                event_type=event.event_type, resource_name=event.resource_name
            )

        original = self._event()
        result = apply_event_redaction(original, hook)

        assert result.resource_name == original.resource_name
        assert result.user_intent is None
        assert result.parameters is None
        assert result.response is None
        # ...while the original the hook was handed a dump of is untouched.
        assert original.user_intent == "raw intent"

    def test_an_async_hook_is_driven_to_completion(self):
        async def hook(event):
            event.user_intent = "async-modified"
            return event

        result = apply_event_redaction(self._event(), hook)
        assert result.user_intent == "async-modified"

    def test_an_async_hook_resolving_to_none_drops_the_event(self):
        """The None-drop check must run against the AWAITED result, not the
        coroutine object drive_hook_result is handed — a coroutine is always
        truthy, so a check performed before awaiting would never drop."""

        async def drop_it(event):
            return None

        result = apply_event_redaction(self._event(), drop_it)
        assert result is None

    def test_hook_never_sees_the_function_fields(self):
        """`Event` never declares redaction_fn/event_redaction_fn at all, so
        `hasattr` on the hook's input is True regardless of whether
        apply_event_redaction actually excludes them from the dump — it
        would read False even if the exclusion were deleted. Patch in
        `UnredactedEvent` (which DOES declare those fields) as the hook's
        input type instead, so a real callable surviving the dump shows up
        as a non-None attribute rather than a silently-absent one."""
        import agentcat.types as types_module

        seen = {}

        def hook(event):
            seen["redaction_fn"] = getattr(event, "redaction_fn", "MISSING")
            seen["event_redaction_fn"] = getattr(
                event, "event_redaction_fn", "MISSING"
            )
            return event

        event = self._event(redaction_fn=lambda s: s, event_redaction_fn=hook)

        with patch.object(types_module, "Event", types_module.UnredactedEvent):
            apply_event_redaction(event, hook)

        assert seen["redaction_fn"] is None
        assert seen["event_redaction_fn"] is None

    def test_result_preserves_redaction_fn_for_the_string_hook_to_run_next(self):
        def string_redact_fn(s):
            return "[REDACTED]"

        event = self._event(redaction_fn=string_redact_fn)
        result = apply_event_redaction(event, lambda e: e)
        assert result.redaction_fn is string_redact_fn
