"""Tests for markdown → Slack mrkdwn conversion and message splitting."""

from __future__ import annotations

import pytest

from flowly.channels.slack_format import (
    MAX_MESSAGE_LENGTH,
    split_message,
    to_mrkdwn,
)


@pytest.mark.parametrize(
    "md, expected",
    [
        ("**bold**", "*bold*"),
        ("__bold__", "*bold*"),
        ("*italic*", "_italic_"),
        ("_italic_", "_italic_"),
        ("~~strike~~", "~strike~"),
        ("[Anthropic](https://anthropic.com)", "<https://anthropic.com|Anthropic>"),
        ("![alt](https://x.com/i.png)", "<https://x.com/i.png|alt>"),
        ("# Heading", "*Heading*"),
        ("### Heading ###", "*Heading*"),
        ("- one\n- two", "• one\n• two"),
        ("* a\n+ b", "• a\n• b"),
    ],
)
def test_basic_conversions(md, expected):
    assert to_mrkdwn(md) == expected


def test_bold_then_italic_not_clobbered():
    # The ** → * bold pass must not be re-eaten by the single-* italic pass.
    assert to_mrkdwn("**bold** and *italic*") == "*bold* and _italic_"


def test_mixed_line():
    assert to_mrkdwn("**Ada** is an _engineer_ at [Acme](https://acme.com)") == (
        "*Ada* is an _engineer_ at <https://acme.com|Acme>"
    )


def test_code_block_preserved():
    src = "Use this:\n```python\nx = **not bold** [no](http://x)\n```\ndone **bold**"
    out = to_mrkdwn(src)
    # Inside the fence everything is verbatim; outside, bold converts.
    assert "x = **not bold** [no](http://x)" in out
    assert out.endswith("done *bold*")


def test_inline_code_preserved():
    assert to_mrkdwn("call `**foo**` then **bar**") == "call `**foo**` then *bar*"


def test_empty_and_none():
    assert to_mrkdwn("") == ""
    assert to_mrkdwn(None) is None  # type: ignore[arg-type]


def test_split_short_is_single_chunk():
    assert split_message("hello") == ["hello"]


def test_split_respects_limit_on_line_boundaries():
    lines = "\n".join(f"line {i}" for i in range(100))
    chunks = split_message(lines, limit=40)
    assert all(len(c) <= 40 for c in chunks)
    assert "\n".join(chunks) == lines  # lossless reassembly


def test_split_hard_splits_oversized_line():
    big = "x" * 100
    chunks = split_message(big, limit=40)
    assert all(len(c) <= 40 for c in chunks)
    assert "".join(chunks) == big


def test_split_default_limit_passthrough():
    text = "a" * (MAX_MESSAGE_LENGTH - 1)
    assert split_message(text) == [text]
    assert len(split_message("a" * (MAX_MESSAGE_LENGTH + 100))) == 2
