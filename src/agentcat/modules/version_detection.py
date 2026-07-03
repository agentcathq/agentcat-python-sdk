"""MCP version detection utilities."""

import importlib.metadata
from typing import Tuple, Optional


def get_mcp_version() -> Optional[str]:
    """Get the installed MCP version."""
    try:
        return importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        return None


def parse_version(version_str: str) -> Tuple[int, int, int]:
    """Parse version string to tuple of integers."""
    parts = version_str.split(".")
    major = int(parts[0]) if len(parts) > 0 else 0
    minor = int(parts[1]) if len(parts) > 1 else 0
    patch = int(parts[2]) if len(parts) > 2 else 0
    return (major, minor, patch)


def has_fastmcp_support() -> bool:
    """Check if the current MCP version supports FastMCP."""
    version = get_mcp_version()
    if not version:
        return False

    major, minor, _ = parse_version(version)

    # FastMCP was introduced after version 1.1
    if major < 1:
        return False
    if major == 1 and minor <= 1:
        return False

    return True


def can_import_fastmcp() -> bool:
    """Check if FastMCP can be imported."""
    try:
        from mcp.server import FastMCP

        return True
    except ImportError:
        return False
