"""A turn that runs out of steps tells the person so, in plain text.

The loop stops at ``max_iterations`` while the model still wants tools. The
reply after that is one more model call: it carries the turn's tools with
``tool_choice="none"`` and says the limit was reached. A model that answers it
with a tool call written as text (deepseek's DSML) has not summarized
anything, so the person gets the step-limit reply instead of the markup.
"""

from __future__ import annotations

from typing import Any

import pytest

from flowly.agent.loop import STEP_LIMIT_REPLIES, AgentLoop
from flowly.agent.tools.base import Tool
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

from tests.test_leaked_tool_markup import STEP_LIMIT_LEAK


class _ProbeTool(Tool):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "probe"

    @property
    def description(self) -> str:
        return "Reads one more part."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        return f"part {self.calls}"


class _NeverDoneProvider(LLMProvider):
    """Asks for one more tool on every call; the reply call gets ``final``."""

    def __init__(self, final: str) -> None:
        super().__init__(api_key="test")
        self.final = final
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        if kwargs.get("tool_choice") == "none":
            return LLMResponse(content=self.final)
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(id=f"c{len(self.calls)}", name="probe", arguments={})],
        )


async def _run_out_of_steps(tmp_path, monkeypatch, final: str, history=None, turn="Read every part and summarize it."):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    provider = _NeverDoneProvider(final)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=2,
        soft_warn_at_iteration=0,
    )
    probe = _ProbeTool()
    loop.tools.register(probe, toolset="test")
    try:
        final_content, *_ = await loop._run_llm_tool_loop(
            messages=[
                {"role": "system", "content": "test"},
                *(history or []),
                {"role": "user", "content": turn},
            ],
            turn_content=turn,
            session_key="web:step-limit",
            tool_platform="web",
        )
    finally:
        loop.stop()
    return final_content, provider, probe


@pytest.mark.asyncio
async def test_reply_call_keeps_the_tools_and_says_the_limit_was_reached(tmp_path, monkeypatch):
    final, provider, probe = await _run_out_of_steps(
        tmp_path, monkeypatch, "I read 2 parts; the rest is left. Say continue."
    )

    assert final == "I read 2 parts; the rest is left. Say continue."
    assert probe.calls == 2
    reply_call = provider.calls[-1]
    assert reply_call["tool_choice"] == "none"
    assert reply_call["tools"] == provider.calls[-2]["tools"]
    assert "probe" in [t["function"]["name"] for t in reply_call["tools"]]
    assert "limit of 2 steps" in reply_call["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_tool_markup_in_the_reply_becomes_the_step_limit_reply(tmp_path, monkeypatch):
    final, _provider, probe = await _run_out_of_steps(tmp_path, monkeypatch, STEP_LIMIT_LEAK)

    assert final == STEP_LIMIT_REPLIES["en"].format(limit=2)
    assert "DSML" not in final
    assert probe.calls == 2


@pytest.mark.asyncio
async def test_step_limit_reply_speaks_the_persons_language(tmp_path, monkeypatch):
    # "devam et" is too short to tell; the message before it is Turkish.
    final, _provider, _probe = await _run_out_of_steps(
        tmp_path,
        monkeypatch,
        STEP_LIMIT_LEAK,
        history=[
            {"role": "user", "content": "Neler var baksana, bana sayfa sayfa basitçe özetler misin?"},
            {"role": "assistant", "content": "Okuyorum."},
        ],
        turn="devam et",
    )

    assert final == STEP_LIMIT_REPLIES["tr"].format(limit=2)
