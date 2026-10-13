"""Subagents belong to the conversation that started them, even on desktop.

A desktop chat is stored under its own key ("web:SGPL…") while replies
travel to a connection id ("web:f7c4…"). Runs were recorded under the
connection, so in a real session the agent could not read or message its own
running subagents ("No subagent run … in this conversation"), their results
were written to a hidden session named after the connection instead of the
chat, and the agent fell back to sleeping, listing tasks and re-spawning.

Also pinned here: ``agent_wait`` — the agent waits for its subagents instead
of polling — and a result or message it took that way is never handled a
second time when the queued notice comes up.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_registry import SubagentRegistry
from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools.agent_tools import AgentWaitTool
from flowly.agent.tools.sessions_list import SessionsListTool
from flowly.bus.events import ConversationInboundMessage
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

CHAT_KEY = "web:SGPLB2bS4uzpHuyXVePh"
CONNECTION = "f7c4ce58-6ddf-47a0-af9d-c7af75bd69c0"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _Provider(LLMProvider):
    """Main agent: spawns, reads, messages; children answer after a moment."""

    def __init__(self) -> None:
        super().__init__(api_key="unused")
        self.main_calls = 0
        self.tool_results: list[str] = []

    def get_default_model(self) -> str:
        return "m"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        messages = kwargs.get("messages") or []
        system = str(messages[0].get("content", "")) if messages else ""
        if "The main agent started you" in system:
            await asyncio.sleep(0.3)
            return LLMResponse(content="Anthropic 2021.")
        self.main_calls += 1
        self.tool_results += [m["content"] for m in messages if m.get("role") == "tool"]
        if self.main_calls == 1:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="s", name="spawn",
                arguments={"title": "Anthropic", "task": "Find the year", "owner_request": "agent çalıştır"},
            )])
        if self.main_calls == 2:
            run_id = json.loads(self.tool_results[-1])["run_id"]
            return LLMResponse(content=None, tool_calls=[
                ToolCallRequest(id="r", name="agent_read", arguments={"run_id": run_id}),
                ToolCallRequest(id="m", name="agent_message",
                                arguments={"to": "all", "message": "Only official sites."}),
            ])
        return LLMResponse(content="Started.")


def _chat_message(content: str) -> ConversationInboundMessage:
    return ConversationInboundMessage(
        channel="web", sender_id=CONNECTION, chat_id=CONNECTION,
        content=content, conversation_key=CHAT_KEY,
    )


@pytest.mark.asyncio
async def test_a_desktop_chat_reaches_its_own_running_subagents(tmp_path):
    provider = _Provider()
    bus = MessageBus()
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, main_config=Config(),
                     max_iterations=5, soft_warn_at_iteration=0)
    try:
        await loop._process_message(_chat_message("Her biri için agent çalıştır"))
        record = loop.subagents.registry.all()[0]
        assert record.parent_session_key == CHAT_KEY
        assert (record.parent_channel, record.parent_chat_id) == ("web", CONNECTION)

        read, sent = provider.tool_results[-2:]
        assert json.loads(read)["status"] == "running"
        assert json.loads(sent)["sent"][0]["status"] == "delivered"

        # The result comes back to the chat itself, not a session named after
        # the connection, and the reply is addressed to the chat.
        notice = await asyncio.wait_for(bus.consume_inbound(), 5)
        assert notice.session_key == CHAT_KEY
        out = await loop._process_message(notice)
        assert out is not None and out.metadata.get("session_key") == CHAT_KEY
        chat = loop.sessions.get_or_create(CHAT_KEY)
        assert any("[System: subagent]" in str(m.get("content")) for m in chat.messages)
        assert not loop.sessions.exists(f"web:{CONNECTION}")
    finally:
        loop.stop()


class _Child(LLMProvider):
    def __init__(self, delay: float = 0.2, ask: bool = False) -> None:
        super().__init__(api_key="unused")
        self.delay = delay
        self.ask = ask
        self.calls = 0

    def get_default_model(self) -> str:
        return "m"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.ask and self.calls == 1:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="q", name="agent_message", arguments={"message": "Which year?"},
            )])
        await asyncio.sleep(self.delay)
        return LLMResponse(content="Result text.")


def _manager(tmp_path, provider, bus):
    return SubagentManager(provider=provider, workspace=tmp_path, bus=bus,
                           registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path)


@pytest.mark.asyncio
async def test_waiting_returns_the_result_and_the_queued_notice_is_not_handled_again(tmp_path):
    bus = MessageBus()
    manager = _manager(tmp_path, _Child(), bus)
    await manager.spawn(task="t", origin_channel="web", origin_chat_id=CONNECTION,
                        parent_session_key=CHAT_KEY, origin="agent", display_name="A")
    with tool_execution_scope(CHAT_KEY):
        outcome = json.loads(await AgentWaitTool(manager).execute(timeout_seconds=5))
    assert [f["result"] for f in outcome["finished"]] == ["Result text."]
    assert outcome["still_running"] == [] and outcome["timed_out"] is False
    notice = await asyncio.wait_for(bus.consume_inbound(), 2)
    assert manager.notice_already_handled(notice.content) is True
    assert manager.pending_for_goal(CHAT_KEY) == []


@pytest.mark.asyncio
async def test_waiting_returns_a_childs_message_once(tmp_path):
    bus = MessageBus()
    manager = _manager(tmp_path, _Child(delay=2.0, ask=True), bus)
    await manager.spawn(task="t", origin_channel="web", origin_chat_id=CONNECTION,
                        parent_session_key=CHAT_KEY, origin="agent", display_name="A")
    with tool_execution_scope(CHAT_KEY):
        outcome = json.loads(await AgentWaitTool(manager).execute(timeout_seconds=5))
    assert outcome["messages"][0]["text"] == "Which year?"
    assert outcome["finished"] == [] and outcome["still_running"] == ["A"]
    notice = await asyncio.wait_for(bus.consume_inbound(), 2)
    assert "message_id=" in notice.content
    assert manager.notice_already_handled(notice.content) is True
    manager.cancel_all()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_waiting_stops_when_the_owner_writes(tmp_path):
    from flowly.agent.delegation import DelegationTurn, delegation_turn_scope

    manager = _manager(tmp_path, _Child(delay=5.0), MessageBus())
    await manager.spawn(task="t", origin_channel="web", origin_chat_id=CONNECTION,
                        parent_session_key=CHAT_KEY, origin="agent", display_name="A")
    owner_wrote = {"now": False}
    turn = DelegationTurn(session_key=CHAT_KEY, should_yield=lambda: owner_wrote["now"])

    async def write_soon():
        await asyncio.sleep(0.2)
        owner_wrote["now"] = True

    asyncio.create_task(write_soon())
    with tool_execution_scope(CHAT_KEY), delegation_turn_scope(turn):
        started = asyncio.get_running_loop().time()
        outcome = json.loads(await AgentWaitTool(manager).execute(timeout_seconds=30))
    assert outcome["interrupted"] is True
    assert asyncio.get_running_loop().time() - started < 3
    manager.cancel_all()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_listing_tasks_shows_only_this_conversations(tmp_path):
    bus = MessageBus()
    manager = _manager(tmp_path, _Child(delay=2.0), bus)
    await manager.spawn(task="mine", origin_channel="web", origin_chat_id=CONNECTION,
                        parent_session_key=CHAT_KEY, origin="agent", display_name="Mine")
    await manager.spawn(task="theirs", origin_channel="web", origin_chat_id="other",
                        origin="agent", display_name="Theirs")
    tool = SessionsListTool(registry=manager.registry, manager=manager)
    with tool_execution_scope(CHAT_KEY):
        listing = await tool.execute(action="list")
    assert "Mine" in listing or "mine" in listing
    assert "Theirs" not in listing and "theirs" not in listing
    manager.cancel_all()
    await asyncio.sleep(0.05)
