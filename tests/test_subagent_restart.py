"""What survives a restart: appends after a torn line, run ownership, the
delivery commit point, results older than an hour, and the shutdown order.

* a transcript or metrics line torn by a crash never swallows the next one;
* a run belongs to the process working on it: no other process settles it
  while that process lives, and of two processes recovering the same dead
  owner's run only one takes it;
* a result is delivered when the conversation saved the turn that handled it
  — a crash after that save never announces it again, and a result agent_wait
  took into a turn that was never saved is announced again;
* a result that finished before a long downtime still reaches the
  conversation, saying how old it is, a bounded number of times;
* the gateway stops subagents before anything else.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import stat
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from flowly.agent import subagent_owner
from flowly.agent.subagent import (
    SUBAGENT_REPORTS_KEY,
    SubagentManager,
    record_subagent_reports,
)
from flowly.agent.subagent_metrics import append_metric, read_metrics
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord, append_jsonl
from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools.agent_tools import AgentWaitTool
from flowly.bus.events import ConversationInboundMessage, InboundMessage
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

CHAT = "web:chat-1"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _Gated(LLMProvider):
    """A child answers once its gate (named in the brief) opens."""

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
        return LLMResponse(content="done")


def _manager(tmp_path, provider: LLMProvider | None = None, bus: MessageBus | None = None) -> SubagentManager:
    return SubagentManager(provider=provider or _Gated(), workspace=tmp_path, bus=bus or MessageBus(),
                           registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path)


def _record(run_id: str, **changes: Any) -> SubagentRunRecord:
    base = dict(run_id=run_id, child_session_key=f"subagent:{run_id}", parent_session_key=CHAT,
                parent_channel="web", parent_chat_id="chat-1", task=f"task {run_id}", label=run_id,
                display_name=run_id.upper(), model="m", cleanup="keep", created_at=time.time() - 60,
                origin="agent")
    return SubagentRunRecord(**(base | changes))


async def _no_notice(bus: MessageBus, wait: float = 0.3) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.consume_inbound(), wait)


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


# ── Appends after a torn line ────────────────────────────────────────────────

def test_append_after_a_torn_line_starts_on_a_fresh_line(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": ')
    append_jsonl(path, b'{"c": 3}\n')
    lines = path.read_bytes().splitlines()
    assert lines == [b'{"a": 1}', b'{"b": ', b'{"c": 3}']
    fresh = tmp_path / "new.jsonl"
    append_jsonl(fresh, b"{}\n")
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600


def test_a_transcript_continues_gapless_after_a_torn_step(tmp_path):
    registry = SubagentRegistry(tmp_path / "runs.json")
    registry.register(_record("r1"))
    for i in range(3):
        registry.append_transcript("r1", {"kind": "assistant", "content": f"step {i}"})
    path = registry._transcript_path("r1")
    with path.open("ab") as stream:
        stream.write(b'{"seq": 4, "ts": 1, "kind": "tool", "content": "cut of')  # crash mid-write
    # A new process appends the next step.
    restarted = SubagentRegistry(tmp_path / "runs.json")
    assert restarted.append_transcript("r1", {"kind": "resumed", "at": 1.0, "attempt": 1}) == 4
    items = restarted.read_transcript("r1")["items"]
    assert [item["seq"] for item in items] == [1, 2, 3, 4]
    assert items[-1]["kind"] == "resumed"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_transcript_written_before_it_was_private_becomes_private(tmp_path):
    registry = SubagentRegistry(tmp_path / "runs.json")
    registry.register(_record("r1"))
    path = registry._transcript_path("r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seq": 1, "ts": 1, "kind": "brief"}) + "\n")
    os.chmod(path, 0o644)
    restarted = SubagentRegistry(tmp_path / "runs.json")
    assert restarted.append_transcript("r1", {"kind": "assistant"}) == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_metric_after_a_torn_line_is_kept(tmp_path):
    path = tmp_path / "metrics.jsonl"
    first = _record("a", ended_at=time.time(), outcome="ok")
    append_metric(path, first)
    with path.open("ab") as stream:
        stream.write(b'{"v":1,"runId":"b","endedAt":')
    append_metric(path, _record("c", ended_at=time.time(), outcome="error"))
    assert [row["runId"] for row in read_metrics(path)] == ["a", "c"]


# ── Ownership ────────────────────────────────────────────────────────────────

def test_owner_states(tmp_path):
    manager = _manager(tmp_path)
    mine = subagent_owner.process_owner(manager._instance)
    assert subagent_owner.owner_state(mine, manager._instance) == subagent_owner.OURS
    # Another live manager in this process.
    assert subagent_owner.owner_state(mine, "someone-else") == subagent_owner.ALIVE
    # Recorded before owners existed: nobody to wait for.
    assert subagent_owner.owner_state({}) == subagent_owner.DEAD
    # An earlier process that had our pid.
    gone = dict(mine, instance="an-earlier-process")
    assert subagent_owner.owner_state(gone) == subagent_owner.DEAD
    # A process that exited.
    assert subagent_owner.owner_state(dict(mine, pid=_dead_pid(), instance="x")) == subagent_owner.DEAD
    # Another boot of the machine.
    if mine["boot"]:
        assert subagent_owner.owner_state(dict(mine, boot="other-boot")) == subagent_owner.DEAD


def test_a_live_process_and_a_reused_pid_are_told_apart(tmp_path):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        from flowly.profile import _process_identity

        identity = _process_identity(sleeper.pid)
        owner = {"instance": "other", "pid": sleeper.pid, "identity": identity,
                 "boot": subagent_owner.boot_id(), "host": "h"}
        assert subagent_owner.owner_state(owner) == subagent_owner.ALIVE
        if identity:
            reused = dict(owner, identity="ps:a process started long ago")
            assert subagent_owner.owner_state(reused) == subagent_owner.DEAD
    finally:
        sleeper.kill()
        sleeper.wait()


@pytest.mark.asyncio
async def test_another_process_never_settles_a_live_owners_run(tmp_path):
    """A CLI or a second gateway on the same home used to finish a live
    gateway's running work as "process restarted"."""
    gated = _Gated()
    gated.gates = {"A": asyncio.Event()}
    bus = MessageBus()
    live = _manager(tmp_path, gated, bus)
    started = json.loads(await live.spawn(task="task A", origin_channel="web", origin_chat_id="chat-1",
                                          parent_session_key=CHAT, origin="agent"))
    other_bus = MessageBus()
    other = _manager(tmp_path, _Gated(), other_bus)
    other.resume_pending()
    await _no_notice(other_bus)
    assert other.registry.get(started["run_id"]).ended_at is None
    gated.gates["A"].set()
    notice = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of A." in notice.content
    assert live.registry.get(started["run_id"]).outcome == "ok"


