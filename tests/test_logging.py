"""Tests for the logging module."""

import time
import uuid
from unittest.mock import patch

import pytest

from agentcat.modules.logging import write_to_log, set_debug_mode


class TestLogging:
    """Test the logging functionality."""

    @pytest.fixture(autouse=True)
    def cleanup_log_file(self):
        """Reset debug mode after each test.

        Every test patches agentcat.modules.logging.os.path.expanduser to a
        tmp_path location, so no cleanup of the real ~/agentcat.log is needed.
        """
        yield

        # Reset debug mode so later teardown (e.g. event-queue shutdown)
        # doesn't write a stray line to the real ~/agentcat.log
        set_debug_mode(False)

    def test_write_to_log_uses_agentcat_log_path(self, tmp_path):
        """Test that write_to_log resolves ~/agentcat.log (no ~/mcpcat.log fallback)."""
        # Enable debug mode
        set_debug_mode(True)

        # Use a unique file name for this test
        unique_id = str(uuid.uuid4())
        log_file = tmp_path / f"test_agentcat_{unique_id}.log"

        # Mock os.path.expanduser to use our temp file
        with patch(
            "agentcat.modules.logging.os.path.expanduser", return_value=str(log_file)
        ) as mock_expanduser:
            test_message = f"Default path test {unique_id}"
            write_to_log(test_message)

        # The code must resolve the new default path
        mock_expanduser.assert_any_call("~/agentcat.log")

        # The old path must NOT be resolved (no fallback)
        assert all(
            call.args != ("~/mcpcat.log",)
            for call in mock_expanduser.call_args_list
        ), "write_to_log wrongly resolved ~/mcpcat.log"

        # The log line lands in the (patched) log file
        assert log_file.exists(), "Log file was not created"
        assert test_message in log_file.read_text(), (
            "Log message not found in log file"
        )

    def test_write_to_log_creates_file(self, tmp_path):
        """Test that write_to_log creates the log file if it doesn't exist."""
        # Enable debug mode
        set_debug_mode(True)

        # Use a unique file name for this test
        unique_id = str(uuid.uuid4())
        log_file = tmp_path / f"test_agentcat_{unique_id}.log"

        # Mock os.path.expanduser to use our temp file
        with patch(
            "agentcat.modules.logging.os.path.expanduser", return_value=str(log_file)
        ):
            # Write a test message
            test_message = f"Test log message {unique_id}"
            write_to_log(test_message)

            # Check that the file was created
            assert log_file.exists(), "Log file was not created"

            # Read the file content
            content = log_file.read_text()

            # Verify the message is in the file
            assert test_message in content, "Log message not found in file"

            # Verify timestamp format (ISO format)
            assert "T" in content, "Timestamp not in ISO format"

    def test_write_to_log_checks_debug_mode(self, tmp_path):
        """Test that write_to_log writes to file when debug mode is enabled."""
        # Enable debug mode
        set_debug_mode(True)

        # Use a unique file name for this test
        unique_id = str(uuid.uuid4())
        log_file = tmp_path / f"test_agentcat_{unique_id}.log"

        # Mock os.path.expanduser to use our temp file
        with patch(
            "agentcat.modules.logging.os.path.expanduser", return_value=str(log_file)
        ):
            # Write a test message
            test_message = f"Test log message {unique_id}"
            write_to_log(test_message)

            # Check that the file was created
            assert log_file.exists(), "Log file was not created"

            # Read the file content
            content = log_file.read_text()

            # Verify the message is in the file
            assert test_message in content, "Log message not found in file"

            # Verify timestamp format (ISO format)
            assert "T" in content, "Timestamp not in ISO format"

        # Check that log file is not created when debug mode is disabled
        set_debug_mode(False)

        # Use a unique file name for this test
        unique_id = str(uuid.uuid4())
        log_file = tmp_path / f"test_agentcat_{unique_id}.log"

        # Mock os.path.expanduser to use our temp file
        with patch(
            "agentcat.modules.logging.os.path.expanduser", return_value=str(log_file)
        ):
            # Write a test message
            test_message = f"Test log message {unique_id}"
            write_to_log(test_message)

            # Check that the file was created
            assert not log_file.exists(), "Log file was wrongly created"

    def test_write_to_log_appends_messages(self, tmp_path):
        """Test that write_to_log appends to existing log file."""
        # Enable debug mode
        set_debug_mode(True)

        # Use a unique file name for this test
        unique_id = str(uuid.uuid4())
        log_file = tmp_path / f"test_agentcat_{unique_id}.log"

        # Mock os.path.expanduser to use our temp file
        with patch(
            "agentcat.modules.logging.os.path.expanduser", return_value=str(log_file)
        ):
            # Write multiple messages with unique identifiers
            messages = [
                f"First message {unique_id}",
                f"Second message {unique_id}",
                f"Third message {unique_id}",
            ]
            for msg in messages:
                write_to_log(msg)
                time.sleep(0.01)  # Small delay to ensure different timestamps

            # Read the file content
            content = log_file.read_text()
            lines = content.strip().split("\n")

            # Filter lines to only those containing our unique_id
            # This prevents interference from other concurrent logging
            test_lines = [line for line in lines if unique_id in line]

            # Verify all messages are present
            assert len(test_lines) == len(messages), (
                f"Expected exactly {len(messages)} lines with unique_id, got {len(test_lines)}"
            )

            for i, msg in enumerate(messages):
                assert msg in test_lines[i], f"Message '{msg}' not found in line {i}"

            # Verify messages are in chronological order
            timestamps = []
            for line in test_lines:
                # Extract timestamp from [timestamp] format
                timestamp = line.split("] ")[0].strip("[")
                timestamps.append(timestamp)

            # Check timestamps are in ascending order
            assert timestamps == sorted(timestamps), (
                "Log entries are not in chronological order"
            )

    def test_write_to_log_handles_directory_creation(self, tmp_path):
        """Test that write_to_log creates parent directories if needed."""
        # Enable debug mode
        set_debug_mode(True)

        # Use a unique file name for this test
        unique_id = str(uuid.uuid4())
        log_file = tmp_path / f"test_agentcat_{unique_id}.log"

        # Mock os.path.expanduser to use our temp file
        with patch(
            "agentcat.modules.logging.os.path.expanduser", return_value=str(log_file)
        ):
            # Write a test message
            test_message = f"Test with directory creation {unique_id}"
            write_to_log(test_message)

            # Check that the file was created
            assert log_file.exists(), "Log file was not created"
            assert test_message in log_file.read_text(), "Message not written to file"

    def test_write_to_log_silently_handles_errors(self, tmp_path, monkeypatch):
        """Test that write_to_log doesn't raise exceptions on errors."""
        # Enable debug mode
        set_debug_mode(True)

        # Use a unique file name for this test
        unique_id = str(uuid.uuid4())
        log_file = tmp_path / f"test_agentcat_{unique_id}.log"

        # Mock os.path.expanduser to use our temp file
        with patch(
            "agentcat.modules.logging.os.path.expanduser", return_value=str(log_file)
        ):
            # Make the parent directory read-only to cause write failure
            log_file.parent.chmod(0o444)

            try:
                # This should not raise an exception
                write_to_log(f"This should fail silently {unique_id}")

                # If we get here without exception, the test passes
                assert True
            finally:
                # Restore permissions
                log_file.parent.chmod(0o755)

    def test_log_format(self, tmp_path):
        """Test the format of log entries."""
        # Enable debug mode
        set_debug_mode(True)
        
        # Use a unique file name for this test
        unique_id = str(uuid.uuid4())
        log_file = tmp_path / f"test_agentcat_{unique_id}.log"

        # Mock os.path.expanduser to use our temp file
        with patch(
            "agentcat.modules.logging.os.path.expanduser", return_value=str(log_file)
        ):
            # Write a test message
            test_message = f"Test format validation {unique_id}"
            write_to_log(test_message)

            # Read the log entry
            content = log_file.read_text().strip()

            # Verify format: "[ISO_TIMESTAMP] MESSAGE"
            assert content.startswith("["), "Log entry should start with ["
            assert "] " in content, (
                "Log entry should have timestamp in brackets followed by space"
            )

            # Extract timestamp and message
            bracket_end = content.index("] ")
            timestamp = content[1:bracket_end]  # Skip the opening bracket
            message = content[bracket_end + 2 :]  # Skip '] '

            # Verify ISO timestamp format (YYYY-MM-DDTHH:MM:SS.ssssss)
            assert len(timestamp) >= 19, "Timestamp too short"
            assert timestamp[4] == "-", "Invalid year-month separator"
            assert timestamp[7] == "-", "Invalid month-day separator"
            assert timestamp[10] == "T", "Invalid date-time separator"
            assert timestamp[13] == ":", "Invalid hour-minute separator"
            assert timestamp[16] == ":", "Invalid minute-second separator"

            # Verify message
            assert message == test_message, "Message content doesn't match"


