"""A step whose only calls are the agent's own bookkeeping is still a step.

Calls to memory, goals and the like are hidden from the live tool panel. A
step that said something and then only made such calls reached clients as a
plain assistant message without ``tool_calls``, so they did not move the text
out of the live bubble: it showed twice, once in the bubble and once in the
work log (seen live on a relay-connected desktop), and a client re-entering
mid-turn got it back glued to the next step's text.
"""

from __future__ import annotations

from typing import Any

import pytest

from flowly.agent import inflight
from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _FollowUp(Tool):
    @property
    def name(self) -> str:
        return "follow_up"

    @property
    def description(self) -> str:
        return "Plan a follow-up."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "planned"


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="unused")
        self.calls = 0

    def get_default_model(self) -> str:
        return "m"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="Checking what I already know.", tool_calls=[
                ToolCallRequest(id="f1", name="follow_up", arguments={}),
            ])
        return LLMResponse(content="Done.")


@pytest.mark.asyncio
async def test_a_step_with_only_hidden_calls_reaches_clients_as_a_boundary(tmp_path):
    loop = AgentLoop(bus=MessageBus(), provider=_Provider(), workspace=tmp_path,
                     main_config=Config(), max_iterations=4, soft_warn_at_iteration=0)
    loop.tools.register(_FollowUp(), toolset="agenda")
    events: list[dict[str, Any]] = []

    async def on_iteration(event: dict[str, Any]) -> None:
        events.append(event)

    try:
        await loop._run_llm_tool_loop(
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "go"}],
            turn_content="go", session_key="web:b", tool_platform="web",
            outbound_run_id="run-b", on_iteration=on_iteration,
        )
    finally:
        loop.stop()
    steps = [e for e in events if e["role"] == "assistant"]
    assert steps[0]["content"] == "Checking what I already know."
    # The hidden call is not shown, but the step says it was a step.
    assert steps[0]["tool_calls"] == []
    # Its result never reaches the client either.
    assert not [e for e in events if e["role"] == "tool"]


def test_re_entry_does_not_glue_a_hidden_steps_text_to_the_next():
    key = "desktop:boundary"
    inflight.begin(key, "run-1", "go")
    inflight.append(key, "run-1", "Checking what I already know.")
    inflight.append_iteration(key, "run-1", {
        "runId": "run-1", "iterationIdx": 0, "role": "assistant",
        "content": "Checking what I already know.", "tool_calls": [],
    })
    inflight.append(key, "run-1", "Here is the answer")
    assert inflight.get(key)["text"] == "Here is the answer"
    # A plain assistant message (no tool_calls at all) is not a boundary.
    inflight.append_iteration(key, "run-1", {
        "runId": "run-1", "iterationIdx": 1, "role": "assistant", "content": "x",
    })
    assert inflight.get(key)["text"] == "Here is the answer"
    inflight.finish(key, "run-1")
