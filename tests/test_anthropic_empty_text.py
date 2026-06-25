"""H2 — the Anthropic serializer must never emit an empty/whitespace text block.

Anthropic's Messages API rejects a text block whose text is empty ("text content
blocks must be non-empty"). Because the agent re-sends the full history every
turn, one such block persisted in a stored assistant message then 400s every
later request in that conversation — permanently breaking it. The serializer must
drop empty/whitespace text while preserving tool_use, images, and real text.
"""

from __future__ import annotations

from flowly.providers.anthropic_provider import _messages_to_anthropic


def _empty_text_blocks(messages) -> list:
    bad = []
    for m in messages:
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text" and not str(b.get("text", "")).strip():
                bad.append(b)
    return bad


def _serialize(messages):
    system, out = _messages_to_anthropic(messages)
    # Fold system blocks in so the invariant covers them too.
    sys_msg = [{"role": "system", "content": system}] if system else []
    return sys_msg + out


def _types(message) -> list:
    return [b.get("type") for b in message.get("content") or [] if isinstance(b, dict)]


# ── the trigger cases (each used to 400) ─────────────────────────────────────

def test_whitespace_assistant_no_tools_emits_no_empty_block():
    out = _serialize([{"role": "assistant", "content": "  "}])
    assert _empty_text_blocks(out) == []


def test_whitespace_assistant_with_tools_keeps_tool_use_only():
    out = _serialize([
        {"role": "assistant", "content": "\n",
         "tool_calls": [{"id": "t1", "function": {"name": "shell", "arguments": "{}"}}]},
    ])
    assert _empty_text_blocks(out) == []
    assert _types(out[0]) == ["tool_use"]  # empty text dropped, tool_use kept


def test_list_of_only_empty_text_parts_emits_no_empty_block():
    out = _serialize([{"role": "assistant", "content": [{"type": "text", "text": "   "}]}])
    assert _empty_text_blocks(out) == []


def test_empty_system_emits_no_empty_block():
    out = _serialize([{"role": "system", "content": "  "}, {"role": "user", "content": "hi"}])
    assert _empty_text_blocks(out) == []


# ── no over-rejection: real content is preserved verbatim ────────────────────

def test_real_text_is_preserved_verbatim():
    out = _serialize([{"role": "assistant", "content": "  hello world  "}])
    texts = [b["text"] for b in out[0]["content"] if b.get("type") == "text"]
    assert texts == ["  hello world  "]  # surrounding whitespace kept, block not dropped


def test_real_text_with_tools_keeps_both():
    out = _serialize([
        {"role": "assistant", "content": "let me check",
         "tool_calls": [{"id": "t1", "function": {"name": "shell", "arguments": "{}"}}]},
    ])
    assert _types(out[0]) == ["text", "tool_use"]


def test_safe_common_case_empty_string_plus_tools():
    # content="" was already guarded; confirm it stays correct.
    out = _serialize([
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "function": {"name": "shell", "arguments": "{}"}}]},
    ])
    assert _empty_text_blocks(out) == []
    assert _types(out[0]) == ["tool_use"]


def test_whitespace_user_keeps_nonempty_placeholder():
    out = _serialize([{"role": "user", "content": "  "}])
    assert _empty_text_blocks(out) == []           # placeholder is non-empty
    assert _types(out[0]) == ["text"]


def test_image_only_user_is_preserved():
    out = _serialize([
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        ]},
    ])
    assert _empty_text_blocks(out) == []
    assert "image" in _types(out[0])
