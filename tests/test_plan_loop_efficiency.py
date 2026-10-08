"""End to end through the agent loop: a plan tick rides along with real work
in one model response, and a plan the turn created is never left at 0/N."""

from __future__ import annotations

from typing import Any

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.agent.tools.plan import PlanTool
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.plans.manager import get_plan_manager, reset_plan_manager_singleton
from flowly.plans.store import reset_plan_store_singleton
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _Work(Tool):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "work"

    @property
    def description(self) -> str:
        return "Does one unit of work."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        return "done"


class _Scripted(LLMProvider):
    def __init__(self, script: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self.script = script
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return self.script[len(self.calls) - 1]


def _call(i: int, name: str, **args: Any) -> ToolCallRequest:
    return ToolCallRequest(id=f"c{i}", name=name, arguments=args)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(PlanTool, "_runs_unattended", lambda self: True)  # YOLO
    reset_plan_store_singleton()
    reset_plan_manager_singleton()
    yield tmp_path
    reset_plan_manager_singleton()
    reset_plan_store_singleton()


def _loop(tmp_path, provider) -> tuple[AgentLoop, _Work]:
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     main_config=Config(), max_iterations=8, soft_warn_at_iteration=0)
    work = _Work()
    loop.tools.register(work, toolset="extensions")
    return loop, work


async def test_ticks_ride_along_with_real_calls(home):
    steps = [{"id": 1, "content": "A"}, {"id": 2, "content": "B"}]
    provider = _Scripted([
        LLMResponse(content=None, tool_calls=[
            _call(1, "plan", action="propose", goal="g", steps=steps), _call(2, "work")]),
        LLMResponse(content=None, tool_calls=[
            _call(3, "plan", action="update", steps=[{"id": 1, "status": "completed"}]),
            _call(4, "work")]),
        LLMResponse(content=None, tool_calls=[
            _call(5, "plan", action="update", steps=[{"id": 2, "status": "completed"}])]),
        LLMResponse(content="All done."),
    ])
    loop, work = _loop(home, provider)
    try:
        result = await loop.process_direct("do A then B", session_key="web:p")
    finally:
        loop.stop()
    assert result == "All done."
    assert work.calls == 2
    plan = get_plan_manager().list_for_session("web:p")[0]
    assert plan.status == "completed"           # no explicit complete() needed
    assert len(provider.calls) == 4             # no standalone in_progress ticks


async def test_a_plan_this_turn_left_unticked_gets_one_nudge(home):
    steps = [{"id": 1, "content": "A"}, {"id": 2, "content": "B"}]
    provider = _Scripted([
        LLMResponse(content=None, tool_calls=[
            _call(1, "plan", action="propose", goal="g", steps=steps), _call(2, "work")]),
        LLMResponse(content="Finished."),        # forgot to tick
        LLMResponse(content=None, tool_calls=[_call(3, "plan", action="update", steps=[
            {"id": 1, "status": "completed"}, {"id": 2, "status": "completed"}])]),
        LLMResponse(content="Finished."),
    ])
    loop, _ = _loop(home, provider)
    try:
        await loop.process_direct("do A then B", session_key="web:q")
    finally:
        loop.stop()
    nudge = str(provider.calls[2]["messages"])
    assert "Unfinished steps" in nudge and "action='update'" in nudge
    assert get_plan_manager().list_for_session("web:q")[0].status == "completed"
    assert len(provider.calls) == 4
