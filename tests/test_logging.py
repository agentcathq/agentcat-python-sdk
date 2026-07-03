"""Tests for the logging module."""

import os
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from agentcat.modules.logging import write_to_log, set_debug_mode


class TestLogging:
    """Test the logging functionality."""

    @pytest.fixture(autouse=True)
    def cleanup_log_file(self):
        """Clean up the log file before and after each test."""
        log_path = os.path.expanduser("~/agentcat.log")

        # Clean up before test
        if os.path.exists(log_path):
            os.remove(log_path)

        yield

        # Clean up after test
        if os.path.exists(log_path):
            os.remove(log_path)

    def test_write_to_log_uses_agentcat_log_path(self):
        """Test that write_to_log writes to ~/agentcat.log (no ~/mcpcat.log fallback)."""
        # Enable debug mode
        set_debug_mode(True)

        log_path = os.path.expanduser("~/agentcat.log")
        old_log_path = os.path.expanduser("~/mcpcat.log")
        old_path_existed_before = os.path.exists(old_log_path)

        test_message = f"Default path test {uuid.uuid4()}"
        write_to_log(test_message)

        # The new default path must receive the entry
        assert os.path.exists(log_path), "~/agentcat.log was not created"
        assert test_message in Path(log_path).read_text(), (
            "Log message not found in ~/agentcat.log"
        )

        # The old path must NOT be written to (no fallback)
        if not old_path_existed_before:
            assert not os.path.exists(old_log_path), (
                "~/mcpcat.log was wrongly created"
            )
        else:
            assert test_message not in Path(old_log_path).read_text(), (
                "Log message wrongly written to ~/mcpcat.log"
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