def test_only_one_of_two_claims_takes_a_dead_owners_run(tmp_path):
    path = tmp_path / "runs.json"
    dead = {"instance": "gone", "pid": _dead_pid(), "identity": None, "boot": None, "host": "h"}
    SubagentRegistry(path).register(_record("r1", owner=dead))
    winners: list[str] = []
    barrier = threading.Barrier(6)

    def claim(i: int) -> None:
        registry = SubagentRegistry(path)
        barrier.wait()
        if registry.claim("r1", {"instance": f"p{i}", "pid": i + 1}, dead) is not None:
            winners.append(f"p{i}")

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert SubagentRegistry(path).get("r1").owner["instance"] == winners[0]


@pytest.mark.asyncio
async def test_two_processes_recovering_one_run_announce_it_once(tmp_path):
    dead = {"instance": "gone", "pid": _dead_pid(), "identity": None, "boot": None, "host": "h"}
    registry = SubagentRegistry(tmp_path / "runs.json")
    registry.register(_record("r1", owner=dead))
    first_bus, second_bus = MessageBus(), MessageBus()
    first, second = _manager(tmp_path, bus=first_bus), _manager(tmp_path, bus=second_bus)
    first.resume_pending()
    second.resume_pending()
    notices = []
    for bus in (first_bus, second_bus):
        try:
            notices.append(await asyncio.wait_for(bus.consume_inbound(), 0.5))
        except asyncio.TimeoutError:
            pass
    assert len(notices) == 1
    assert SubagentRegistry(tmp_path / "runs.json").get("r1").error_code == "process_restarted"


# ── The delivery commit point ────────────────────────────────────────────────

def test_reports_are_kept_bounded_newest_last():
    metadata: dict[str, Any] = {}
    record_subagent_reports(metadata, [f"r{i}" for i in range(300)])
    record_subagent_reports(metadata, ["r5", "new"])
    kept = metadata[SUBAGENT_REPORTS_KEY]
    assert len(kept) == 256 and kept[-2:] == ["r5", "new"] and kept.count("r5") == 1


def _finished(registry: SubagentRegistry, run_id: str, state: str, **changes: Any) -> None:
    registry.register(_record(run_id, **changes))
    registry.finish(run_id, "ok", result=f"answer {run_id}")
    registry.update(run_id, delivery_state=state, announced=True)


