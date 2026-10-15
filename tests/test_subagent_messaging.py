"""The main agent and its running subagents can talk while they work.

* the main agent's message reaches a running child at its next step — it
  never interrupts a tool call in progress — and shows in the transcript;
* a child can tell or ask the main agent something; the message reaches the
  conversation that started it and the child can wait for the answer;
* a subagent with no task running keeps a message for its next task
  (``queued``, no task starts); a closed one takes none; a conversation
  cannot reach another conversation's subagents.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_registry import SubagentRegistry
from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools.agent_tools import AgentMessageTool
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _SteerableProvider(LLMProvider):
    """Calls a slow tool first, then answers with whatever it was told."""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__(api_key="unused")
        self.gate = gate
        self.calls = 0

    def get_default_model(self) -> str:
        return "fake/model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content=None, tool_calls=[
                ToolCallRequest(id="w1", name="web_fetch", arguments={"url": "https://example.com"}),
            ])
        notes = [m["content"] for m in kwargs["messages"]
                 if m.get("role") == "user" and "[Message from the main agent]" in str(m.get("content"))]
        return LLMResponse(content="Heard: " + " | ".join(notes))


class _AskingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="unused")
        self.calls = 0
        self.tool_results: list[str] = []

    def get_default_model(self) -> str:
        return "fake/model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="q1", name="agent_message",
                arguments={"message": "Which year should I use?", "await_reply": True},
            )])
        self.tool_results = [m["content"] for m in kwargs["messages"] if m.get("role") == "tool"]
        return LLMResponse(content="Used the answer.")


def _manager(tmp_path, provider: LLMProvider, bus: MessageBus | None = None) -> SubagentManager:
    return SubagentManager(
        provider=provider, workspace=tmp_path, bus=bus or MessageBus(),
        registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_a_message_reaches_a_running_child_at_its_next_step(tmp_path, monkeypatch):
    gate = asyncio.Event()

    async def slow_fetch(self, **kwargs):
        await gate.wait()
        return "page"

    monkeypatch.setattr("flowly.agent.tools.web.WebFetchTool.execute", slow_fetch)
    bus = MessageBus()
    manager = _manager(tmp_path, _SteerableProvider(gate), bus)
    started = json.loads(await manager.spawn(
        task="Read the page", origin_channel="web", origin_chat_id="c1", origin="agent",
    ))
    run_id = started["run_id"]
    await asyncio.sleep(0.05)  # the child is now inside its tool call

    tool = AgentMessageTool(manager)
    with tool_execution_scope("web:c1"):
        sent = json.loads(await tool.execute(to=run_id, message="Only 2025 prices."))
    assert sent["sent"] == [{"run_id": run_id, "status": "delivered"}]

    gate.set()
    notice = await asyncio.wait_for(bus.consume_inbound(), 5)
    assert "Heard: [Message from the main agent]\nOnly 2025 prices." in notice.content
    kinds = [i["kind"] for i in manager.registry.read_transcript(run_id)["items"]]
    assert "message_in" in kinds
    # The message waited for the tool call to finish instead of cutting it.
    assert kinds.index("message_in") < kinds.index("result")


@pytest.mark.asyncio
async def test_a_child_asks_and_waits_for_the_main_agent(tmp_path):
    bus = MessageBus()
    provider = _AskingProvider()
    manager = _manager(tmp_path, provider, bus)
    started = json.loads(await manager.spawn(
        task="Compare prices", origin_channel="web", origin_chat_id="c1",
        origin="owner", display_name="Fiyatlar",
    ))
    run_id = started["run_id"]

    question = await asyncio.wait_for(bus.consume_inbound(), 5)
    assert question.sender_id == "subagent" and question.chat_id == "web:c1"
    assert "Fiyatlar" in question.content and "Which year should I use?" in question.content
    assert f'agent_message(to="{run_id}"' in question.content

    tool = AgentMessageTool(manager)
    with tool_execution_scope("web:c1"):
        await tool.execute(to=run_id, message="Use 2025.")
    done = await asyncio.wait_for(bus.consume_inbound(), 5)
    assert "Used the answer." in done.content
    assert any('"reply": "Use 2025."' in r for r in provider.tool_results)
    kinds = [i["kind"] for i in manager.registry.read_transcript(run_id)["items"]]
    assert kinds.count("message_out") == 1 and kinds.count("message_in") == 1


@pytest.mark.asyncio
async def test_idle_closed_or_foreign_subagents(tmp_path):
    class _Quick(LLMProvider):
        def get_default_model(self):
            return "fake/model"

        async def chat(self, **kwargs):
            return LLMResponse(content="done")

    manager = _manager(tmp_path, _Quick(api_key="x"))
    await manager.spawn("Quick task", origin_channel="web", origin_chat_id="c1",
                        origin="agent", wait=True)
    run_id = manager.registry.all()[0].run_id

    tool = AgentMessageTool(manager)
    with tool_execution_scope("web:c1"):
        idle = json.loads(await tool.execute(to=run_id, message="hello?"))
    # Its task ended: the message waits for its next task; none starts.
    assert idle["sent"][0]["status"] == "queued" and idle["sent"][0]["agent_id"] == run_id
    assert manager.registry.get_thread(run_id).pending_mail[0]["text"] == "hello?"
    assert len(manager.registry.all()) == 1 and not manager._running_tasks

    await manager.close_threads([run_id], parent_session_key="web:c1")
    with tool_execution_scope("web:c1"):
        closed = json.loads(await tool.execute(to=run_id, message="still there?"))
    assert closed["sent"][0]["status"] == "closed"
    assert "was closed with agent_close; it takes no new task or message" in closed["sent"][0]["error"]
    assert manager.registry.get_thread(run_id).pending_mail == []

    with tool_execution_scope("web:other"):
        foreign = json.loads(await tool.execute(to=run_id, message="hello?"))
    assert "No subagent run" in foreign["error"]

    with tool_execution_scope("web:c1"):
        nobody = json.loads(await tool.execute(to="all", message="status?"))
    assert "No subagent of this conversation is running" in nobody["error"]
