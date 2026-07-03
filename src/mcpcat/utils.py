"""Utility functions for AgentCat."""

from typing import Optional
from datetime import datetime, timezone

from .thirdparty.ksuid import Ksuid, KsuidMs


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
    Generate a prefixed KSUID (e.g., "ses_iewjf9023rjdf").

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
        prefixed_ksuid: The prefixed KSUID string (e.g., "ses_iewjf9023rjdf")
        use_milliseconds: If True, parses as KsuidMs

    Returns:
        A tuple of (prefix, Ksuid object)
    """
    if "_" not in prefixed_ksuid:
        raise ValueError("Invalid prefixed KSUID format. Expected format: prefix_ksuid")

    prefix, ksuid_str = prefixed_ksuid.split("_", 1)
    ksuid = parse_ksuid(ksuid_str, use_milliseconds=use_milliseconds)
    return prefix, ksuid