@pytest.mark.asyncio
async def test_a_result_whose_turn_was_saved_is_not_announced_again(tmp_path):
    """Crash between the session save and mark_delivered (D1)."""
    registry = SubagentRegistry(tmp_path / "runs.json")
    _finished(registry, "saved", "queued")
    _finished(registry, "unsaved", "queued")
    bus = MessageBus()
    restarted = _manager(tmp_path, bus=bus)
    restarted.committed_reports = lambda key: {"saved"} if key == CHAT else set()
    restarted.resume_pending()
    notice = await asyncio.wait_for(bus.consume_inbound(), 2)
    assert "answer unsaved" in notice.content and "answer saved" not in notice.content
    await _no_notice(bus)
    assert restarted.registry.get("saved").delivery_state == "delivered"


@pytest.mark.asyncio
async def test_a_result_taken_into_a_turn_that_was_never_saved_is_announced_again(tmp_path):
    """agent_wait took it, the process died before the turn was saved (D3)."""
    gated = _Gated()
    gated.gates = {"A": asyncio.Event()}
    gated.gates["A"].set()
    manager = _manager(tmp_path, gated)
    started = json.loads(await manager.spawn(task="task A", origin_channel="web", origin_chat_id="chat-1",
                                             parent_session_key=CHAT, origin="agent"))
    with tool_execution_scope(CHAT):
        outcome = json.loads(await AgentWaitTool(manager).execute(timeout_seconds=5))
    assert [f["result"] for f in outcome["finished"]] == ["Result of A."]
    assert manager.registry.get(started["run_id"]).delivery_state == "taken"
    assert manager.pending_for_goal(CHAT) == [manager.registry.get(started["run_id"]).display_name]
    # The process stops before the turn is saved.
    subagent_owner.forget_instance(manager._instance)
    bus = MessageBus()
    restarted = _manager(tmp_path, bus=bus)
    restarted.committed_reports = lambda key: set()
    restarted.resume_pending()
    notice = await asyncio.wait_for(bus.consume_inbound(), 2)
    assert "Result of A." in notice.content


@pytest.mark.asyncio
async def test_a_turn_that_ends_unsaved_gives_its_taken_results_back(tmp_path):
    gated = _Gated()
    gated.gates = {"A": asyncio.Event()}
    gated.gates["A"].set()
    bus = MessageBus()
    manager = _manager(tmp_path, gated, bus)
    started = json.loads(await manager.spawn(task="task A", origin_channel="web", origin_chat_id="chat-1",
                                             parent_session_key=CHAT, origin="agent"))
    first = await asyncio.wait_for(bus.consume_inbound(), 2)  # its own notice, queued meanwhile
    with tool_execution_scope(CHAT):
        await AgentWaitTool(manager).execute(timeout_seconds=5)
    assert manager.notice_already_handled(first.content)
    manager.release_taken(CHAT)  # the turn failed before its save
    again = await asyncio.wait_for(bus.consume_inbound(), 2)
    assert "Result of A." in again.content
    assert manager.registry.get(started["run_id"]).delivery_state == "queued"
    # A saved turn delivers it and nothing is given back.
    manager.mark_delivered(CHAT, [started["run_id"]])
    manager.release_taken(CHAT)
    await _no_notice(bus)
    assert manager.pending_for_goal(CHAT) == []


class _WaitingAgent(LLMProvider):
    """The main agent: one agent_wait, then a reply. Children answer at once."""

    def __init__(self) -> None:
        super().__init__(api_key="unused")

    def get_default_model(self) -> str:
        return "m"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        messages = kwargs["messages"]
        if str(messages[0].get("content", "")).startswith("# Subagent"):
            return LLMResponse(content="Child result.")
        if not any(m.get("role") == "tool" for m in messages[-3:]):
            return LLMResponse(content=None, tool_calls=[
                ToolCallRequest(id="w1", name="agent_wait", arguments={"timeout_seconds": 5}),
            ])
        return LLMResponse(content="Here is what it found.")


def _agent_loop(tmp_path, provider: LLMProvider):
    from flowly.agent.loop import AgentLoop
    from flowly.config.schema import Config

    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     main_config=Config(), max_iterations=5, soft_warn_at_iteration=0)


