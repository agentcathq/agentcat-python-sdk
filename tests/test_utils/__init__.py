"""Test utilities for AgentCat tests."""

import os
from pathlib import Path

import pytest

LOG_FILE = "agentcat.log"


def cleanup_log_file():
    """Remove the log file if it exists."""
    if os.path.exists(LOG_FILE):
        os.unlink(LOG_FILE)


@pytest.fixture(autouse=True)
def setup_test_environment():
    """Set up and tear down test environment."""
    # Clean up before test
    cleanup_log_file()

    yield

    # Clean up after test
    cleanup_log_file()
