"""A turn that keeps re-reading what it already has ends with an answer.

In a real session the agent re-read three finished subagent results about
35 times in a row — identical results every time, no errors, so the only
check the loop had (an error-rate checkpoint) told it to keep going — until
the owner pressed stop.
"""

from __future__ import annotations

from typing import Any

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.repetition import STALL_AFTER, RepetitionGuard
from flowly.agent.tools.base import Tool
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _ReadResult(Tool):
    """Stands in for agent_read on finished runs: the same text every time."""

    @property
    def name(self) -> str:
        return "read_result"

    @property
    def description(self) -> str:
        return "Read a finished result."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"run": {"type": "string"}}}

    async def execute(self, run: str = "", **kwargs: Any) -> str:
        return f"Result of {run}: founded 2021."


class _Circling(LLMProvider):
    """Reads runs a, b, c in turn for as long as it is offered tools."""

    def __init__(self) -> None:
        super().__init__(api_key="unused")
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "m"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        if not kwargs.get("tools"):
            return LLMResponse(content="Anthropic 2021, Mistral 2023, Cohere 2019.")
        run = "abc"[(len(self.calls) - 1) % 3]
        return LLMResponse(content=None, tool_calls=[
            ToolCallRequest(id=f"c{len(self.calls)}", name="read_result", arguments={"run": run}),
        ])


@pytest.mark.asyncio
async def test_a_turn_re_reading_the_same_results_answers_instead(tmp_path):
    provider = _Circling()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     main_config=Config(), max_iterations=40, soft_warn_at_iteration=0)
    loop.tools.register(_ReadResult(), toolset="test")
    try:
        final, *_ = await loop._run_llm_tool_loop(
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "go"}],
            turn_content="go", session_key="web:r", tool_platform="web",
        )
    finally:
        loop.stop()
    assert final == "Anthropic 2021, Mistral 2023, Cohere 2019."
    # Three first reads, then STALL_AFTER repeats, then one call without tools.
    assert len(provider.calls) == 3 + STALL_AFTER + 1
    last = provider.calls[-1]
    assert not last.get("tools")
    assert "answer the owner now" in str(last["messages"][-1]["content"])
    repeated = [m for m in last["messages"] if m.get("role") == "tool"][3:]
    assert all(str(m["content"]).startswith("[Identical to an earlier read_result") for m in repeated)


def test_new_information_resets_the_count():
    guard = RepetitionGuard(stall_after=2)
    assert guard.observe("t", "x") is False
    assert guard.observe("t", "x") is True
    assert guard.observe("t", "y") is False  # something new arrived
    assert guard.observe("t", "x") is True and not guard.stalled
    assert guard.observe("t", "y") is True and guard.stalled


def test_the_same_text_from_different_tools_is_not_a_repeat():
    guard = RepetitionGuard()
    assert guard.observe("process", "ok") is False
    assert guard.observe("write_file", "ok") is False