@pytest.mark.asyncio
async def test_a_report_turn_records_its_results_in_the_same_save(tmp_path):
    loop = _agent_loop(tmp_path, _WaitingAgent())
    try:
        registry = loop.subagents.registry
        _finished(registry, "r1", "queued")
        notice = ConversationInboundMessage(
            channel="system", sender_id="subagent", chat_id="web:chat-1",
            content="[Subagent 'R1' completed successfully] run_id=00000000-0000-0000-0000-000000000001",
            conversation_key=CHAT,
        )
        # The notice names a run by its uuid; use one.
        registry.register(_record("00000000-0000-0000-0000-000000000001"))
        registry.finish("00000000-0000-0000-0000-000000000001", "ok", result="x")
        registry.update("00000000-0000-0000-0000-000000000001", delivery_state="queued")
        saved: list[list[str]] = []
        real_save = loop.sessions.save

        def save(session, *args, **kwargs):
            saved.append(list(session.metadata.get(SUBAGENT_REPORTS_KEY) or []))
            return real_save(session, *args, **kwargs)

        loop.sessions.save = save
        await loop._process_system_message(notice)
        assert "00000000-0000-0000-0000-000000000001" in saved[-1]
        assert registry.get("00000000-0000-0000-0000-000000000001").delivery_state == "delivered"
        assert "00000000-0000-0000-0000-000000000001" in loop._committed_subagent_reports(CHAT)
    finally:
        loop.stop()


@pytest.mark.asyncio
async def test_an_owner_turn_delivers_what_it_waited_for_only_by_saving(tmp_path):
    loop = _agent_loop(tmp_path, _WaitingAgent())
    try:
        started = json.loads(await loop.subagents.spawn(
            task="task A", origin_channel="web", origin_chat_id="chat-1",
            parent_session_key=CHAT, origin="agent"))
        await asyncio.sleep(0.1)
        record = loop.subagents.registry.get(started["run_id"])
        assert record.ended_at is not None
        message = InboundMessage(channel="web", sender_id="owner", chat_id="chat-1", content="Is it done?")
        await loop._process_message(message)
        record = loop.subagents.registry.get(started["run_id"])
        assert record.delivery_state == "delivered"
        assert started["run_id"] in loop._committed_subagent_reports(message.session_key)
    finally:
        loop.stop()


# ── Results from before a long downtime ──────────────────────────────────────

@pytest.mark.asyncio
async def test_a_result_announced_again_three_times_is_not_announced_a_fourth(tmp_path):
    registry = SubagentRegistry(tmp_path / "runs.json")
    _finished(registry, "r1", "queued")
    registry.update("r1", reannounced=3)
    bus = MessageBus()
    restarted = _manager(tmp_path, bus=bus)
    restarted.resume_pending()
    await _no_notice(bus)
    assert restarted.registry.get("r1").delivery_state == "undelivered"


@pytest.mark.asyncio
async def test_each_announcement_after_a_restart_is_counted(tmp_path):
    registry = SubagentRegistry(tmp_path / "runs.json")
    _finished(registry, "r1", "queued")
    bus = MessageBus()
    _manager(tmp_path, bus=bus).resume_pending()
    await asyncio.wait_for(bus.consume_inbound(), 2)
    assert SubagentRegistry(tmp_path / "runs.json").get("r1").reannounced == 1


def test_a_result_announced_after_a_restart_holds_its_goal_while_handled(tmp_path):
    registry = SubagentRegistry(tmp_path / "runs.json")
    _finished(registry, "r1", "queued")
    registry._runs["r1"].ended_at = time.time() - 3 * 3600
    with registry._lock:
        registry._write_locked()
    restarted = _manager(tmp_path)
    assert restarted.pending_for_goal(CHAT) == ["R1"]


# ── Shutdown ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shutdown_stops_subagents_and_refuses_new_ones(tmp_path):
    gated = _Gated()
    gated.gates = {"A": asyncio.Event()}
    manager = _manager(tmp_path, gated)
    await manager.spawn(task="task A", origin_channel="web", origin_chat_id="chat-1",
                        parent_session_key=CHAT, origin="agent")
    await asyncio.sleep(0.05)
    started = time.monotonic()
    assert await manager.shutdown(wait_seconds=1.5) == 1
    assert time.monotonic() - started < 1.5
    assert manager.get_running_count() == 0
    refused = json.loads(await manager.spawn(task="task B", origin_channel="web", origin_chat_id="chat-1",
                                             parent_session_key=CHAT, origin="agent"))
    assert refused["status"] == "rejected"
    assert await manager.shutdown() == 0  # idempotent


def test_the_gateway_stops_subagents_before_anything_else():
    from flowly.cli import gateway_cmd

    source = inspect.getsource(gateway_cmd)
    cleanup = source[source.index('console.print("[dim]Cleaning up...[/dim]")'):]
    first = cleanup.index("await _shutdown_subagents()")
    for later in ("close_mcp_connection_runtime", "cron.shutdown()", "gateway_server.stop()",
                  "channels.stop_all()"):
        assert first < cleanup.index(later), later
    # …and right when the signal arrives, before the main task is unwound.
    signal_wait = source.index("return_when=asyncio.FIRST_COMPLETED")
    assert source.index("await _shutdown_subagents()", signal_wait) < source.index(
        "# Cancel pending tasks", signal_wait)
