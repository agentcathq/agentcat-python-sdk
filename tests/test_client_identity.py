"""Per-request client identity + protocol version ladder (spec §7).

Client name/version now arrives per request under a fully-qualified `_meta`
key, with the pre-2026 initialize-time capture as the last rung. The ladder is
first-hit-wins and every field is narrowed to `isinstance(x, str)`, because a
non-string leaking into an event tag is a wire-format break. The two key
literals below are the cross-SDK wire contract — fix the implementation, never
the literal.
"""

from agentcat.modules.client_identity import (
    ClientIdentity,
    client_identity_from_meta,
    resolve_client_identity,
    resolve_protocol_version,
)

KEY = "io.modelcontextprotocol/clientInfo"
PV = "io.modelcontextprotocol/protocolVersion"


class FakeMeta:
    """mcp 1.x `RequestParams.Meta`: extras live in `.model_extra`, not on the
    object, and `.model_extra` is `None` unless the model allows extras."""

    def __init__(self, model_extra):
        self.model_extra = model_extra


def test_meta_narrowing():
    assert client_identity_from_meta({KEY: {"name": "cursor", "version": "2.1"}}) == ClientIdentity("cursor", "2.1")  # noqa: E501
    assert client_identity_from_meta({KEY: {"name": "cursor", "version": 7}}) == ClientIdentity("cursor", None)  # noqa: E501
    assert client_identity_from_meta({KEY: "junk"}) is None
    assert client_identity_from_meta(None) is None


# spec §7 rung 2 — pre-2026 servers hand us the pydantic Meta model, not a
# dict, and the fully-qualified key is an extra field rather than an attribute.
def test_meta_reads_pydantic_style_model_extra():
    meta = FakeMeta({KEY: {"name": "cursor", "version": "2.1"}})
    assert client_identity_from_meta(meta) == ClientIdentity("cursor", "2.1")
    assert client_identity_from_meta(FakeMeta(None)) is None
    assert client_identity_from_meta(FakeMeta({})) is None


# A meta object with no usable key must not be mistaken for a hit; the ladder
# relies on `None` to fall through to the next rung.
def test_meta_without_the_key_is_a_miss():
    assert client_identity_from_meta({}) is None
    assert client_identity_from_meta({"other": {"name": "cursor"}}) is None


def test_ladder_order_and_legacy():
    envelope = {KEY: {"name": "envelope", "version": "1"}}
    passthrough = {KEY: {"name": "meta", "version": "2"}}

    class Legacy:  # duck-typed clientInfo object
        name, version = "legacy", "3"

    assert resolve_client_identity([envelope, passthrough], lambda: Legacy()).name == "envelope"  # noqa: E501
    assert resolve_client_identity([None, passthrough], lambda: Legacy()).name == "meta"
    assert resolve_client_identity([None, None], lambda: Legacy()).name == "legacy"
    assert resolve_client_identity([None], lambda: (_ for _ in ()).throw(RuntimeError())) == ClientIdentity()  # noqa: E501


# A rung that is present but unusable is a miss, not a stop: junk under the key
# must not shadow a good value further down the ladder.
def test_unusable_meta_rung_falls_through():
    passthrough = {KEY: {"name": "meta", "version": "2"}}
    assert resolve_client_identity([{KEY: "junk"}, passthrough], lambda: None).name == "meta"  # noqa: E501
    assert resolve_client_identity([{}, FakeMeta({KEY: {"name": "extra"}})], lambda: None).name == "extra"  # noqa: E501


# spec §7 rung 3 — the legacy accessor is whatever the era hands back:
# a clientInfo model, a plain dict, `None` (v2 sessions may have none), or an
# exception from touching a torn-down request context. All four resolve, never
# raise, and the empty identity is the floor.
def test_legacy_rung_shapes():
    assert resolve_client_identity([], lambda: {"name": "dict", "version": "4"}) == ClientIdentity("dict", "4")  # noqa: E501
    assert resolve_client_identity([None], lambda: None) == ClientIdentity()
    assert resolve_client_identity([], lambda: None) == ClientIdentity()

    def boom():
        raise RuntimeError("request context is gone")

    assert resolve_client_identity([None, None], boom) == ClientIdentity()


# Narrowing applies to every rung, not just the meta ones.
def test_legacy_rung_is_narrowed_per_field():
    class Weird:
        name, version = "legacy", 3

    identity = resolve_client_identity([], lambda: Weird())
    assert identity == ClientIdentity("legacy", None)


def test_protocol_version():
    assert resolve_protocol_version([{PV: "2026-07-28"}]) == "2026-07-28"
    assert resolve_protocol_version([None], fallback="2026-07-28") == "2026-07-28"
    assert resolve_protocol_version([{PV: 9}]) is None


# Same ladder shape as the identity resolver: first hit wins, misses fall
# through, pydantic Meta is read the same way, and the fallback is the floor.
def test_protocol_version_ladder():
    assert resolve_protocol_version([{PV: "first"}, {PV: "second"}]) == "first"
    assert resolve_protocol_version([{}, {PV: "second"}]) == "second"
    assert resolve_protocol_version([FakeMeta({PV: "2026-07-28"})]) == "2026-07-28"
    assert resolve_protocol_version([FakeMeta(None)], fallback="fb") == "fb"
    assert resolve_protocol_version([]) is None
