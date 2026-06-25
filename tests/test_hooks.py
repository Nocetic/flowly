"""Tests for HookRegistry — registration, firing, action protocols."""

from __future__ import annotations

import pytest
from typing import Any

from flowly.agent.hooks import (
    BlockAction,
    GatewayDispatchContext,
    HookContext,
    HookRegistry,
    LLMHookContext,
    RewriteAction,
    SessionHookContext,
    SkipAction,
    SubagentStopContext,
    ToolHookContext,
    VALID_EVENTS,
)


# ── Registration ───────────────────────────────────────────────


class TestRegistration:
    def test_register_returns_callable(self):
        reg = HookRegistry()
        fn = lambda ctx: None
        assert reg.register("post_tool_call", fn) is fn

    def test_register_unknown_event_warns(self, caplog):
        reg = HookRegistry()
        reg.register("not_a_real_event", lambda ctx: None)
        assert "unknown hook event" in caplog.text

    def test_decorator_form(self):
        reg = HookRegistry()

        @reg.on("post_tool_call")
        async def handler(ctx):
            pass

        assert handler in reg._hooks["post_tool_call"]

    def test_backward_compat_on_pre_tool(self):
        reg = HookRegistry()

        @reg.on_pre_tool
        def handler(ctx):
            pass

        assert handler in reg._hooks["pre_tool_call"]

    def test_backward_compat_on_post_tool(self):
        reg = HookRegistry()

        @reg.on_post_tool
        def handler(ctx):
            pass

        assert handler in reg._hooks["post_tool_call"]

    def test_valid_events_set_matches_literal(self):
        # Sanity: typing.get_args coverage
        assert "pre_tool_call" in VALID_EVENTS
        assert "pre_gateway_dispatch" in VALID_EVENTS
        assert len(VALID_EVENTS) == 14


# ── Firing — generic ───────────────────────────────────────────


class TestFireGeneric:
    @pytest.mark.asyncio
    async def test_fire_returns_non_none_in_order(self):
        reg = HookRegistry()
        reg.register("post_tool_call", lambda ctx: "first")
        reg.register("post_tool_call", lambda ctx: None)
        reg.register("post_tool_call", lambda ctx: "third")
        results = await reg.fire("post_tool_call", ToolHookContext())
        assert results == ["first", "third"]

    @pytest.mark.asyncio
    async def test_fire_supports_async_callback(self):
        reg = HookRegistry()

        async def async_handler(ctx):
            return "async_result"

        reg.register("post_tool_call", async_handler)
        results = await reg.fire("post_tool_call", ToolHookContext())
        assert results == ["async_result"]

    @pytest.mark.asyncio
    async def test_fire_isolates_callback_errors(self):
        reg = HookRegistry()

        def bad(ctx):
            raise RuntimeError("kaboom")

        reg.register("post_tool_call", bad)
        reg.register("post_tool_call", lambda ctx: "survived")
        results = await reg.fire("post_tool_call", ToolHookContext())
        assert results == ["survived"]

    @pytest.mark.asyncio
    async def test_fire_no_callbacks_returns_empty(self):
        reg = HookRegistry()
        assert await reg.fire("post_tool_call", ToolHookContext()) == []


# ── pre_tool_call block protocol ──────────────────────────────


class TestPreToolBlock:
    @pytest.mark.asyncio
    async def test_no_callbacks_returns_none(self):
        reg = HookRegistry()
        assert await reg.fire_pre_tool(ToolHookContext()) is None

    @pytest.mark.asyncio
    async def test_block_action_returned(self):
        reg = HookRegistry()
        reg.register("pre_tool_call", lambda ctx: BlockAction("nope"))
        result = await reg.fire_pre_tool(ToolHookContext(tool_name="x"))
        assert isinstance(result, BlockAction)
        assert result.message == "nope"

    @pytest.mark.asyncio
    async def test_first_block_wins(self):
        reg = HookRegistry()
        reg.register("pre_tool_call", lambda ctx: None)
        reg.register("pre_tool_call", lambda ctx: BlockAction("first"))
        reg.register("pre_tool_call", lambda ctx: BlockAction("second"))
        result = await reg.fire_pre_tool(ToolHookContext())
        assert result.message == "first"

    @pytest.mark.asyncio
    async def test_non_block_returns_ignored(self):
        reg = HookRegistry()
        reg.register("pre_tool_call", lambda ctx: "string is not a block")
        reg.register("pre_tool_call", lambda ctx: {"action": "block"})  # dict, not BlockAction
        result = await reg.fire_pre_tool(ToolHookContext())
        assert result is None


# ── transform_tool_result ─────────────────────────────────────


