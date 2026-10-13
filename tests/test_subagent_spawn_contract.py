"""A subagent's whole life as the agent sees it: start, context, result, read.

* the loop tells the spawn tool how many spawn calls a response holds, so a
  lone unrequested spawn is refused while a split into parts starts;
* a started run carries its full id, origin and mark, and the child reads
  the conversation before its brief;
* the completion message names the run and hands over a readable result,
  and ``agent_read`` pages through the rest — for this conversation only.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from flowly.agent.delegation import DelegationTurn, delegation_turn_scope
from flowly.agent.loop import AgentLoop
from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_observation import run_view
from flowly.agent.subagent_registry import SubagentRegistry
from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools.agent_tools import AgentReadTool
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _ChildProvider(LLMProvider):
    """What every subagent talks to: records the first message it was given."""

    def __init__(self, answer: str = "Child result.") -> None:
        super().__init__(api_key="unused")
        self.answer = answer
        self.first_messages: list[str] = []

    def get_default_model(self) -> str:
        return "fake/model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        messages = kwargs.get("messages") or []
        if len(messages) >= 2:
            self.first_messages.append(str(messages[1].get("content")))
        return LLMResponse(content=self.answer)


class _ParentProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key="unused")
        self.responses = list(responses)
        self.tool_messages: list[str] = []

    def get_default_model(self) -> str:
        return "parent/model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        for message in kwargs.get("messages") or []:
            if message.get("role") == "tool" and message.get("name") == "spawn":
                self.tool_messages.append(str(message.get("content")))
        return self.responses.pop(0)


def _spawn_call(call_id: str, **args: Any) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="spawn", arguments=args)


def _loop(tmp_path, parent: LLMProvider, child: LLMProvider) -> AgentLoop:
    loop = AgentLoop(
        bus=MessageBus(), provider=parent, workspace=tmp_path,
        main_config=Config(), max_iterations=4, soft_warn_at_iteration=0,
    )
    loop.subagents.provider = child
    return loop


async def _turn(loop: AgentLoop, owner_text: str) -> None:
    turn = DelegationTurn(
        owner_text=owner_text,
        conversation=(("user", owner_text),),
        model="parent/model",
    )
    loop.tools.get("spawn").set_context("web", "conv-1")
    with tool_execution_scope("web:conv-1"), delegation_turn_scope(turn):
        await loop._run_llm_tool_loop(
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": owner_text}],
            turn_content=owner_text, session_key="web:conv-1", tool_platform="web",
        )


async def _settle(loop: AgentLoop) -> None:
    for _ in range(100):
        if not loop.subagents._running_tasks:
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_lone_unrequested_spawn_is_refused_in_the_loop(tmp_path):
    parent = _ParentProvider([
        LLMResponse(content=None, tool_calls=[_spawn_call("s1", title="Araştırma", task="Research X")]),
        LLMResponse(content="Kendim araştırdım."),
    ])
    loop = _loop(tmp_path, parent, _ChildProvider())
    try:
        await _turn(loop, "X'i detaylı araştır.")
    finally:
        await _settle(loop)
        loop.stop()
    assert json.loads(parent.tool_messages[0])["status"] == "refused"
    assert loop.subagents.registry.all() == []


@pytest.mark.asyncio
async def test_independent_parts_in_one_response_both_start(tmp_path):
    parent = _ParentProvider([
        LLMResponse(content=None, tool_calls=[
            _spawn_call("s1", title="Rakip A", task="Research competitor A"),
            _spawn_call("s2", title="Rakip B", task="Research competitor B"),
        ]),
        LLMResponse(content="İkisini başlattım."),
    ])
    child = _ChildProvider()
    loop = _loop(tmp_path, parent, child)
    try:
        await _turn(loop, "A ve B rakiplerini ayrı ayrı araştır.")
        await _settle(loop)
    finally:
        loop.stop()
    started = [json.loads(m) for m in parent.tool_messages]
    assert [s["status"] for s in started] == ["started", "started"]
    assert all(len(s["run_id"]) == 36 for s in started)
    records = loop.subagents.registry.all()
    assert {r.origin for r in records} == {"agent"}
    assert len({r.mark["glyph"] for r in records}) == 2
    # The child read the conversation before its brief.
    assert any("A ve B rakiplerini ayrı ayrı araştır." in m and "## Your task" in m
               for m in child.first_messages)


@pytest.mark.asyncio
async def test_an_owner_request_starts_one_subagent_on_the_named_model(tmp_path):
    parent = _ParentProvider([
        LLMResponse(content=None, tool_calls=[_spawn_call(
            "s1", title="Composer hatası", task="Fix the double submit",
            owner_request="Luna'ya ver", model="gpt-6-luna",
        )]),
        LLMResponse(content="Luna'ya verdim."),
    ])
    loop = _loop(tmp_path, parent, _ChildProvider())
    try:
        await _turn(loop, "Composer hatasını Luna'ya ver.")
        await _settle(loop)
    finally:
        loop.stop()
    started = json.loads(parent.tool_messages[0])
    assert started["status"] == "started" and started["model"] == "gpt-6-luna"
    record = loop.subagents.registry.all()[0]
    view = run_view(record)
    assert view["origin"] == "owner"
    assert view["label"] == "Composer hatası"
    assert view["childSessionKey"] == f"subagent:{record.run_id}"
    assert set(view["mark"]) == {"glyph", "hue"}


@pytest.mark.asyncio
async def test_the_completion_message_names_the_run_and_hands_over_the_result(tmp_path):
    long_result = "Bulgu. " * 3000  # longer than one notice
    bus = MessageBus()
    manager = SubagentManager(
        provider=_ChildProvider(long_result), workspace=tmp_path, bus=bus,
        registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path,
    )
    started = json.loads(await manager.spawn(
        task="Research", origin_channel="web", origin_chat_id="conv-1",
        origin="agent", display_name="Araştırma",
    ))
    notice = await asyncio.wait_for(bus.consume_inbound(), 5)
    run_id = started["run_id"]
    assert f"run_id={run_id}" in notice.content
    assert "Araştırma" in notice.content
    assert f'agent_read(run_id="{run_id}", offset=8000)' in notice.content
    assert notice.sender_id == "subagent"

    reader = AgentReadTool(manager.registry)
    with tool_execution_scope("web:conv-1"):
        page = json.loads(await reader.execute(run_id=run_id, offset=8000, limit=500))
    assert page["status"] == "ok"
    assert page["content"] == long_result[8000:8500]
    assert page["total_chars"] == len(long_result)

    with tool_execution_scope("web:someone-else"):
        hidden = json.loads(await reader.execute(run_id=run_id))
    assert "No subagent run" in hidden["error"]


@pytest.mark.asyncio
async def test_a_goal_waits_until_results_are_handled_not_just_finished(tmp_path):
    bus = MessageBus()
    manager = SubagentManager(
        provider=_ChildProvider("done"), workspace=tmp_path, bus=bus,
        registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path,
    )
    started = json.loads(await manager.spawn(
        task="Part A", origin_channel="web", origin_chat_id="g1",
        origin="agent", display_name="Rakip A",
    ))
    await manager.spawn("Self review", origin_channel="web", origin_chat_id="g1",
                        label="self-review", silent=True)
    notice = await asyncio.wait_for(bus.consume_inbound(), 5)
    await asyncio.sleep(0.05)
    # Finished and announced, but the conversation has not handled it yet.
    assert manager.pending_for_goal("web:g1") == ["Rakip A"]
    manager.mark_delivered([started["run_id"]])
    assert manager.pending_for_goal("web:g1") == []
    assert started["run_id"] in notice.content