class TestEnvDebugMode:
    """The AGENTCAT_DEBUG_MODE parse that seeds debug_mode at import time."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("true", True),
            ("TRUE", True),
            ("1", True),
            ("yes", True),
            ("on", True),
            ("false", False),
            ("0", False),
            ("", False),
            ("garbage", False),
        ],
    )
    def test_parses_truthy_tokens(self, monkeypatch, raw, expected):
        monkeypatch.setenv("AGENTCAT_DEBUG_MODE", raw)
        from agentcat.modules.logging import _env_debug_mode

        assert _env_debug_mode() is expected

    def test_unset_means_off(self, monkeypatch):
        monkeypatch.delenv("AGENTCAT_DEBUG_MODE", raising=False)
        from agentcat.modules.logging import _env_debug_mode

        assert _env_debug_mode() is False


class TestTrackDebugModePrecedence:
    """track() must honor: explicit option > AGENTCAT_DEBUG_MODE seed > off."""

    @pytest.fixture(autouse=True)
    def reset_debug_mode(self):
        yield
        set_debug_mode(False)

    def _track(self, tmp_path, **track_kwargs):
        """Run track(object()) with ~/agentcat.log redirected to tmp_path.

        The untrackable object takes the early no-project/no-exporters warning
        path in _apply_tracking, which write_to_log's — enough to observe the
        debug gate without touching the event queue.
        """
        import agentcat

        log_file = tmp_path / "agentcat.log"
        with patch(
            "agentcat.modules.logging.os.path.expanduser",
            return_value=str(log_file),
        ):
            agentcat.track(object(), **track_kwargs)
        return log_file

    def test_default_options_preserve_env_seeded_debug_mode(self, tmp_path):
        from agentcat.modules import logging as logging_module

        set_debug_mode(True)  # simulate AGENTCAT_DEBUG_MODE=true import seed
        log_file = self._track(tmp_path)

        assert logging_module.debug_mode is True, (
            "track() with default options clobbered the env-seeded debug flag"
        )
        assert log_file.exists(), "debug log was not written"
        assert "Failed to track server" in log_file.read_text()

    def test_explicit_true_enables_logging(self, tmp_path):
        import agentcat
        from agentcat.modules import logging as logging_module

        set_debug_mode(False)
        log_file = self._track(
            tmp_path, options=agentcat.AgentCatOptions(debug_mode=True)
        )

        assert logging_module.debug_mode is True
        assert log_file.exists()

    def test_explicit_false_overrides_env_seed(self, tmp_path):
        import agentcat
        from agentcat.modules import logging as logging_module

        set_debug_mode(True)  # simulate env seed
        log_file = self._track(
            tmp_path, options=agentcat.AgentCatOptions(debug_mode=False)
        )

        assert logging_module.debug_mode is False
        assert not log_file.exists()