class TestTransformToolResult:
    @pytest.mark.asyncio
    async def test_first_string_wins(self):
        reg = HookRegistry()
        reg.register("transform_tool_result", lambda ctx: None)
        reg.register("transform_tool_result", lambda ctx: "rewritten")
        reg.register("transform_tool_result", lambda ctx: "ignored")
        out = await reg.fire_transform_tool_result(ToolHookContext())
        assert out == "rewritten"

    @pytest.mark.asyncio
    async def test_no_string_returns_none(self):
        reg = HookRegistry()
        reg.register("transform_tool_result", lambda ctx: 123)
        reg.register("transform_tool_result", lambda ctx: {"x": 1})
        out = await reg.fire_transform_tool_result(ToolHookContext())
        assert out is None


# ── pre_llm_call context injection ────────────────────────────


class TestPreLLMCall:
    @pytest.mark.asyncio
    async def test_collect_strings(self):
        reg = HookRegistry()
        reg.register("pre_llm_call", lambda ctx: "ctx-a")
        reg.register("pre_llm_call", lambda ctx: "ctx-b")
        contexts = await reg.fire_pre_llm_call(LLMHookContext())
        assert contexts == ["ctx-a", "ctx-b"]

    @pytest.mark.asyncio
    async def test_collect_context_dict(self):
        reg = HookRegistry()
        reg.register("pre_llm_call", lambda ctx: {"context": "from-dict"})
        reg.register("pre_llm_call", lambda ctx: "from-str")
        contexts = await reg.fire_pre_llm_call(LLMHookContext())
        assert contexts == ["from-dict", "from-str"]

    @pytest.mark.asyncio
    async def test_ignores_irrelevant_returns(self):
        reg = HookRegistry()
        reg.register("pre_llm_call", lambda ctx: 42)
        reg.register("pre_llm_call", lambda ctx: {"unrelated": "key"})
        contexts = await reg.fire_pre_llm_call(LLMHookContext())
        assert contexts == []


# ── pre_gateway_dispatch ─────────────────────────────────────


class TestGatewayDispatch:
    @pytest.mark.asyncio
    async def test_skip_action(self):
        reg = HookRegistry()
        reg.register("pre_gateway_dispatch", lambda ctx: SkipAction("spam"))
        result = await reg.fire_gateway_dispatch(GatewayDispatchContext())
        assert isinstance(result, SkipAction)
        assert result.reason == "spam"

    @pytest.mark.asyncio
    async def test_rewrite_action(self):
        reg = HookRegistry()
        reg.register("pre_gateway_dispatch", lambda ctx: RewriteAction("new"))
        result = await reg.fire_gateway_dispatch(GatewayDispatchContext())
        assert isinstance(result, RewriteAction)
        assert result.text == "new"

    @pytest.mark.asyncio
    async def test_first_action_wins(self):
        reg = HookRegistry()
        reg.register("pre_gateway_dispatch", lambda ctx: None)
        reg.register("pre_gateway_dispatch", lambda ctx: SkipAction("first"))
        reg.register("pre_gateway_dispatch", lambda ctx: RewriteAction("loses"))
        result = await reg.fire_gateway_dispatch(GatewayDispatchContext())
        assert isinstance(result, SkipAction)


# ── Session lifecycle ────────────────────────────────────────


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_fire_session_start(self):
        reg = HookRegistry()
        seen: list[str] = []
        reg.register("on_session_start", lambda ctx: seen.append(ctx.session_id))
        await reg.fire_session_start(SessionHookContext(session_id="sess-1"))
        assert seen == ["sess-1"]

    @pytest.mark.asyncio
    async def test_fire_session_end_propagates_completed(self):
        reg = HookRegistry()
        captured: list[bool] = []
        reg.register("on_session_end", lambda ctx: captured.append(ctx.completed))
        await reg.fire_session_end(SessionHookContext(completed=False))
        assert captured == [False]


# ── Context dataclass shapes ─────────────────────────────────


class TestContextDefaults:
    def test_tool_hook_context_defaults(self):
        ctx = ToolHookContext()
        assert ctx.tool_name == ""
        assert ctx.params == {}
        assert ctx.result is None
        assert ctx.duration_ms == 0.0
        assert ctx.success is None

    def test_llm_hook_context_defaults(self):
        ctx = LLMHookContext()
        assert ctx.model == ""
        assert ctx.messages == []
        assert ctx.tools == []
        assert ctx.usage == {}

    def test_session_hook_context_completed_default_true(self):
        ctx = SessionHookContext()
        assert ctx.completed is True
        assert ctx.interrupted is False

    def test_subagent_stop_context_defaults(self):
        ctx = SubagentStopContext()
        assert ctx.subagent_id == ""
        assert ctx.reason == ""
