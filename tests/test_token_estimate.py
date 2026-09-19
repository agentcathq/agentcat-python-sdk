"""Shared token-estimate vectors, pinned byte-for-byte against the TypeScript
and Go SDKs. See the TypeScript repo's
docs/superpowers/specs/2026-09-19-sdk-token-estimates-design.md."""

import pytest

from agentcat.modules.token_estimate import (
    TOKEN_ESTIMATE_BYTES_PER_TOKEN,
    estimate_input_tokens,
    estimate_output_tokens,
    estimate_tokens,
)


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def test_the_divisor_is_pinned():
    assert TOKEN_ESTIMATE_BYTES_PER_TOKEN == 3.5


@pytest.mark.parametrize(
    "byte_count, tokens",
    [(0, 0), (1, 1), (7, 2), (13, 4), (4096, 1171), (7516192765, 2147483647)],
)
def test_estimate_tokens(byte_count, tokens):
    assert estimate_tokens(byte_count) == tokens


@pytest.mark.parametrize(
    "arguments, tokens",
    [
        ({"q": "hello"}, 4),
        ({}, 1),
        ({"name": "café"}, 5),  # 20 bytes -> 6 under ensure_ascii; must be 16 -> 5
        ({"html": "<a>&</a>"}, 6),
        ({"t": "你好"}, 4),
        ({"ids": [1, 2, 3], "opts": {"deep": True, "n": None}}, 13),
    ],
)
def test_estimate_input_tokens(arguments, tokens):
    assert estimate_input_tokens(arguments) == tokens


def test_absent_arguments_are_omitted():
    assert estimate_input_tokens(None) is None


def test_lone_surrogate_counts_three_bytes_on_the_input_side():
    # A lone surrogate off the wire (json.loads on an unpaired \ud83d escape)
    # must count, not be omitted: surrogatepass encodes it to 3 bytes, the
    # same width Go counts after its decoder substitutes U+FFFD.
    # {"a":"<surrogate>"} = 6 + 3 + 2 = 11 bytes -> ceil(11 / 3.5) = 4.
    assert estimate_input_tokens({"a": "\ud83d"}) == 4


def test_unserializable_arguments_are_omitted():
    # No default=str: TypeScript and Go both omit an unserializable value
    # rather than counting a stand-in string, so Python must match.
    assert estimate_input_tokens({"when": object()}) is None


class _Hostile:
    def __str__(self) -> str:
        raise RuntimeError("boom")


class _HostileResponse(dict):
    def get(self, *args, **kwargs):
        raise RuntimeError("boom")


def test_never_raises_on_the_input_side():
    # json.dumps raises TypeError on an unserializable value before __str__
    # is ever consulted (no default=str); the field is omitted either way,
    # so a hostile __str__ never gets the chance to raise into the call.
    assert estimate_input_tokens({"when": _Hostile()}) is None


def test_never_raises_on_the_output_side():
    assert estimate_output_tokens(_HostileResponse(content=[])) is None


@pytest.mark.parametrize(
    "response, tokens",
    [
        ({"content": [text("hello world")]}, 4),
        ({"content": [text("abcd"), text("e")]}, 2),
        ({"content": [text("")]}, 0),
        ({"content": [{"type": "image", "data": "QUJD", "mimeType": "image/png"}]}, 0),
        (
            {"content": [{"type": "resource", "resource": {"uri": "file:///a", "text": "resource body"}}]},
            4,
        ),
        ({"content": [{"type": "resource", "resource": {"uri": "file:///a", "blob": "QUJD"}}]}, 0),
        ({"content": [text("hi")], "structuredContent": {"big": "y" * 1000}}, 1),
        ({"content": [text("hi")], "structured_content": {"big": "y" * 1000}}, 1),
        ({"content": [text("x" * 4096)]}, 1171),
        ({"content": [text("hi")], "isError": True}, 1),
        ({"content": [{"type": "text", "text": 42}, None, "str"]}, 0),
    ],
)
def test_estimate_output_tokens(response, tokens):
    assert estimate_output_tokens(response) == tokens


def test_no_content_list_falls_back_to_the_whole_response():
    assert estimate_output_tokens({"result": "ok"}) == 5


def test_lone_surrogate_counts_three_bytes_on_the_output_side():
    # A lone surrogate is 3 bytes -> ceil(3 / 3.5) = 1.
    assert estimate_output_tokens({"content": [text("\ud83d")]}) == 1


def test_absent_response_is_omitted():
    assert estimate_output_tokens(None) is None
