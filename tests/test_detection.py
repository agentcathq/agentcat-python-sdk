"""Per-object server flavor classification (spec §8.1).

The classifier decides which adapter wraps a customer's server, so a
misclassification silently untracks a fleet or wraps the wrong handler table.
Every double here is built from class name + module path + attribute presence
only — exactly the signals `detect_server` is allowed to read. It must never
import version-specific symbols, and no probe may raise into the customer's
process.
"""

import pytest

from agentcat.modules.detection import Detection, ServerFlavor, detect_server

from .test_utils.flavors import FASTMCP_MAJOR, MCP_MAJOR, flavor_ids, flavors

# spec §8.1 — the fingerprint keys logged for unrecognized shapes; the beacon
# is only useful for fleet drift detection if every probe always reports.
PROBE_KEYS = [
    "is_fastmcp_class",
    "has_local_provider",
    "has_add_middleware",
    "has_middleware",
    "has_tool_manager",
    "has_mcp_server_attr",
    "has_lowlevel_server_attr",
    "has_extensions",
    "has_request_state_security",
    "has_request_handlers",
    "has_private_request_handlers",
    "has_add_request_handler",
    "has_request_context",
]

# Every attribute name a probe could touch.
PROBE_NAMES = (
    "_local_provider",
    "add_middleware",
    "middleware",
    "_tool_manager",
    "_mcp_server",
    "_lowlevel_server",
    "_extensions",
    "add_extension",
    "_request_state_security",
    "request_handlers",
    "_request_handlers",
    "add_request_handler",
    "_add_request_handler",
    "request_context",
)


def make(name, module, **attrs):
    cls = type(name, (), {})
    cls.__module__ = module
    obj = cls()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def make_exploding(name, module, raising, **attrs):
    """Double whose attribute ACCESS raises on `raising` names.

    `hasattr` only swallows AttributeError, so an unguarded probe of a property
    that raises anything else propagates into `track()`.
    """
    namespace = {}
    for attr in raising:

        def _boom(self, _attr=attr):
            raise RuntimeError(f"probing {_attr} exploded")

        namespace[attr] = property(_boom)
    cls = type(name, (), namespace)
    cls.__module__ = module
    obj = cls()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


COMMUNITY_V3_ATTRS = {
    "_local_provider": object(),
    "add_middleware": lambda m: None,
    "middleware": [],
}


def lowlevel_v1():
    return make(
        "Server",
        "mcp.server.lowlevel.server",
        request_handlers={},
        request_context=None,
    )


def lowlevel_v2(seam="add_request_handler"):
    return make(
        "Server",
        "mcp.server.lowlevel.server",
        _request_handlers={},
        **{seam: lambda *a: None},
    )


# The handler-registration seam has been spelled both ways on the 2.x line:
# `_add_request_handler` on the development line (see the vendored checkout at
# model-context-protocol-sdks/python-sdk), `add_request_handler` in 2.0.0. A
# build shipping only the other spelling must not fall through to UNKNOWN —
# that would return every lowlevel-v2 server untracked with no error raised
# and no visible failure, which is the worst way for a fleet to drift.
def test_lowlevel_v2_is_classified_under_either_registration_spelling():
    for seam in ("add_request_handler", "_add_request_handler"):
        server = lowlevel_v2(seam)
        detected = detect_server(server)
        assert detected.flavor is ServerFlavor.LOWLEVEL_V2, seam
        assert detected.lowlevel is server
        assert detected.fingerprint["has_add_request_handler"] is True, seam


def test_mcpserver_v2_is_classified_under_either_registration_spelling():
    for seam in ("add_request_handler", "_add_request_handler"):
        lowlevel = lowlevel_v2(seam)
        server = make(
            "MCPServer",
            "mcp.server.mcpserver.server",
            _lowlevel_server=lowlevel,
            _tool_manager=object(),
        )
        detected = detect_server(server)
        assert detected.flavor is ServerFlavor.MCPSERVER_V2, seam
        assert detected.lowlevel is lowlevel


def test_community_v4_vs_v3():
    base = COMMUNITY_V3_ATTRS
    v3 = make("FastMCP", "fastmcp.server.server", **base)
    v4 = make(
        "FastMCP",
        "fastmcp.server.server",
        **base,
        _extensions={},
        add_extension=lambda e: None,
    )
    assert detect_server(v3).flavor is ServerFlavor.COMMUNITY_V3
    assert detect_server(v4).flavor is ServerFlavor.COMMUNITY_V4


