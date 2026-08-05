"""Per-request client identity + protocol version ladder (spec §7).

Cross-SDK contract: 2026-07-28-cross-sdk-changelog.md §5.1; TS reference
src/modules/session.ts (``narrowClientInfo`` / ``getClientInfoForRequest``).
Client name/version arrives per request under the fully-qualified
``META_CLIENT_INFO_KEY`` meta key (envelope or ``_meta`` passthrough); the
pre-2026 initialize-time capture is the last rung, supplied lazily by the
adapter as a callable. First hit wins and every rung is narrowed per field
to ``isinstance(x, str)`` — a non-string leaking into an event tag is a
wire-format break.

A rung that is present but unusable (junk under the key, no string field)
is a miss, not a stop: it must not shadow a good value further down the
ladder. Nothing here raises — a torn-down request context or a hostile
meta object resolves to a miss.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agentcat.modules.constants import (
    META_CLIENT_INFO_KEY,
    META_PROTOCOL_VERSION_KEY,
)


@dataclass
class ClientIdentity:
    name: str | None = None
    version: str | None = None


def _meta_value(meta: Any, key: str) -> Any:
    """Raw value under a fully-qualified meta key; ``None`` on any miss.

    Accepts plain mappings (v2 envelopes / lifted meta) and pydantic
    ``RequestParams.Meta``-style objects whose extras live in
    ``.model_extra`` (``None`` when the model forbids extras).
    """
    if meta is None:
        return None
    try:
        if isinstance(meta, Mapping):
            return meta.get(key)
        extra = getattr(meta, "model_extra", None)
        if isinstance(extra, Mapping) and key in extra:
            return extra[key]
        return getattr(meta, key, None)
    except Exception:
        return None


def _narrow(raw: Any) -> ClientIdentity | None:
    """Per-field narrowing of a clientInfo-shaped mapping or object; a value
    with no usable string field is a miss (``None``), mirroring the TS
    narrower."""
    if raw is None:
        return None
    try:
        if isinstance(raw, Mapping):
            name = raw.get("name")
            version = raw.get("version")
        else:
            name = getattr(raw, "name", None)
            version = getattr(raw, "version", None)
    except Exception:
        return None
    narrowed_name = name if isinstance(name, str) else None
    narrowed_version = version if isinstance(version, str) else None
    if narrowed_name is None and narrowed_version is None:
        return None
    return ClientIdentity(name=narrowed_name, version=narrowed_version)


def client_identity_from_meta(meta: Any) -> ClientIdentity | None:
    return _narrow(_meta_value(meta, META_CLIENT_INFO_KEY))


def resolve_client_identity(
    meta_sources: list[Any], legacy: Callable[[], Any | None]
) -> ClientIdentity:
    for meta in meta_sources:
        identity = client_identity_from_meta(meta)
        if identity is not None:
            return identity
    try:
        legacy_info = legacy()
    except Exception:
        legacy_info = None
    identity = _narrow(legacy_info)
    return identity if identity is not None else ClientIdentity()


def resolve_protocol_version(
    meta_sources: list[Any], fallback: str | None = None
) -> str | None:
    for meta in meta_sources:
        value = _meta_value(meta, META_PROTOCOL_VERSION_KEY)
        # Non-empty like TS getProtocolVersion (`value.length > 0`).
        if isinstance(value, str) and value:
            return value
    return fallback
