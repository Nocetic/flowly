"""Tests for pre_gateway_dispatch hook firing in AgentLoop._process_message.

The hook gives plugins a chance to drop (SkipAction) or rewrite
(RewriteAction) an inbound message before any session lifecycle work or
LLM dispatch happens. We exercise the firing site directly with a
minimal AgentLoop shell — no real provider, no real session store.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from flowly.agent.hooks import (
    HookRegistry,
    SkipAction,
    RewriteAction,
    GatewayDispatchContext,
)
from flowly.agent.tools.registry import ToolRegistry
from flowly.bus.events import InboundMessage


def _make_loop():
    """Build a minimal AgentLoop with just enough wiring for _process_message.

    We bypass __init__ because it requires a real LLM provider; instead
    we attach the bare minimum the hook firing path touches.
    """
    from flowly.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.hooks = HookRegistry()
    loop.tools = ToolRegistry()
    loop.sessions = MagicMock()
    loop.subagents = MagicMock()
    loop.subagents.mark_busy = MagicMock()
    loop.subagents.mark_idle = MagicMock()
    loop.model = "test-model"
    loop._started_sessions = set()
    # Inner processor is what runs after the hook — stub it so we can
    # verify whether it was reached.
    loop._process_message_inner = AsyncMock(return_value=None)
    loop._process_system_message = AsyncMock(return_value=None)
    return loop


def _msg(content: str = "hello", channel: str = "telegram") -> InboundMessage:
    return InboundMessage(
        channel=channel,
        sender_id="user1",
        chat_id="chat1",
        content=content,
    )


class TestPreGatewayDispatch:
    @pytest.mark.asyncio
    async def test_no_plugins_passes_through(self):
        loop = _make_loop()
        msg = _msg("hello world")
        await loop._process_message(msg)
        # Inner processor reached, message untouched
        loop._process_message_inner.assert_awaited_once()
        called_msg = loop._process_message_inner.call_args[0][0]
        assert called_msg.content == "hello world"

    @pytest.mark.asyncio
    async def test_skip_action_drops_message(self):
        loop = _make_loop()

        def spam_filter(ctx):
            if "bitcoin" in ctx.event.content.lower():
                return SkipAction(reason="spam")
            return None

        loop.hooks.register("pre_gateway_dispatch", spam_filter)
        msg = _msg("free bitcoin click here")
        result = await loop._process_message(msg)
        assert result is None
        # Inner processor never called — message dropped
        loop._process_message_inner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rewrite_action_replaces_content(self):
        loop = _make_loop()

        def pii_redactor(ctx):
            text = ctx.event.content
            if "0532" in text:
                return RewriteAction(text=text.replace("0532", "[REDACTED]"))
            return None

        loop.hooks.register("pre_gateway_dispatch", pii_redactor)
        msg = _msg("call me at 0532-123-4567")
        await loop._process_message(msg)
        loop._process_message_inner.assert_awaited_once()
        called_msg = loop._process_message_inner.call_args[0][0]
        assert "[REDACTED]" in called_msg.content
        assert "0532" not in called_msg.content

    @pytest.mark.asyncio
    async def test_failing_hook_does_not_break_dispatch(self):
        loop = _make_loop()

        def broken(ctx):
            raise RuntimeError("plugin bug")

        loop.hooks.register("pre_gateway_dispatch", broken)
        msg = _msg("hi")
        await loop._process_message(msg)
        # HookRegistry catches the exception; processing continues
        loop._process_message_inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_first_skip_or_rewrite_wins(self):
        """fire_gateway_dispatch returns the first Skip/Rewrite — later
        plugins don't override an earlier decision."""
        loop = _make_loop()

        loop.hooks.register(
            "pre_gateway_dispatch",
            lambda ctx: RewriteAction(text="first"),
        )
        loop.hooks.register(
            "pre_gateway_dispatch",
            lambda ctx: RewriteAction(text="second"),
        )
        msg = _msg("original")
        await loop._process_message(msg)
        called_msg = loop._process_message_inner.call_args[0][0]
        assert called_msg.content == "first"

    @pytest.mark.asyncio
    async def test_system_channel_bypasses_hook(self):
        """system messages are subagent announces — hooks shouldn't see them."""
        loop = _make_loop()
        seen = []

        def observer(ctx):
            seen.append(ctx.event.content)
            return None

        loop.hooks.register("pre_gateway_dispatch", observer)
        msg = _msg("subagent done", channel="system")
        await loop._process_message(msg)
        # _process_system_message was hit, gateway hook was not
        loop._process_system_message.assert_awaited_once()
        assert seen == []

    @pytest.mark.asyncio
    async def test_context_carries_gateway_name(self):
        loop = _make_loop()
        captured = []

        def capture(ctx):
            captured.append((ctx.gateway, ctx.session_id, ctx.event))

        loop.hooks.register("pre_gateway_dispatch", capture)
        msg = _msg("hi", channel="telegram")
        await loop._process_message(msg)
        assert len(captured) == 1
        gateway, session_id, event = captured[0]
        assert gateway == "telegram"
        assert session_id == "telegram:chat1"
        assert event is msg

    @pytest.mark.asyncio
    async def test_skip_does_not_fire_session_start(self):
        """If the message is dropped, on_session_start should not fire —
        the session never actually 'started'."""
        loop = _make_loop()
        loop.hooks.register(
            "pre_gateway_dispatch",
            lambda ctx: SkipAction(reason="rate-limit"),
        )
        session_starts = []
        loop.hooks.register(
            "on_session_start", lambda ctx: session_starts.append(ctx.session_id)
        )
        msg = _msg("hi")
        await loop._process_message(msg)
        assert session_starts == []