# spec §8.1 — v4 is v3 plus ANY of the three discriminators, so a build that
# grew only one of them must not fall back to the v3 middleware era.
def test_each_v4_discriminator_alone_is_enough():
    discriminators = (
        {"add_extension": lambda e: None},
        {"_extensions": {}},
        {"_request_state_security": object()},
    )
    for extra in discriminators:
        server = make("FastMCP", "fastmcp.server.server", **COMMUNITY_V3_ATTRS, **extra)
        assert detect_server(server).flavor is ServerFlavor.COMMUNITY_V4


def test_community_v2_unsupported():
    v2 = make("FastMCP", "fastmcp.server", _mcp_server=object(), _tool_manager=object())
    assert detect_server(v2).flavor is ServerFlavor.COMMUNITY_V2_UNSUPPORTED


# The detection-order hazard: community v2 and official FastMCP v1 are
# attribute-identical (`_mcp_server` + `_tool_manager`, class named FastMCP);
# only the module prefix separates them. Guard each against the other so a
# reordered or loosened prefix check fails here and not in a customer's
# process — one direction untracks a supported server, the other adapts an
# unsupported one.
def test_community_v2_and_official_fastmcp_v1_are_never_confused():
    ll1 = lowlevel_v1()
    community = make(
        "FastMCP", "fastmcp.server", _mcp_server=object(), _tool_manager=object()
    )
    official = make(
        "FastMCP", "mcp.server.fastmcp.server", _mcp_server=ll1, _tool_manager=object()
    )
    assert detect_server(community).flavor is ServerFlavor.COMMUNITY_V2_UNSUPPORTED
    assert detect_server(community).flavor is not ServerFlavor.OFFICIAL_FASTMCP_V1
    assert detect_server(official).flavor is ServerFlavor.OFFICIAL_FASTMCP_V1
    assert detect_server(official).flavor is not ServerFlavor.COMMUNITY_V2_UNSUPPORTED


def test_official_flavors():
    ll1 = lowlevel_v1()
    ll2 = lowlevel_v2()
    fm1 = make(
        "FastMCP", "mcp.server.fastmcp.server", _mcp_server=ll1, _tool_manager=object()
    )
    ms2 = make(
        "MCPServer",
        "mcp.server.mcpserver.server",
        _lowlevel_server=ll2,
        _tool_manager=object(),
    )
    assert detect_server(ll1).flavor is ServerFlavor.LOWLEVEL_V1
    d = detect_server(ll2)
    assert d.flavor is ServerFlavor.LOWLEVEL_V2 and d.lowlevel is ll2
    d = detect_server(fm1)
    assert d.flavor is ServerFlavor.OFFICIAL_FASTMCP_V1 and d.lowlevel is ll1
    d = detect_server(ms2)
    assert d.flavor is ServerFlavor.MCPSERVER_V2 and d.lowlevel is ll2


# spec §8.1 — a bare lowlevel v1 Server is adapted in place, so `lowlevel` is
# the server itself rather than None.
def test_bare_lowlevel_servers_are_their_own_lowlevel():
    ll1 = lowlevel_v1()
    assert detect_server(ll1).lowlevel is ll1


# spec §8.1 — `lowlevel` is the object the adapters wrap; community flavors go
# through middleware and unknown shapes go untracked, so neither has one.
# A non-None value here would send an adapter at a FastMCP instance.
def test_lowlevel_is_none_for_community_and_unknown():
    v3 = make("FastMCP", "fastmcp.server.server", **COMMUNITY_V3_ATTRS)
    v2 = make("FastMCP", "fastmcp.server", _mcp_server=object(), _tool_manager=object())
    assert detect_server(v3).lowlevel is None
    assert detect_server(v2).lowlevel is None
    assert detect_server(object()).lowlevel is None


def test_unknown_shape_has_fingerprint():
    d = detect_server(object())
    assert d.flavor is ServerFlavor.UNKNOWN and isinstance(d.fingerprint, dict)


