"""Gemini thinking models attach a ``thought_signature`` (OpenAI-compat
``extra_content``) to each tool call and reject the next turn with HTTP 400 if
it isn't replayed — while strict non-Gemini providers reject any payload that
*carries* it. These tests cover the capture / replay / strip-unless-Gemini logic
in the OpenRouter (OpenAI-compat) provider.
"""

from __future__ import annotations

from flowly.providers.base import ToolCallRequest
from flowly.providers.openrouter_provider import (
    OpenRouterProvider,
    _extract_tool_call_extra,
    _model_consumes_thought_signature,
    _strip_tool_call_extra_content,
)

SIG = {"google": {"thought_signature": "abc123"}}


# -- model gating -----------------------------------------------------------

def test_model_gating():
    assert _model_consumes_thought_signature("gemini-2.5-flash")
    assert _model_consumes_thought_signature("models/gemini-3-pro")
    assert _model_consumes_thought_signature("gemma-3-27b")
    assert not _model_consumes_thought_signature("anthropic/claude-haiku-4.5")
    assert not _model_consumes_thought_signature("openai/gpt-4o")
    assert not _model_consumes_thought_signature(None)


# -- extract from SDK objects ----------------------------------------------

class _ModelExtraTC:
    def __init__(self, extra):
        self.model_extra = {"extra_content": extra}


class _AttrTC:
    """Pydantic-like with a model_dump()-able extra_content attribute."""
    class _Extra:
        def model_dump(self):
            return SIG
    extra_content = _Extra()


def test_extract_from_model_extra():
    assert _extract_tool_call_extra(_ModelExtraTC(SIG)) == SIG


def test_extract_from_attr_with_model_dump():
    assert _extract_tool_call_extra(_AttrTC()) == SIG


def test_extract_none_when_absent():
    assert _extract_tool_call_extra(object()) is None
    assert _extract_tool_call_extra(_ModelExtraTC(None)) is None


# -- strip-unless-Gemini ----------------------------------------------------

def _assistant_with_sig():
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "search", "arguments": "{}"},
            "extra_content": SIG,
        }],
    }


def test_strip_removes_extra_content_without_mutating_input():
    msgs = [{"role": "user", "content": "hi"}, _assistant_with_sig()]
    out = _strip_tool_call_extra_content(msgs)
    # input untouched
    assert "extra_content" in msgs[1]["tool_calls"][0]
    # output stripped
    assert "extra_content" not in out[1]["tool_calls"][0]
    # other fields preserved
    assert out[1]["tool_calls"][0]["function"]["name"] == "search"
    # message without tool_calls passes through identically
    assert out[0] is msgs[0]


def test_prepare_messages_keeps_for_gemini_strips_for_others():
    p = OpenRouterProvider(api_key="test")
    gem = p._prepare_messages([_assistant_with_sig()], "gemini-2.5-flash")
    assert gem[0]["tool_calls"][0].get("extra_content") == SIG
    other = p._prepare_messages([_assistant_with_sig()], "openai/gpt-4o")
    assert "extra_content" not in other[0]["tool_calls"][0]
    # Claude path also strips (then applies cache control) — must not carry it.
    claude = p._prepare_messages([_assistant_with_sig()], "anthropic/claude-haiku-4.5")
    assert "extra_content" not in claude[0]["tool_calls"][0]


# -- ToolCallRequest carries it --------------------------------------------

def test_tool_call_request_field_default_and_set():
    assert ToolCallRequest(id="1", name="x", arguments={}).extra_content is None
    tc = ToolCallRequest(id="1", name="x", arguments={}, extra_content=SIG)
    assert tc.extra_content == SIG
