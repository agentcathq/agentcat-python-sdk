"""Utility functions for AgentCat."""

import functools
from typing import Optional
from datetime import datetime, timezone

from .thirdparty.ksuid import Ksuid, KsuidMs


def get_agentcat_version() -> str | None:
    """The installed AgentCat SDK version, or None if it cannot be read.

    Lives here rather than in the package root so the event pipeline and the
    telemetry exporters can stamp it without importing `agentcat` itself.
    """
    try:
        import importlib.metadata

        return importlib.metadata.version("agentcat")
    except Exception:
        return None


@functools.cache
def get_dist_version(name: str) -> str | None:
    """Best-effort installed-distribution version; None when absent.

    Shared by the log-line version suffix, the diagnostics beacon, and the
    OTLP exporter to stamp the MCP SDK in use (`mcp` and/or `fastmcp`), so
    the lookup runs once per distribution per process.
    """
    try:
        import importlib.metadata

        return importlib.metadata.version(name)
    except Exception:
        return None


def generate_ksuid(
    use_milliseconds: bool = False, dt: Optional[datetime] = None
) -> str:
    """
    Generate a KSUID (K-Sortable Unique Identifier).

    Args:
        use_milliseconds: If True, uses KsuidMs for millisecond precision
        dt: Optional datetime to use for the timestamp portion

    Returns:
        A base62-encoded KSUID string
    """
    if use_milliseconds:
        return str(KsuidMs(datetime=dt))
    return str(Ksuid(datetime=dt))


def generate_prefixed_ksuid(
    prefix: str, use_milliseconds: bool = False, dt: Optional[datetime] = None
) -> str:
    """
    Generate a prefixed KSUID (e.g., "ses_2cOHEO0LYGADMzRvWTXXVbbgxgm").

    Args:
        prefix: The prefix to add (e.g., "ses", "usr", "evt")
        use_milliseconds: If True, uses KsuidMs for millisecond precision
        dt: Optional datetime to use for the timestamp portion

    Returns:
        A prefixed base62-encoded KSUID string
    """
    ksuid = generate_ksuid(use_milliseconds=use_milliseconds, dt=dt)
    return f"{prefix}_{ksuid}"


def parse_ksuid(ksuid_str: str, use_milliseconds: bool = False) -> Ksuid:
    """
    Parse a KSUID string back into a Ksuid object.

    Args:
        ksuid_str: The base62-encoded KSUID string
        use_milliseconds: If True, parses as KsuidMs

    Returns:
        A Ksuid or KsuidMs object
    """
    if use_milliseconds:
        return KsuidMs.from_base62(ksuid_str)
    return Ksuid.from_base62(ksuid_str)


def parse_prefixed_ksuid(
    prefixed_ksuid: str, use_milliseconds: bool = False
) -> tuple[str, Ksuid]:
    """
    Parse a prefixed KSUID string back into its prefix and Ksuid object.

    Args:
        prefixed_ksuid: The prefixed KSUID string
            (e.g., "ses_2cOHEO0LYGADMzRvWTXXVbbgxgm")
        use_milliseconds: If True, parses as KsuidMs

    Returns:
        A tuple of (prefix, Ksuid object)
    """
    if "_" not in prefixed_ksuid:
        raise ValueError("Invalid prefixed KSUID format. Expected format: prefix_ksuid")

    prefix, ksuid_str = prefixed_ksuid.split("_", 1)
    ksuid = parse_ksuid(ksuid_str, use_milliseconds=use_milliseconds)
    return prefix, ksuid