# The fingerprint is the payload of the fleet-drift beacon, so every probe must
# report on every shape (including the ones that classified cleanly) and report
# a plain bool — not a truthy handler table or a raw attribute value.
def test_fingerprint_reports_every_probe_as_a_bool():
    for server in (lowlevel_v1(), lowlevel_v2(), object()):
        d = detect_server(server)
        assert isinstance(d, Detection)
        for key in PROBE_KEYS:
            assert key in d.fingerprint, key
        assert all(isinstance(v, bool) for v in d.fingerprint.values())
    fp = detect_server(lowlevel_v1()).fingerprint
    assert fp["has_request_handlers"] is True and fp["has_request_context"] is True
    assert fp["is_fastmcp_class"] is False and fp["has_tool_manager"] is False


# track() must never raise (spec §3.1), and a customer server can expose a
# property on any probed name — lazy config, a deprecation shim, a proxy — that
# blows up on access. hasattr() re-raises anything that is not AttributeError,
# so every probe has to be wrapped.
def test_probes_never_raise_and_still_classify():
    ll1 = make_exploding(
        "Server",
        "mcp.server.lowlevel.server",
        raising=(
            "_tool_manager",
            "_mcp_server",
            "_lowlevel_server",
            "_local_provider",
            "middleware",
            "_extensions",
            "_request_state_security",
        ),
        request_handlers={},
        request_context=None,
    )
    assert detect_server(ll1).flavor is ServerFlavor.LOWLEVEL_V1


def test_a_server_that_explodes_on_every_probe_is_unknown():
    hostile = make_exploding("Mystery", "vendor.server", raising=PROBE_NAMES)
    d = detect_server(hostile)
    assert d.flavor is ServerFlavor.UNKNOWN
    assert all(isinstance(v, bool) for v in d.fingerprint.values())


# The flavor is logged and shipped on the diagnostics beacon, so it has to
# serialize as a string; `str, Enum` is the contract in the interface list.
def test_flavor_is_a_string_enum_with_distinct_values():
    assert isinstance(ServerFlavor.UNKNOWN, str)
    values = [f.value for f in ServerFlavor]
    assert len(set(values)) == len(values)
    assert all(isinstance(v, str) and v for v in values)


# ── real SDK objects ─────────────────────────────────────────────────────────
#
# Everything above is a synthetic double, deliberately: the doubles are how the
# classifier's RULES get tested — shapes no installed SDK produces, probes that
# raise, a fingerprint that must report every key. But a double encodes the
# same assumptions the classifier does, so on its own the suite cannot notice
# an upstream rename: FastMCP moving `_local_provider`, or `MCPServer` renaming
# `_lowlevel_server`, would leave every test above green and every real server
# UNKNOWN — silently untracked, in the field.
#
# These two run the classifier against the objects the SDKs actually build.


class TestRealServerObjects:
    """The classifier, against real servers from the installed dependency set."""

    def test_this_era_can_build_every_shape_it_is_supposed_to(self):
        """Nothing dropped out of the harness the cross-flavor suites use.

        `flavors()` is what those suites parametrize over, so a shape missing
        from it is coverage that vanishes without a failure anywhere. The era
        tables are restated here rather than imported, so the two have to
        agree.
        """
        official = {
            1: {"official-fastmcp-v1", "lowlevel-v1"},
            2: {"mcpserver-v2", "lowlevel-v2"},
        }[MCP_MAJOR]
        community = {f"community-v{FASTMCP_MAJOR}"} if FASTMCP_MAJOR else set()
        assert set(flavor_ids()) == official | community

    @pytest.mark.parametrize("flavor", flavors(), ids=lambda f: f.id)
    def test_a_real_server_classifies_as_the_flavor_it_is(self, flavor):
        """The one assertion a synthetic double structurally cannot make."""
        server = flavor.build("detection").server
        detection = detect_server(server)

        assert detection.flavor is flavor.flavor
        # The object the adapter is handed: the wrapped lowlevel server for the
        # official facades, the server itself for a bare lowlevel one, and
        # None for the community flavors, which are adapted by middleware.
        if flavor.flavor in (ServerFlavor.COMMUNITY_V3, ServerFlavor.COMMUNITY_V4):
            assert detection.lowlevel is None
        else:
            assert detection.lowlevel is not None
        # Every probe answered on a real object — none raised, none went
        # missing — which is what the fleet-drift beacon carries.
        assert sorted(detection.fingerprint) == sorted(PROBE_KEYS)
        assert all(isinstance(v, bool) for v in detection.fingerprint.values())
