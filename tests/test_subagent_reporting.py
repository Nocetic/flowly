"""How subagents report back without keeping the conversation busy.

In a real session the agent started three subagents and then sat in
``agent_wait`` for minutes: the chat stayed busy, the owner's message waited
in a queue, and once the results arrived the agent re-read them until the
owner stopped it. Now:

* subagents started in the same turn report together, in one notice, once
  the last of them has finished — the agent ends its turn after starting
  them and answers the request as a whole when that notice arrives;
* ``agent_wait`` is short and has a per-turn budget;
* a turn a subagent's report started stops waiting when the owner writes,
  and does not hold up other conversations while it runs;
* a plan does not push the agent to keep going while its subagents work;
* a subagent that keeps getting the same results wraps up with a summary;
* a search backend whose SDK is missing is not offered as working.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from flowly.agent.delegation import DelegationTurn, delegation_turn_scope
from flowly.agent import subagent_owner
from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_registry import SubagentRegistry
from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools import agent_tools
from flowly.agent.tools.agent_tools import AgentWaitTool
from flowly.bus.events import ConversationInboundMessage
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

CHAT = "web:chat-1"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _Children(LLMProvider):
    """Each child answers after the delay named in its brief ("delay=0.3")."""

    def __init__(self) -> None:
        super().__init__(api_key="unused")
        self.gates: dict[str, asyncio.Event] = {}

    def get_default_model(self) -> str:
        return "m"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        brief = str(kwargs["messages"][1]["content"])
        for name, gate in self.gates.items():
            if f"task {name}" in brief:
                await gate.wait()
                return LLMResponse(content=f"Result of {name}.")
        return LLMResponse(content="Result.")


def _manager(tmp_path, provider: LLMProvider, bus: MessageBus) -> SubagentManager:
    return SubagentManager(provider=provider, workspace=tmp_path, bus=bus,
                           registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path)


async def _start(manager: SubagentManager, name: str, batch: str) -> str:
    started = json.loads(await manager.spawn(
        task=f"task {name}", origin_channel="web", origin_chat_id="conn",
        parent_session_key=CHAT, origin="agent", display_name=name, batch_id=batch,
    ))
    return started["run_id"]


async def _no_notice(bus: MessageBus, seconds: float = 0.3) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.consume_inbound(), seconds)


@pytest.mark.asyncio
async def test_parts_started_together_report_together_once_all_have_finished(tmp_path):
    bus, children = MessageBus(), _Children()
    children.gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    manager = _manager(tmp_path, children, bus)
    a = await _start(manager, "A", "turn-1")
    b = await _start(manager, "B", "turn-1")

    children.gates["A"].set()
    await _no_notice(bus)  # A is done, B is not: A's result is held
    assert manager.registry.get(a).delivery_state == "pending"
    assert manager.pending_for_goal(CHAT) == ["A", "B"]

    children.gates["B"].set()
    notice = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert isinstance(notice, ConversationInboundMessage) and notice.session_key == CHAT
    assert "Result of A." in notice.content and "Result of B." in notice.content
    assert f"run_id={a}" in notice.content and f"run_id={b}" in notice.content
    assert "started together and have all finished" in notice.content
    await _no_notice(bus)  # exactly one notice

    # The notice is handled once both of its runs are.
    assert manager.notice_already_handled(notice.content) is False
    manager.mark_delivered(CHAT, [a])
    assert manager.notice_already_handled(notice.content) is False
    manager.mark_delivered(CHAT, [b])
    assert manager.notice_already_handled(notice.content) is True


@pytest.mark.asyncio
async def test_parts_finishing_at_the_same_moment_send_one_notice(tmp_path):
    bus, children = MessageBus(), _Children()
    gate = asyncio.Event()
    children.gates = {"A": gate, "B": gate}
    manager = _manager(tmp_path, children, bus)
    await _start(manager, "A", "turn-1")
    await _start(manager, "B", "turn-1")
    gate.set()
    notice = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of A." in notice.content and "Result of B." in notice.content
    await _no_notice(bus)


@pytest.mark.asyncio
async def test_different_turns_report_separately_and_a_lone_run_at_once(tmp_path):
    bus, children = MessageBus(), _Children()
    children.gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    manager = _manager(tmp_path, children, bus)
    await _start(manager, "A", "turn-1")
    await _start(manager, "B", "turn-2")
    children.gates["A"].set()
    first = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of A." in first.content and "Result of B." not in first.content
    children.gates["B"].set()
    second = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of B." in second.content


@pytest.mark.asyncio
async def test_a_result_already_taken_with_agent_wait_is_not_repeated(tmp_path):
    bus, children = MessageBus(), _Children()
    children.gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    manager = _manager(tmp_path, children, bus)
    a = await _start(manager, "A", "turn-1")
    await _start(manager, "B", "turn-1")
    children.gates["A"].set()
    with tool_execution_scope(CHAT):
        outcome = json.loads(await AgentWaitTool(manager).execute(run_ids=[a], timeout_seconds=5))
    assert [f["result"] for f in outcome["finished"]] == ["Result of A."]
    children.gates["B"].set()
    notice = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of B." in notice.content and "Result of A." not in notice.content


@pytest.mark.asyncio
async def test_after_a_restart_held_results_still_report_together(tmp_path):
    bus, children = MessageBus(), _Children()
    children.gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    manager = _manager(tmp_path, children, bus)
    await _start(manager, "A", "turn-1")
    await _start(manager, "B", "turn-1")
    children.gates["A"].set()
    await _no_notice(bus)
    # The process stops here, with A's result held and B still running.
    subagent_owner.forget_instance(manager._instance)
    restarted_bus = MessageBus()
    restarted = _manager(tmp_path, _Children(), restarted_bus)
    try:
        restarted.resume_pending()
        notice = await asyncio.wait_for(restarted_bus.consume_inbound(), 3)
        assert "Result of A." in notice.content
        assert "process restarted" in notice.content.lower()
        await _no_notice(restarted_bus)
    finally:
        manager._registry = restarted.registry  # the old process is gone
        for task in manager._running_tasks.values():
            task.cancel()
        await asyncio.sleep(0.05)


# ── agent_wait is short, bounded per turn, and yields to the owner ──────────

@pytest.mark.asyncio
async def test_a_turn_cannot_wait_past_its_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_tools, "_WAIT_TURN_BUDGET_SECONDS", 1)
    children = _Children()
    children.gates = {"A": asyncio.Event()}
    manager = _manager(tmp_path, children, MessageBus())
    await _start(manager, "A", "turn-1")
    turn = DelegationTurn(session_key=CHAT)
    with tool_execution_scope(CHAT), delegation_turn_scope(turn):
        first = json.loads(await AgentWaitTool(manager).execute(timeout_seconds=30))
        second = json.loads(await AgentWaitTool(manager).execute(timeout_seconds=30))
    assert first["timed_out"] is True and "end your turn" in first["note"]
    assert second["still_running"] == ["A"] and "waited long enough" in second["note"]
    assert turn.waited[0] < 2
    manager.cancel_all()
    await asyncio.sleep(0.05)


def test_waits_are_short_by_default():
    params = AgentWaitTool(object()).parameters["properties"]["timeout_seconds"]  # type: ignore[arg-type]
    assert params["maximum"] == 120 and "default 30" in params["description"]


@pytest.mark.asyncio
async def test_a_report_turn_stops_waiting_when_the_owner_writes(tmp_path):
    from flowly.agent.loop import AgentLoop
    from flowly.config.schema import Config

    class _Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="unused")
            self.waits: list[dict] = []

        def get_default_model(self) -> str:
            return "m"

        async def chat(self, **kwargs: Any) -> LLMResponse:
            tool_results = [m for m in kwargs["messages"] if m.get("role") == "tool"]
            if not tool_results:
                return LLMResponse(content=None, tool_calls=[
                    ToolCallRequest(id="w", name="agent_wait", arguments={"timeout_seconds": 60}),
                ])
            self.waits.append(json.loads(tool_results[-1]["content"]))
            return LLMResponse(content="Both are done; here is the summary.")

    provider = _Provider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     main_config=Config(), max_iterations=5, soft_warn_at_iteration=0)
    children = _Children()
    children.gates = {"Other": asyncio.Event()}
    loop.subagents.provider = children
    await loop.subagents.spawn(task="task Other", origin_channel="web", origin_chat_id="conn",
                               parent_session_key=CHAT, origin="agent", display_name="Other")
    notice = ConversationInboundMessage(
        channel="system", sender_id="subagent", chat_id="web:conn",
        content="[Subagent 'A' completed successfully] run_id=00000000-0000-0000-0000-000000000000\n\nResult:\nx",
        conversation_key=CHAT,
    )

    async def owner_writes() -> None:
        await asyncio.sleep(0.3)
        loop._goal_user_epochs = {CHAT: loop.goal_user_epoch(CHAT) + 1}

    try:
        started = asyncio.get_running_loop().time()
        writer = asyncio.create_task(owner_writes())
        await loop._process_system_message(notice)
        await writer
        assert asyncio.get_running_loop().time() - started < 5
        assert provider.waits[0]["interrupted"] is True
        assert "answer it right after this" in provider.waits[0]["note"]
    finally:
        loop.subagents.cancel_all()
        await asyncio.sleep(0.05)
        loop.stop()


@pytest.mark.asyncio
async def test_a_report_turn_does_not_hold_up_other_conversations(tmp_path):
    from flowly.agent.loop import AgentLoop
    from flowly.config.schema import Config

    loop = AgentLoop(bus=MessageBus(), provider=_Children(), workspace=tmp_path,
                     main_config=Config(), max_iterations=2, soft_warn_at_iteration=0)
    loop._concurrent_turns = set()
    seen: list[str] = []
    release = asyncio.Event()

    async def slow_turn(msg):
        seen.append(msg.session_key)
        await release.wait()

    loop._process_turn = slow_turn  # type: ignore[method-assign]
    try:
        report = ConversationInboundMessage(channel="system", sender_id="subagent",
                                            chat_id="web:conn", content="x", conversation_key=CHAT)
        await asyncio.wait_for(loop._dispatch_inbound(report), 1)  # returns at once
        await asyncio.sleep(0)
        assert seen == [CHAT] and len(loop._concurrent_turns) == 1
    finally:
        release.set()
        await asyncio.sleep(0.01)
        loop.stop()


# ── A plan does not keep the agent going while its subagents work ───────────

@pytest.fixture
def plans_unattended(monkeypatch):
    from flowly.agent.tools.plan import PlanTool
    from flowly.plans.manager import reset_plan_manager_singleton
    from flowly.plans.store import reset_plan_store_singleton

    monkeypatch.setattr(PlanTool, "_runs_unattended", lambda self: True)
    reset_plan_store_singleton()
    reset_plan_manager_singleton()
    yield
    reset_plan_manager_singleton()
    reset_plan_store_singleton()


@pytest.mark.asyncio
@pytest.mark.parametrize("children_running", [True, False])
async def test_a_plan_does_not_push_the_agent_to_wait_for_its_subagents(
    tmp_path, plans_unattended, children_running,
):
    from flowly.agent.loop import AgentLoop
    from flowly.config.schema import Config

    steps = [{"id": 1, "content": "Start agents"}, {"id": 2, "content": "Verify"}]

    class _Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="unused")
            self.calls = 0

        def get_default_model(self) -> str:
            return "m"

        async def chat(self, **kwargs: Any) -> LLMResponse:
            if not kwargs.get("tools"):
                return LLMResponse(content="Title")  # side calls (a chat title)
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    id="p", name="plan", arguments={"action": "propose", "goal": "g", "steps": steps},
                )])
            if self.calls == 2:
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    id="u", name="plan",
                    arguments={"action": "update", "steps": [{"id": 1, "status": "completed"}]},
                )])
            return LLMResponse(content="Agents are researching; I'll report when they finish.")

    provider = _Provider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     main_config=Config(), max_iterations=8, soft_warn_at_iteration=0)
    children = _Children()
    children.gates = {"A": asyncio.Event()}
    loop.subagents.provider = children
    if children_running:
        await loop.subagents.spawn(task="task A", origin_channel="web", origin_chat_id="conn",
                                   parent_session_key=CHAT, origin="agent", display_name="A")
    try:
        await loop.process_direct("go", session_key=CHAT)
    finally:
        loop.subagents.cancel_all()
        await asyncio.sleep(0.05)
        loop.stop()
    # With its subagents at work the agent may end the turn; without them
    # the plan's unfinished step earns the usual one nudge.
    assert provider.calls == (3 if children_running else 4)


# ── A subagent going round in circles wraps up ──────────────────────────────

@pytest.mark.asyncio
async def test_a_subagent_repeating_the_same_reads_wraps_up_with_a_summary(tmp_path):
    class _Circling(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="unused")
            self.calls = 0

        def get_default_model(self) -> str:
            return "m"

        async def chat(self, **kwargs: Any) -> LLMResponse:
            self.calls += 1
            if kwargs.get("tools") is None:
                return LLMResponse(content="Summary of what I found.")
            return LLMResponse(content=None, tool_calls=[
                ToolCallRequest(id=f"c{self.calls}", name="list_dir", arguments={"path": str(tmp_path)}),
            ])

    provider = _Circling()
    manager = _manager(tmp_path, provider, MessageBus())
    result = await manager.spawn("look around", origin_channel="web", origin_chat_id="c", wait=True)
    assert result == "Summary of what I found."
    assert provider.calls == 1 + 4 + 1  # first read, four repeats, the summary


# ── A search backend without its SDK is not offered ─────────────────────────

def test_a_search_backend_without_its_sdk_is_not_available(monkeypatch):
    from flowly.agent.tools.web_providers import exa, firecrawl, parallel

    section = type("S", (), {"enabled": True, "api_key": "k", "api_url": ""})()
    for module, provider in (
        (exa, exa.ExaWebSearchProvider()),
        (parallel, parallel.ParallelWebSearchProvider()),
        (firecrawl, firecrawl.FirecrawlWebSearchProvider()),
    ):
        monkeypatch.setattr(module, "provider_section", lambda _name, s=section: s)
        monkeypatch.setattr(module, "_sdk_installed", lambda: False)
        assert provider.is_available() is False
        monkeypatch.setattr(module, "_sdk_installed", lambda: True)
        assert provider.is_available() is True


# ── Stopping on request ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stopping_every_agent_takes_one_call_and_reports_nothing_back(tmp_path):
    from flowly.agent.tools.sessions_list import SessionsListTool

    bus, children = MessageBus(), _Children()
    children.gates = {name: asyncio.Event() for name in ("A", "B", "C", "other")}
    manager = _manager(tmp_path, children, bus)
    for name in "ABC":
        await _start(manager, name, "turn-1")
    other = json.loads(await manager.spawn(
        task="task other", origin_channel="web", origin_chat_id="x",
        parent_session_key="web:other-chat", origin="agent", display_name="Other",
    ))["run_id"]
    tool = SessionsListTool(registry=manager.registry, manager=manager)
    with tool_execution_scope(CHAT):
        result = await tool.execute(action="cancel", run_id="all")
    assert result.startswith("Stopped 3 task(s)")
    assert {r.outcome for r in manager.registry.all() if r.parent_session_key == CHAT} == {"cancelled"}
    assert manager.registry.get(other).ended_at is None  # another chat's work is untouched
    await _no_notice(bus)  # whoever stopped them already knows
    await manager.cancel(other)


@pytest.mark.asyncio
async def test_a_run_stopped_on_request_is_left_out_of_its_batchs_report(tmp_path):
    bus, children = MessageBus(), _Children()
    children.gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    manager = _manager(tmp_path, children, bus)
    await _start(manager, "A", "turn-1")
    b = await _start(manager, "B", "turn-1")
    await manager.cancel(b)
    await _no_notice(bus)
    children.gates["A"].set()
    notice = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of A." in notice.content and f"run_id={b}" not in notice.content
