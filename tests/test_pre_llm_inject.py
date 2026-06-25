"""Tests for AgentLoop._inject_pre_llm_context.

The injection helper is the bridge between plugin pre_llm_call hooks and
the model. Without it, plugins can register all the hooks they want but
nothing reaches the model. We test the helper directly to keep the
suite fast — no real LLM, no full agent boot.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from flowly.agent.hooks import HookRegistry, LLMHookContext
from flowly.agent.tools.registry import ToolRegistry


def _make_loop():
    """Build a minimal AgentLoop-like object with just the helper attached."""
    from flowly.agent.loop import AgentLoop

    # Construct an empty shell — bypass __init__ since it requires a provider
    loop = AgentLoop.__new__(AgentLoop)
    loop.hooks = HookRegistry()
    loop.tools = ToolRegistry()
    return loop


class TestInjectPreLLMContext:
    @pytest.mark.asyncio
    async def test_no_plugins_returns_messages_unchanged(self):
        loop = _make_loop()
        original = [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "Hi"},
        ]
        result = await loop._inject_pre_llm_context(
            messages=original, tools=None, model="test"
        )
        # No plugins registered → no mutation
        assert result == original

    @pytest.mark.asyncio
    async def test_string_return_is_injected_into_user_message(self):
        loop = _make_loop()
        loop.hooks.register("pre_llm_call", lambda ctx: "user is a lawyer")
        messages = [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "Find Mehmet"},
        ]
        result = await loop._inject_pre_llm_context(
            messages=messages, tools=None, model="test"
        )
        assert result[0] == messages[0]  # system unchanged
        assert "<plugin_context>" in result[1]["content"]
        assert "user is a lawyer" in result[1]["content"]
        assert "Find Mehmet" in result[1]["content"]

    @pytest.mark.asyncio
    async def test_dict_return_with_context_key(self):
        loop = _make_loop()
        loop.hooks.register("pre_llm_call", lambda ctx: {"context": "via dict"})
        messages = [{"role": "user", "content": "hello"}]
        result = await loop._inject_pre_llm_context(
            messages=messages, tools=None, model="test"
        )
        assert "via dict" in result[0]["content"]

    @pytest.mark.asyncio
    async def test_multiple_plugins_concatenated(self):
        loop = _make_loop()
        loop.hooks.register("pre_llm_call", lambda ctx: "first")
        loop.hooks.register("pre_llm_call", lambda ctx: "second")
        messages = [{"role": "user", "content": "go"}]
        result = await loop._inject_pre_llm_context(
            messages=messages, tools=None, model="test"
        )
        # Both should be present, joined with a blank line
        assert "first" in result[0]["content"]
        assert "second" in result[0]["content"]

    @pytest.mark.asyncio
    async def test_injected_at_last_user_message_not_first(self):
        loop = _make_loop()
        loop.hooks.register("pre_llm_call", lambda ctx: "tag-here")
        messages = [
            {"role": "user", "content": "older question"},
            {"role": "assistant", "content": "older answer"},
            {"role": "user", "content": "current question"},
        ]
        result = await loop._inject_pre_llm_context(
            messages=messages, tools=None, model="test"
        )
        assert result[0]["content"] == "older question"  # untouched
        assert "<plugin_context>" in result[2]["content"]
        assert "current question" in result[2]["content"]

    @pytest.mark.asyncio
    async def test_no_user_message_no_injection(self):
        loop = _make_loop()
        loop.hooks.register("pre_llm_call", lambda ctx: "context")
        # Only system message, no user message anywhere
        messages = [{"role": "system", "content": "rules"}]
        result = await loop._inject_pre_llm_context(
            messages=messages, tools=None, model="test"
        )
        # Nothing to inject into — return unchanged
        assert result == messages

    @pytest.mark.asyncio
    async def test_failing_hook_does_not_break_loop(self):
        loop = _make_loop()
        loop.hooks.register("pre_llm_call", lambda ctx: 1 / 0)
        loop.hooks.register("pre_llm_call", lambda ctx: "survives")
        messages = [{"role": "user", "content": "ask"}]
        result = await loop._inject_pre_llm_context(
            messages=messages, tools=None, model="test"
        )
        # The good hook's return still lands; the failing one is logged + ignored
        assert "survives" in result[0]["content"]

    @pytest.mark.asyncio
    async def test_session_id_propagated_from_tools_registry(self):
        loop = _make_loop()
        loop.tools.set_active_session("web:abc-123")

        captured = {}

        def capture(ctx: LLMHookContext) -> str | None:
            captured["session_id"] = ctx.session_id
            captured["model"] = ctx.model
            captured["user_message"] = ctx.user_message
            return None

        loop.hooks.register("pre_llm_call", capture)
        await loop._inject_pre_llm_context(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            model="claude-haiku-4.5",
        )
        assert captured["session_id"] == "web:abc-123"
        assert captured["model"] == "claude-haiku-4.5"
        assert captured["user_message"] == "hi"

    @pytest.mark.asyncio
    async def test_non_string_user_content_skipped_safely(self):
        # Multimodal user messages can have list content (text + image).
        # We don't try to inject into those — just leave them alone.
        loop = _make_loop()
        loop.hooks.register("pre_llm_call", lambda ctx: "context")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "with image"}]},
        ]
        result = await loop._inject_pre_llm_context(
            messages=messages, tools=None, model="test"
        )
        # Content should be unchanged (still a list)
        assert result[0]["content"] == messages[0]["content"]
