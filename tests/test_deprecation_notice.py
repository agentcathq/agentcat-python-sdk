"""The mcpcat -> agentcat rename notice must be visible with default warning
filters (the reason it is a FutureWarning), and must never touch stdout,
which would corrupt stdio MCP transports."""

import subprocess
import sys


def _import_mcpcat_subprocess() -> subprocess.CompletedProcess:
    # A fresh interpreter with default warning filters — pytest's own filter
    # config must not leak into what real users see.
    return subprocess.run(
        [sys.executable, "-c", "import mcpcat"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_rename_notice_visible_by_default():
    result = _import_mcpcat_subprocess()
    assert result.returncode == 0
    assert "FutureWarning" in result.stderr
    assert "renamed to 'agentcat'" in result.stderr
    assert "pip install agentcat" in result.stderr


def test_rename_notice_never_writes_to_stdout():
    result = _import_mcpcat_subprocess()
    assert result.returncode == 0
    assert result.stdout == ""
