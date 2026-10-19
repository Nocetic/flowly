"""A subagent takes more than one task: follow-ups (phase 4).

A subagent is a thread (``subagent:<threadId>``); every task it does is a run
of that thread. These tests pin

* an idle follow-up: a new turn (``trigger: "followup"``, the next ``turn``)
  whose model gets the thread's own conversation — system prompt, earlier
  tasks, tool calls, results and reports — then the new task, and that is
  saved, reported and streamed like any turn;
* a follow-up while the subagent works: the task enters the running turn at
  its next step and that turn's report covers it; one that arrives too late
  to be read starts the next turn;
* a closed subagent takes no task or message, for each reason, and says why;
* ``agent_close``: closes, stops a running task without a report, tells the
  clients (``threadStatus``);
* spawns and follow-ups started in one parent turn report together, the
  notice naming ``agent_id`` and the task number; the same limits;
* messages kept for an idle subagent's next task;
* the tools: ``agent_followup``, ``agent_close``, ``agent_read`` by
  ``agent_id``, ``sessions_list``; their descriptions state facts only;
* history repair: a turn stopped mid-call leaves a conversation every
  provider accepts, checked against strict ordering;
* checkpoint v3 for a follow-up turn: the golden replay at every model call
  (``session history + checkpoint``), across a summary that is kept for the
  next turn; a restart mid follow-up continues with no call run twice; a
  changed history is not continued; version 2 still continues;
* a long thread: its follow-up fits the window, its summary is kept and the
  next task starts from it.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from flowly.agent import inflight
from flowly.agent.delegation import DelegationTurn, delegation_turn_scope
from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_checkpoint import (
    ADDED_TASK_LABEL,
    CHECKPOINT_VERSION,
    NOT_RUN_TEXT,
    TASK_LABEL,
    ResumeRefusal,
    SubagentCheckpoint,
    history_digest,
    read_checkpoint,
    restore_child_state,
)
from flowly.agent.subagent_metrics import metrics_path, read_metrics, summarize
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord, add_usage
from flowly.agent.subagent_stream import SUBAGENT_TURN_ORIGIN, SubagentStreamHub
from flowly.agent.subagent_threads import (
    STOPPED_REPLY_TEXT,
    THREAD_IDLE_CLOSE_SECONDS,
    UNANSWERED_CALL_TEXT,
    repair_thread_history,
    thread_history,
    thread_session_key,
)
from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools.agent_tools import (
    AgentCloseTool,
    AgentFollowupTool,
    AgentMessageTool,
    AgentReadTool,
)
from flowly.agent.tools.discovery import annotate_search_repetition
from flowly.agent.tools.filesystem import WriteFileTool
from flowly.agent.tools.sessions_list import SessionsListTool
from flowly.agent.tools.shell import ExecTool
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.compaction.service import CompactionService
from flowly.compaction.summarizer import SUMMARIZE_SYSTEM_PROMPT
from flowly.compaction.types import CompactionConfig, is_summary_message
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

CHAT = "desktop:chat-1"
MODEL = "m"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("FLOWLY_HOME", str(path))
    inflight._runs.clear()
    yield path
    inflight._runs.clear()


@pytest.fixture
def side_effects(tmp_path, monkeypatch) -> Path:
    """write_file and exec only note that they ran, in side_effects.log."""
    log = tmp_path / "side_effects.log"

    async def write(self, path: str, content: str, **kwargs: Any) -> str:
        with log.open("a") as stream:
            stream.write(f"write {content[:20]}\n")
        return f"Wrote {path}"

    async def run(self, command: str, **kwargs: Any) -> str:
        with log.open("a") as stream:
            stream.write(f"exec {command}\n")
        return "exit code 0"

    monkeypatch.setattr(WriteFileTool, "execute", write)
    monkeypatch.setattr(ExecTool, "execute", run)
    return log


# ── Helpers ──────────────────────────────────────────────────────────────────


def is_summary_call(messages: list[dict[str, Any]]) -> bool:
    return bool(messages) and messages[0].get("content") == SUMMARIZE_SYSTEM_PROMPT


class _Scripted(LLMProvider):
    """Answers from a script (a default when it runs out); keeps what it was sent
    and, per call, the running run's checkpoint as it stood then. A step may
    wait on ``gates[n]`` (the n-th call). Summary calls are answered apart."""

    def __init__(self, script: list[Any] | None = None, default: str = "All done.") -> None:
        super().__init__(api_key="unused")
        self.script = list(script or [])
        self.default = default
        self.sent: list[list[dict[str, Any]]] = []
        self.logs: list[list[dict[str, Any]]] = []
        self.summary_calls: list[list[dict[str, Any]]] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.waiting: dict[int, asyncio.Event] = {}
        self.manager: SubagentManager | None = None
        self.run_id = ""

    def get_default_model(self) -> str:
        return MODEL

    def gate(self, n: int) -> asyncio.Event:
        self.gates[n] = asyncio.Event()
        self.waiting.setdefault(n, asyncio.Event())
        return self.gates[n]

    async def chat(self, **kwargs: Any) -> LLMResponse:
        if is_summary_call(kwargs["messages"]):
            self.summary_calls.append(copy.deepcopy(kwargs["messages"]))
            return LLMResponse(content=f"Record {len(self.summary_calls)}: the work so far.",
                               usage={"prompt_tokens": 40, "completion_tokens": 9})
        self.sent.append(copy.deepcopy(kwargs["messages"]))
        if self.manager is not None and self.run_id:
            self.logs.append(read_checkpoint(self.manager.registry.checkpoint_path(self.run_id)))
        n = len(self.sent)
        if n in self.gates:
            self.waiting.setdefault(n, asyncio.Event()).set()
            await self.gates[n].wait()
        step = self.script.pop(0) if self.script else LLMResponse(
            content=self.default, usage={"prompt_tokens": 7, "completion_tokens": 3})
        if callable(step):
            step = await step(self)
        return step


def _manager(home: Path, workspace: Path, provider: LLMProvider, *, bus: MessageBus | None = None,
             resume: bool = True, window: int | None = None, max_iterations: int = 30,
             sink: Any = None) -> SubagentManager:
    compaction = None
    if window is not None:
        compaction = CompactionService(provider=provider, model=MODEL, config=CompactionConfig(
            context_window=window, context_window_explicit=True))
    manager = SubagentManager(provider=provider, workspace=workspace, bus=bus or MessageBus(),
                              registry=SubagentRegistry(home / "subagents" / "runs.json"),
                              state_dir=workspace, max_iterations=max_iterations, compaction=compaction)
    manager.resume_enabled = resume
    manager.stream_sink = sink
    if isinstance(provider, _Scripted):
        provider.manager = manager
    return manager


def _call(call_id: str, name: str, **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


def _turn(turn_id: str, owner_text: str = "Ask it to also check B.") -> DelegationTurn:
    return DelegationTurn(owner_text=owner_text, session_key=CHAT, origin_channel="desktop",
                          origin_chat_id="chat-1", turn_id=turn_id, model=MODEL)


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


async def _settle(manager: SubagentManager, run_id: str) -> SubagentRunRecord:
    await _until(lambda: manager.registry.get(run_id) is not None
                 and manager.registry.get(run_id).ended_at is not None and not manager._running_tasks)
    return manager.registry.get(run_id)


async def _spawn(manager: SubagentManager, *, task: str = "Look into the notes", batch: str = "t1",
                 context: str | None = "owner: look into it\nagent: on it", title: str = "Notes") -> str:
    started = json.loads(await manager.spawn(
        task=task, origin_channel="desktop", origin_chat_id="chat-1", parent_session_key=CHAT,
        origin="agent", context=context, batch_id=batch, display_name=title,
    ))
    assert started["status"] == "started", started
    assert started["agent_id"] == started["run_id"]
    if isinstance(manager.provider, _Scripted):
        manager.provider.run_id = started["run_id"]
    return started["run_id"]


async def _follow(manager: SubagentManager, to: str, task: str, *, turn_id: str = "t2",
                  owner_request: str | None = None) -> dict[str, Any]:
    tool = AgentFollowupTool(manager)
    with delegation_turn_scope(_turn(turn_id)), tool_execution_scope(CHAT):
        result = json.loads(await tool.execute(to=to, task=task, owner_request=owner_request))
    if result.get("status") == "started" and isinstance(manager.provider, _Scripted):
        manager.provider.run_id = result["run_id"]
    return result


async def _notices(bus: MessageBus, wait: float = 0.4) -> list[str]:
    found = []
    while True:
        try:
            found.append((await asyncio.wait_for(bus.consume_inbound(), wait)).content)
        except asyncio.TimeoutError:
            return found


def restore(records: list[dict[str, Any]], base: list[dict[str, Any]] | None = None):
    return restore_child_state(records, annotate=annotate_search_repetition, base=base)


def strict_provider_check(messages: list[dict[str, Any]]) -> None:
    """The ordering the strictest providers enforce, as an assertion.

    Every reply's calls are answered right after it, once each, before
    anything else; no result answers a call not made by the reply before it;
    no reply is empty; the list starts with the system prompt and holds no
    other system message but a summary.
    """
    assert messages and messages[0]["role"] == "system"
    open_calls: list[str] = []
    for index, message in enumerate(messages):
        role = message["role"]
        if role == "tool":
            assert open_calls, f"result at {index} answers no call"
            assert message["tool_call_id"] == open_calls.pop(0), f"result at {index} out of order"
            continue
        assert not open_calls, f"calls {open_calls} unanswered before message {index}"
        if role == "system":
            assert index == 0 or is_summary_message(message), f"stray system message at {index}"
        if role == "assistant":
            calls = [c["id"] for c in message.get("tool_calls") or []]
            assert calls or str(message.get("content") or "").strip(), f"empty reply at {index}"
            open_calls = calls
    assert not open_calls, f"calls {open_calls} unanswered at the end"


def _history_rows(manager: SubagentManager, thread_id: str) -> list[dict[str, Any]]:
    return feature_rpc.subagents_history({"threadId": thread_id, "limit": 200})["messages"]


@pytest.fixture
def rpc(monkeypatch, home):
    """feature_rpc reads this home's registry; the parent conversation exists."""
    def registry_of(manager: SubagentManager):
        monkeypatch.setattr(feature_rpc, "_subagent_registry", lambda: manager.registry)
        monkeypatch.setattr(feature_rpc, "_require_parent_visible", lambda key: None)
    return registry_of


# ── An idle subagent's next task ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_idle_followup_is_a_new_turn_that_sees_the_earlier_history(tmp_path, home, rpc):
    (tmp_path / "notes.txt").write_text("A costs 10.")
    provider = _Scripted([
        LLMResponse(content="Reading.", tool_calls=[_call("c1", "read_file", path=str(tmp_path / "notes.txt"))],
                    usage={"prompt_tokens": 20, "completion_tokens": 4}),
        LLMResponse(content="A costs 10.", usage={"prompt_tokens": 30, "completion_tokens": 5}),
        LLMResponse(content="B costs 12, and A was 10.", usage={"prompt_tokens": 50, "completion_tokens": 6}),
    ])
    bus = MessageBus()
    manager = _manager(home, tmp_path, provider, bus=bus)
    rpc(manager)
    first = await _spawn(manager)
    one = await _settle(manager, first)
    first_notice = (await _notices(bus))[0]
    assert f"run_id={first} agent_id={first}" in first_notice
    assert "agent_followup gives it another task, agent_close ends it." in first_notice
    turn_one = copy.deepcopy(provider.sent[-1])  # what the model had at its report

    started = await _follow(manager, first, "Also check what B costs.")
    assert started["status"] == "started" and started["agent_id"] == first and started["turn"] == 2
    second = started["run_id"]
    assert second != first
    two = await _settle(manager, second)

    assert two.outcome == "ok" and two.result_preview == "B costs 12, and A was 10."
    assert (two.thread_id, two.turn, two.trigger) == (first, 2, "followup")
    assert two.child_session_key == f"subagent:{first}" and two.mark == one.mark
    assert two.batch_id == "t2" and two.origin == "agent" and two.task == "Also check what B costs."
    thread = manager.registry.get_thread(first)
    assert thread.runs == [first, second] and thread.active_run_id is None and thread.status == "open"
    assert thread.usage["input"] == 20 + 30 + 50

    # The model got its own conversation, then the new task: the same system
    # prompt, the first task, its call and result, its report, then the task.
    sent = provider.sent[-1]
    assert sent[0] == turn_one[0]
    assert sent[1:-1] == [*turn_one[1:], {"role": "assistant", "content": "A costs 10."}]
    assert sent[-1] == {"role": "user", "content": f"{TASK_LABEL}\nAlso check what B costs."}
    strict_provider_check(sent)

    # Saved as the thread's second turn; reported with its number and agent id.
    rows = _history_rows(manager, first)
    tasks = [r for r in rows if r.get("kind") == "subagent_task"]
    assert [(t["turn"], t["trigger"]) for t in tasks] == [(1, "spawn"), (2, "followup")]
    assert tasks[1]["brief"] == "Also check what B costs." and "withConversation" not in tasks[1]
    assert tasks[0]["withConversation"] is True
    assert rows[-1]["content"][0]["text"] == "B costs 12, and A was 10." and rows[-1]["runId"] == second
    notice = (await _notices(bus))[0]
    assert f"run_id={second} agent_id={first}" in notice
    header = notice.splitlines()[0]
    assert header.startswith("[Subagent 'Notes' completed successfully (runtime: ") and ", task 2)]" in header
    assert "Brief: Also check what B costs." in notice
    # The thread's views say so.
    view = feature_rpc.subagents_history({"threadId": first})["thread"]
    assert view["turns"] == 2 and view["runIds"] == [first, second] and view["status"] == "open"
    assert not manager.registry.checkpoint_path(second).exists()
    # One metric line per task; the stats tell first tasks from later ones.
    rows = read_metrics(metrics_path(manager.registry._path))
    assert [(r["runId"], r["trigger"]) for r in rows] == [(first, "spawn"), (second, "followup")]
    assert summarize(rows, days=1)["byTrigger"] == {"spawn": 1, "followup": 1}


@pytest.mark.asyncio
async def test_a_followup_the_owner_asked_for_is_labeled_theirs_and_never_refused(tmp_path, home):
    manager = _manager(home, tmp_path, _Scripted())
    first = await _spawn(manager)
    await _settle(manager, first)
    mine = await _follow(manager, first, "Check B.", owner_request="compare every price there is")
    assert mine["status"] == "started"  # a quote the owner never wrote refuses nothing …
    assert manager.registry.get(mine["run_id"]).origin == "agent"  # … it only labels the run
    await _settle(manager, mine["run_id"])
    tool = AgentFollowupTool(manager)
    with delegation_turn_scope(_turn("t4", "Aynı agent'a B'yi de kontrol ettir.")), tool_execution_scope(CHAT):
        owner = json.loads(await tool.execute(to=first, task="Check B as well.",
                                              owner_request="Aynı agent'a B'yi de kontrol ettir"))
    assert owner["status"] == "started" and manager.registry.get(owner["run_id"]).origin == "owner"
    await _settle(manager, owner["run_id"])


# ── A follow-up while it works ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_followup_while_it_works_enters_the_running_turn(tmp_path, home, rpc):
    (tmp_path / "notes.txt").write_text("notes")
    provider = _Scripted([
        LLMResponse(content="Reading.", tool_calls=[_call("c1", "read_file", path=str(tmp_path / "notes.txt"))]),
        LLMResponse(content="A checked."),  # produced while the task arrives: not the end
        LLMResponse(content="Both checked: A and B."),
    ])
    gate = provider.gate(2)
    bus = MessageBus()
    hub_events: list[tuple[str, str, dict[str, Any]]] = []
    manager = _manager(home, tmp_path, provider, bus=bus, sink=_ListSink(hub_events))
    rpc(manager)
    first = await _spawn(manager)
    await provider.waiting[2].wait()  # its second model call: the step has begun

    added = await _follow(manager, first, "Also check B.")
    assert added["status"] == "added_to_running_task" and added["run_id"] == first
    assert added["agent_id"] == first and added["turn"] == 1
    assert "the report of the task it is on covers this one too" in added["message"]
    gate.set()
    record = await _settle(manager, first)

    assert record.outcome == "ok" and len(manager.registry.all()) == 1
    # Read at its next step, labeled as a task: the reply it was writing is
    # kept, and it goes on.
    assert len(provider.sent) == 3
    assert provider.sent[-1][-2:] == [{"role": "assistant", "content": "A checked."},
                                      {"role": "user", "content": f"{ADDED_TASK_LABEL}\nAlso check B."}]
    rows = _history_rows(manager, first)
    notes = [r for r in rows if r.get("kind") == "agent_note"]
    assert [(n["noteKind"], n["author"]) for n in notes] == [("task", "main_agent")]
    live = [d for e, _k, d in hub_events if e == "chat" and d.get("state") == "agent_note"]
    assert [n["noteKind"] for n in live] == ["task"]
    # One report covers both.
    notices = await _notices(bus)
    assert len(notices) == 1 and "Both checked: A and B." in notices[0]


class _ListSink:
    """A stream sink that keeps every event (no transports)."""

    def __init__(self, events: list[tuple[str, str, dict[str, Any]]]) -> None:
        self.events = events

    def publish(self, event: str, session_key: str, data: dict[str, Any], **kwargs: Any) -> None:
        self.events.append((event, session_key, json.loads(json.dumps(data))))


@pytest.mark.asyncio
async def test_a_task_that_arrives_as_the_turn_ends_starts_the_next_turn(tmp_path, home):
    provider = _Scripted([LLMResponse(content="First done."), LLMResponse(content="Second done.")])
    gate = provider.gate(1)
    bus = MessageBus()
    manager = _manager(home, tmp_path, provider, bus=bus, max_iterations=1)
    first = await _spawn(manager)
    await provider.waiting[1].wait()  # its only step's model call: nothing more is read
    added = await _follow(manager, first, "Then do the second part.")
    assert added["status"] == "added_to_running_task"
    gate.set()
    await _until(lambda: len(manager.registry.all()) == 2)
    second = next(r for r in manager.registry.all() if r.run_id != first)
    record = await _settle(manager, second.run_id)
    assert (record.turn, record.trigger, record.batch_id) == (2, "followup", "t2")
    assert record.result_preview == "Second done."
    assert provider.sent[-1][-1] == {"role": "user", "content": f"{TASK_LABEL}\nThen do the second part."}
    notices = await _notices(bus)
    assert len(notices) == 2 and "First done." in notices[0] and "Second done." in notices[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["owner_stop", "cancel"])
async def test_a_stopped_turn_starts_no_task_it_did_not_read_but_keeps_its_messages(tmp_path, home, how):
    provider = _Scripted([LLMResponse(content="never used")])
    gate = provider.gate(1)
    manager = _manager(home, tmp_path, provider)
    first = await _spawn(manager)
    await provider.waiting[1].wait()
    assert (await _follow(manager, first, "Then the second part."))["status"] == "added_to_running_task"
    with tool_execution_scope(CHAT):
        await AgentMessageTool(manager).execute(to=first, message="Use 2025 prices.")
    if how == "owner_stop":
        manager.cancel_children(CHAT)  # the owner stopped the conversation's turn
    else:
        await manager.cancel(first)
    gate.set()
    record = await _settle(manager, first)
    await asyncio.sleep(0.05)
    assert record.outcome == ("interrupted" if how == "owner_stop" else "cancelled")
    assert len(manager.registry.all()) == 1 and not manager._running_tasks  # no new turn
    thread = manager.registry.get_thread(first)
    assert [(m["text"], m.get("kind")) for m in thread.pending_mail] == [("Use 2025 prices.", None)]


@pytest.mark.asyncio
async def test_a_kept_task_survives_a_restart_and_starts_then(tmp_path, home):
    provider = _Scripted([LLMResponse(content="First done.")])
    gate = provider.gate(1)
    manager = _manager(home, tmp_path, provider, max_iterations=1)
    first = await _spawn(manager)
    await provider.waiting[1].wait()
    assert (await _follow(manager, first, "Then the second part."))["status"] == "added_to_running_task"
    manager._shutting_down = True  # the kept task cannot start in this process any more
    gate.set()
    await _settle(manager, first)
    await asyncio.sleep(0.05)
    kept = manager.registry.get_thread(first).pending_mail
    assert [(m["text"], m["kind"], m["batch"]) for m in kept] == [("Then the second part.", "task", "t2")]
    from flowly.agent import subagent_owner
    subagent_owner.forget_instance(manager._instance)

    later = _Scripted([LLMResponse(content="Second done.")])
    restarted = _manager(home, tmp_path, later)
    restarted.resume_pending()
    await _until(lambda: len(restarted.registry.all()) == 2)
    second = next(r for r in restarted.registry.all() if r.run_id != first)
    record = await _settle(restarted, second.run_id)
    assert (record.turn, record.trigger, record.batch_id, record.result_preview) == (
        2, "followup", "t2", "Second done.")
    assert later.sent[-1][-1] == {"role": "user", "content": f"{TASK_LABEL}\nThen the second part."}
    assert restarted.registry.get_thread(first).pending_mail == []


def test_a_turn_number_is_decided_under_the_registry_lock(home):
    registry = SubagentRegistry(home / "subagents" / "runs.json")
    now = time.time()

    def run(run_id: str, turn: int, thread: str) -> SubagentRunRecord:
        return SubagentRunRecord(
            run_id=run_id, child_session_key=f"subagent:{thread}", parent_session_key=CHAT,
            parent_channel="desktop", parent_chat_id="chat-1", task="t", label="t", model=MODEL,
            cleanup="keep", created_at=now, origin="agent", thread_id=thread, turn=turn,
            trigger="spawn" if turn == 1 else "followup")

    from flowly.agent.subagent_threads import SubagentThreadRecord
    thread_id = "11111111-1111-4111-8111-111111111111"
    registry.register(run(thread_id, 1, thread_id), thread=SubagentThreadRecord(
        thread_id=thread_id, parent_session_key=CHAT, child_session_key=f"subagent:{thread_id}",
        created_at=now))
    registry.finish(thread_id, "ok", result="r")
    stale = run("22222222-2222-4222-8222-222222222222", 7, thread_id)  # a stale count
    registry.register(stale)
    assert registry.get(stale.run_id).turn == 2


# ── Closed subagents ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["agent", "parent_deleted", "idle"])
async def test_a_closed_subagent_takes_no_task_or_message_and_says_why(tmp_path, home, reason):
    manager = _manager(home, tmp_path, _Scripted())
    first = await _spawn(manager)
    await _settle(manager, first)
    if reason == "agent":
        await manager.close_threads([first], parent_session_key=CHAT)
        why = "'Notes' was closed with agent_close; it takes no new task or message. Its history stays readable."
    elif reason == "parent_deleted":
        manager.forget_conversation(CHAT)
        why = "'Notes' belonged to a conversation that was deleted; it takes no new task or message."
    else:
        thread = manager.registry.get_thread(first)
        thread.last_run_ended_at = time.time() - THREAD_IDLE_CLOSE_SECONDS - 60
        with manager.registry._lock:
            manager.registry._write_locked()
        manager.registry.maintain()
        why = "'Notes' was closed after 7 days with no task; it takes no new task or message."
    assert manager.registry.get_thread(first).closed_reason == reason

    refused = await _follow(manager, first, "One more thing.")
    assert refused == {"status": "closed", "agent_id": first, "title": "Notes", "error": refused["error"]}
    assert refused["error"].startswith(why)
    with tool_execution_scope(CHAT):
        message = json.loads(await AgentMessageTool(manager).execute(to=first, message="hello?"))
    assert message["sent"][0]["status"] == "closed" and message["sent"][0]["error"].startswith(why)
    assert len(manager.registry.all()) == 1 and not manager._running_tasks


@pytest.mark.asyncio
async def test_agent_close_stops_a_working_subagent_without_a_report(tmp_path, home):
    (tmp_path / "notes.txt").write_text("notes")
    provider = _Scripted([
        LLMResponse(content="Reading.", tool_calls=[_call("c1", "read_file", path=str(tmp_path / "notes.txt"))]),
    ])
    gate = provider.gate(2)
    bus = MessageBus()
    published: list[tuple[str, dict[str, Any]]] = []

    async def on_event(name: str, data: dict) -> None:
        published.append((name, data))

    manager = _manager(home, tmp_path, provider, bus=bus)
    manager._on_event = on_event
    first = await _spawn(manager)
    idle = await _spawn(manager, task="Another", title="Other", batch="t0")
    await provider.waiting[2].wait()
    await _until(lambda: manager.registry.get(idle).ended_at is not None or len(provider.sent) >= 2)

    with delegation_turn_scope(_turn("t2")), tool_execution_scope(CHAT):
        closed = json.loads(await AgentCloseTool(manager).execute(to=first))
    assert closed["closed"][0]["agent_id"] == first and closed["closed"][0]["status"] == "closed"
    assert closed["closed"][0]["stopped_run_id"] == first
    gate.set()
    record = await _settle(manager, first)
    assert record.outcome == "cancelled" and record.delivery_state == "not_required"
    thread = manager.registry.get_thread(first)
    assert (thread.status, thread.closed_reason, thread.active_run_id) == ("closed", "agent", None)
    # The stopped turn is in its history, stopped, and its open call answered.
    rows = manager.registry.thread_store.display_rows(first)
    assert rows[-1].get("aborted") is True
    # No report for it (the "Other" one reports on its own).
    notices = await _notices(bus)
    assert all(f"run_id={first}" not in n for n in notices)
    await manager._events.flush()
    closing = [d for n, d in published if d["runId"] == first and d.get("threadStatus") == "closed"]
    assert closing, "clients hear that the subagent closed"
    # Closing again, and "all".
    with tool_execution_scope(CHAT):
        again = json.loads(await AgentCloseTool(manager).execute(to=[first]))
        everything = json.loads(await AgentCloseTool(manager).execute(to="all"))
    assert again["closed"][0]["status"] == "already_closed" and again["closed"][0]["reason"] == "agent"
    assert [c["agent_id"] for c in everything["closed"]] == [idle]
    assert manager.registry.get_thread(idle).closed_reason == "agent"
    with tool_execution_scope("desktop:other"):
        foreign = json.loads(await AgentCloseTool(manager).execute(to=idle))
    assert "No subagent run" in foreign["error"]


# ── Batches and limits ───────────────────────────────────────────────────────


class _HoldsB(_Scripted):
    """Holds the request whose task mentions B until ``release`` is set."""

    def __init__(self) -> None:
        super().__init__(default="Done.")
        self.release = asyncio.Event()

    async def chat(self, **kwargs: Any) -> LLMResponse:
        if "price B" in str(kwargs["messages"][-1].get("content")):
            await self.release.wait()
        return await super().chat(**kwargs)


@pytest.mark.asyncio
async def test_spawns_and_followups_of_one_turn_report_together(tmp_path, home):
    provider = _HoldsB()
    bus = MessageBus()
    manager = _manager(home, tmp_path, provider, bus=bus)
    first = await _spawn(manager, task="Price A", title="A")
    await _settle(manager, first)
    await _notices(bus)

    follow = await _follow(manager, first, "Now price B as well.", turn_id="t2")
    with delegation_turn_scope(_turn("t2")):
        fresh = json.loads(await manager.spawn(
            task="Price C", origin_channel="desktop", origin_chat_id="chat-1", parent_session_key=CHAT,
            origin="agent", batch_id="t2", display_name="C",
        ))
    await _until(lambda: manager.registry.get(fresh["run_id"]).ended_at is not None)
    assert await _notices(bus, wait=0.2) == []  # held for its sibling, the follow-up
    provider.release.set()
    await _settle(manager, follow["run_id"])
    notices = await _notices(bus)
    assert len(notices) == 1
    notice = notices[0]
    assert f"run_id={follow['run_id']} agent_id={first}" in notice and ", task 2)]" in notice
    assert f"run_id={fresh['run_id']} agent_id={fresh['run_id']}" in notice
    assert "These 2 subagents were started together and have all finished" in notice
    assert "Each subagent keeps its conversation" in notice


@pytest.mark.asyncio
async def test_followups_count_toward_the_running_limits(tmp_path, home, monkeypatch):
    # A small limit keeps the test short; the rule does not depend on its value.
    monkeypatch.setattr("flowly.agent.subagent.MAX_CHILDREN_PER_CONVERSATION", 3)
    provider = _Scripted(default="Done.")
    manager = _manager(home, tmp_path, provider)
    first = await _spawn(manager, title="A")
    await _settle(manager, first)
    gates = [provider.gate(n) for n in (2, 3, 4)]
    running = [await _spawn(manager, task=f"Task {n}", title=f"T{n}", batch="t2") for n in range(3)]
    refused = await _follow(manager, first, "One more.")
    assert refused["status"] == "rejected" and "3 subagents are already running" in refused["error"]
    assert manager.registry.get_thread(first).runs == [first]
    for gate in gates:
        gate.set()
    for run_id in running:
        await _settle(manager, run_id)


@pytest.mark.asyncio
async def test_a_message_to_an_idle_subagent_waits_for_its_next_task(tmp_path, home):
    provider = _Scripted(default="Done.")
    manager = _manager(home, tmp_path, provider)
    first = await _spawn(manager)
    await _settle(manager, first)
    with tool_execution_scope(CHAT):
        queued = json.loads(await AgentMessageTool(manager).execute(to=first, message="Use 2025 prices."))
    assert queued["sent"][0]["status"] == "queued" and "No task was started" in queued["sent"][0]["note"]
    assert len(manager.registry.all()) == 1
    assert feature_rpc_thread_view(manager, first)["pendingMessages"] == 1

    started = await _follow(manager, first, "Price B.")
    record = await _settle(manager, started["run_id"])
    assert record.notes == ["Use 2025 prices."]
    assert provider.sent[-1][-1]["content"] == (
        "[Message from the main agent]\nUse 2025 prices.\n\n" f"{TASK_LABEL}\nPrice B.")
    assert manager.registry.get_thread(first).pending_mail == []


def feature_rpc_thread_view(manager: SubagentManager, thread_id: str) -> dict[str, Any]:
    from flowly.agent.subagent_threads import thread_view

    return thread_view(manager.registry.get_thread(thread_id))


# ── The tools ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_read_by_agent_id_and_sessions_list_show_the_subagent(tmp_path, home):
    provider = _Scripted([LLMResponse(content="First result."), LLMResponse(content="Second result.")])
    manager = _manager(home, tmp_path, provider)
    first = await _spawn(manager)
    await _settle(manager, first)
    second = (await _follow(manager, first, "Again."))["run_id"]
    await _settle(manager, second)
    reader = AgentReadTool(manager.registry)
    with tool_execution_scope(CHAT):
        latest = json.loads(await reader.execute(agent_id=first))
        by_run = json.loads(await reader.execute(run_id=first))
        neither = json.loads(await reader.execute())
    assert latest["content"] == "Second result." and latest["run_id"] == second
    assert (latest["agent_id"], latest["task_number"]) == (first, 2)
    assert by_run["content"] == "First result."
    assert "run_id or agent_id" in neither["error"]
    with tool_execution_scope(CHAT):
        listing = await SessionsListTool(manager.registry, manager).execute(action="list")
    assert "Subagents (1):" in listing
    assert f"'Notes' [agent_id {first[:8]}] — open, idle, 2 tasks; latest result: Second result." in listing
    assert f"[{second[:8]}, task 2]" in listing


def test_the_new_tools_state_facts_and_never_steer():
    manager = _manager(Path("/tmp"), Path("/tmp"), _Scripted(), resume=False)
    steering = ("should", "prefer", "instead of", "rather than", "consider", "recommend", "better",
                "do it yourself", "do this part yourself", "only if", "avoid")
    for tool in (AgentFollowupTool(manager), AgentCloseTool(manager), AgentMessageTool(manager)):
        text = (tool.description + json.dumps(tool.parameters)).lower()
        for phrase in steering:
            assert phrase not in text, f"{tool.name}: {phrase!r}"
    assert "agent_followup" in AgentFollowupTool(manager).name and "agent_close" == AgentCloseTool(manager).name


# ── History repair ───────────────────────────────────────────────────────────


def test_unanswered_calls_mid_history_are_answered_where_they_were_made():
    history = [
        {"role": "user", "content": "task 1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": "A"},
        {"role": "tool", "tool_call_id": "zzz", "name": "exec", "content": "orphan"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_call_id": "b", "name": "exec", "content": "too late"},
        {"role": "user", "content": "task 2"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "web_fetch", "arguments": "{}"}}]},
        {"role": "assistant", "content": "report"},
    ]
    repaired = repair_thread_history(history)
    strict_provider_check([{"role": "system", "content": "S"}, *repaired])
    assert repaired[2:4] == [
        {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": "A"},
        {"role": "tool", "tool_call_id": "b", "name": "exec", "content": UNANSWERED_CALL_TEXT},
    ]
    assert repaired[4] == {"role": "assistant", "content": "[No reply.]"}
    assert repaired[7] == {"role": "tool", "tool_call_id": "c", "name": "web_fetch", "content": UNANSWERED_CALL_TEXT}
    # A history that is already well formed comes back equal.
    good = [{"role": "user", "content": "t"}, {"role": "assistant", "content": "r"}]
    assert repair_thread_history(good) == good
    # The projection says a stopped turn was stopped.
    rows = [{"role": "user", "content": "t", "kind": "subagent_task"},
            {"role": "assistant", "content": "", "aborted": True, "outcome": "cancelled"}]
    assert thread_history(rows)[1] == {"role": "assistant", "content": STOPPED_REPLY_TEXT}


@pytest.mark.asyncio
async def test_a_turn_stopped_mid_call_leaves_a_history_every_provider_accepts(tmp_path, home, side_effects):
    gate = asyncio.Event()
    started_exec = asyncio.Event()

    async def slow_exec(self, command: str, **kwargs: Any) -> str:
        started_exec.set()
        await gate.wait()
        return "exit code 0"

    provider = _Scripted([
        LLMResponse(content="Two things.", tool_calls=[
            _call("e1", "exec", command="sleep 100"), _call("e2", "exec", command="echo second")]),
        LLMResponse(content="Picked up again: nothing left to do."),
    ])
    manager = _manager(home, tmp_path, provider)
    ExecTool.execute, original = slow_exec, ExecTool.execute
    try:
        first = await _spawn(manager)
        await started_exec.wait()
        await manager.cancel(first)
    finally:
        ExecTool.execute = original
    record = await _settle(manager, first)
    assert record.outcome == "cancelled"
    rows = manager.registry.thread_store.display_rows(first)
    results = [(r["tool_call_id"], r["status"]) for r in rows if r["role"] == "tool"]
    assert results == [("e1", "interrupted"), ("e2", "not_run")]
    assert rows[-1]["aborted"] is True and rows[-1]["outcome"] == "cancelled"

    started = await _follow(manager, first, "Finish what you started.")
    await _settle(manager, started["run_id"])
    sent = provider.sent[-1]
    strict_provider_check(sent)
    texts = [m["content"] for m in sent if m["role"] == "tool"]
    assert texts[0].startswith("Stopped: the task was stopped while this call was running.")
    assert texts[1] == "Not run: the task was stopped before this call started."
    assert {"role": "assistant", "content": STOPPED_REPLY_TEXT} in sent
    assert not side_effects.exists()  # the stopped call ran no further, nothing else ran


@pytest.mark.asyncio
async def test_an_older_conversation_with_calls_left_open_mid_history_is_repaired(tmp_path, home):
    provider = _Scripted([LLMResponse(content="First."), LLMResponse(content="Second.")])
    manager = _manager(home, tmp_path, provider)
    first = await _spawn(manager)
    await _settle(manager, first)
    # A conversation saved before stopped turns answered their calls: a call
    # with no result, then the turn's closing row and a later turn.
    key = thread_session_key(first)

    def damage(session: Any) -> None:
        closing = session.messages.pop()
        session.add_message("assistant", "", tool_calls=[
            {"id": "x1", "type": "function", "function": {"name": "web_fetch", "arguments": "{}"}}])
        session.messages.append(closing)

    manager.registry.thread_store.sessions.mutate(key, damage)
    manager.registry.thread_store.sessions.evict(key)
    started = await _follow(manager, first, "Next.")
    await _settle(manager, started["run_id"])
    strict_provider_check(provider.sent[-1])
    assert {"role": "tool", "tool_call_id": "x1", "name": "web_fetch",
            "content": UNANSWERED_CALL_TEXT} in provider.sent[-1]


# ── Checkpoint v3: a follow-up turn replays on top of the thread's history ──


_LINE = "2026-08-04 04:12:33 INFO worker {n} finished batch {i} in 182ms; queue depth 7\n"


def _log_file(n: int, chars: int = 1_620) -> str:
    return "".join(_LINE.format(n=n, i=i) for i in range(chars // 60 + 2))[:chars]


def _reads(tmp_path: Path, prefix: str, files: range, text: str | None = "Reading.") -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[
        _call(f"{prefix}{n}", "read_file", path=str(tmp_path / f"f{n}.log")) for n in files],
        usage={"prompt_tokens": 100, "completion_tokens": 5})


def _golden(provider: _Scripted, manager: SubagentManager, thread_id: str, start: int) -> None:
    """At each model call from ``start``: replaying the checkpoint as it stood
    on top of the thread's history (as the model gets it now) gives the very
    list the provider was sent."""
    base = manager.registry.thread_store.turn_context(thread_id).history
    for index in range(start, len(provider.sent)):
        state = restore(provider.logs[index], base)
        assert not isinstance(state, ResumeRefusal), state
        assert state.messages == provider.sent[index], f"call {index + 1} differs"


@pytest.mark.asyncio
async def test_a_followup_turn_replays_exactly_and_its_summary_is_kept_for_the_next(tmp_path, home):
    """A thread over a small window: turn 1 reads logs; turn 2 (a follow-up)
    starts over the threshold, is fitted (cut, then summarized — its earlier
    history too), works, and is kept with its summary; turn 3 starts from that
    summary. At every model call of turns 2 and 3 the checkpoint replays on
    top of the thread's saved history to the list the provider was sent."""
    for n in range(12):
        (tmp_path / f"f{n}.log").write_text(_log_file(n))
    provider = _Scripted([
        _reads(tmp_path, "a", range(0, 3)),
        LLMResponse(content="Turn one: batches read.", usage={"prompt_tokens": 100, "completion_tokens": 5}),
        _reads(tmp_path, "b", range(3, 9), text=None),
        LLMResponse(content="Drafting.", tool_calls=[
            _call("w1", "write_file", path=str(tmp_path / "draft.md"), content="R" * WRITE_CHARS)],
            usage={"prompt_tokens": 100, "completion_tokens": 5}),
        _reads(tmp_path, "c", range(9, 12), text="Three more."),
        LLMResponse(content="Turn two: slowest batch is 41.", usage={"prompt_tokens": 100, "completion_tokens": 5}),
        LLMResponse(content="Turn three: confirmed.", usage={"prompt_tokens": 100, "completion_tokens": 5}),
    ])
    manager = _manager(home, tmp_path, provider, window=12_000)
    first = await _spawn(manager)
    await _settle(manager, first)
    threshold = manager.compaction.compaction_threshold_for(MODEL)

    turn_two_from = len(provider.sent)
    base_before = manager.registry.thread_store.turn_context(first).history
    second = (await _follow(manager, first, "Find the slowest batch in the rest."))["run_id"]
    record = await _settle(manager, second)
    assert record.outcome == "ok", record.error
    # Its checkpoint names the history it started from.
    header = provider.logs[turn_two_from][0]
    assert header["v"] == CHECKPOINT_VERSION == 3 and header["baseCount"] == len(base_before) > 0
    assert header["baseDigest"] == history_digest(base_before)
    assert (header["threadId"], header["turn"], header["trigger"]) == (first, 2, "followup")
    # Replays exactly, on top of the history as it was when the turn started.
    for index in range(turn_two_from, len(provider.sent)):
        state = restore(provider.logs[index], base_before)
        assert not isinstance(state, ResumeRefusal), state
        assert state.messages == provider.sent[index], f"call {index + 1} differs"
    fits = [r for r in provider.logs[-1] if r["type"] in ("microcompact", "compacted")]
    assert any(r["type"] == "compacted" and r["summarized"] for r in fits)
    for sent in provider.sent[turn_two_from:]:
        assert CompactionServiceBudget(manager).tokens(sent) <= threshold
        strict_provider_check(sent)
        # The task stays verbatim, after the summary and whatever history it kept.
        assert {"role": "user", "content": f"{TASK_LABEL}\nFind the slowest batch in the rest."} in sent
    summarized = next(s for s in provider.sent[turn_two_from:] if is_summary_message(s[1]))
    assert summarized[0] == provider.sent[0][0]

    # The summary is kept: the working context starts with it; the display
    # still holds every row, with the compaction seam.
    session = manager.registry.thread_store.sessions.read(thread_session_key(first))
    assert is_summary_message(session.messages[0]) and session.metadata["subagent"]["contextRun"] == second
    rows = manager.registry.thread_store.display_rows(first)
    assert any(r.get("kind") == "context_boundary" for r in rows)
    assert [r.get("turn") for r in rows if r.get("kind") == "subagent_task"] == [1, 2]
    assert sum(1 for r in rows if r["role"] == "tool") == 13
    calls_before = len(provider.summary_calls)

    # Turn 3 starts from the kept summary: no summary call needed, exact replay.
    turn_three_from = len(provider.sent)
    third = (await _follow(manager, first, "Confirm it.", turn_id="t3"))["run_id"]
    await _settle(manager, third)
    sent = provider.sent[turn_three_from]
    assert is_summary_message(sent[1]) and sent[0] == provider.sent[0][0]
    assert len(provider.summary_calls) == calls_before
    assert sent[-1] == {"role": "user", "content": f"{TASK_LABEL}\nConfirm it."}
    strict_provider_check(sent)
    base_three = provider.logs[turn_three_from][0]
    assert base_three["baseCount"] == len(sent) - 2
    # After the kept summary the display log goes on as before: every row once.
    rows = manager.registry.thread_store.display_rows(first)
    assert [r.get("turn") for r in rows if r.get("kind") == "subagent_task"] == [1, 2, 3]
    assert sum(1 for r in rows if r["role"] == "tool") == 13
    assert rows[-1]["content"] == "Turn three: confirmed." and rows[-1]["run_id"] == third


WRITE_CHARS = 6000


@pytest.mark.asyncio
async def test_a_long_threads_next_task_fits_from_its_first_request(tmp_path, home):
    """Four turns, each with a large write (its arguments cannot be cut), make
    a history over the threshold of a smaller window (the thread now runs on
    a model with less room). The next task's very first request is fitted:
    the earlier turns are summarized,
    what the summary keeps of them stays before the task, the task stays
    verbatim, every request fits and is well formed, the replay is exact, and
    the summary is kept for the task after."""
    script: list[Any] = []
    for n in range(4):
        script += [
            LLMResponse(content=f"Writing part {n}.", tool_calls=[
                _call(f"w{n}", "write_file", path=str(tmp_path / f"part{n}.md"), content=f"{n}" * 9_000)],
                usage={"prompt_tokens": 100, "completion_tokens": 5}),
            LLMResponse(content=f"Part {n} written.", usage={"prompt_tokens": 100, "completion_tokens": 5}),
        ]
    script.append(LLMResponse(content="All four parts are consistent.",
                              usage={"prompt_tokens": 100, "completion_tokens": 5}))
    provider = _Scripted(script)
    roomy = _manager(home, tmp_path, provider, window=64_000)
    first = await _spawn(roomy, context=None, task="Write part 0.")
    await _settle(roomy, first)
    for n in range(1, 4):
        await _settle(roomy, (await _follow(roomy, first, f"Write part {n}.", turn_id=f"t{n}"))["run_id"])
    assert provider.summary_calls == []
    manager = _manager(home, tmp_path, provider, window=12_000)
    threshold = manager.compaction.compaction_threshold_for(MODEL)
    base = manager.registry.thread_store.turn_context(first).history
    assert CompactionServiceBudget(manager).tokens(base) > threshold, "the history alone is over"

    start = len(provider.sent)
    last = (await _follow(manager, first, "Check the four parts agree.", turn_id="t9"))["run_id"]
    record = await _settle(manager, last)
    assert record.outcome == "ok" and record.result_preview == "All four parts are consistent."
    sent = provider.sent[start]
    assert provider.summary_calls, "the earlier turns were summarized"
    assert is_summary_message(sent[1]) and sent[0] == provider.sent[0][0]
    assert sent[-1] == {"role": "user", "content": f"{TASK_LABEL}\nCheck the four parts agree."}
    assert CompactionServiceBudget(manager).tokens(sent) < CompactionServiceBudget(manager).tokens(base)
    assert record.usage["input"] >= 100 + 40  # the summary's tokens count toward the task
    strict_provider_check(sent)
    # What the summary kept of the earlier turns is theirs, in order, before the task.
    kept_before = sent[2:-1]
    assert kept_before == [m for m in base if m in kept_before]
    # Exact replay on top of the history the task started from.
    for index in range(start, len(provider.sent)):
        state = restore(provider.logs[index], base)
        assert not isinstance(state, ResumeRefusal), state
        assert state.messages == provider.sent[index], f"call {index + 1} differs"
    # Kept for the next task, which starts from it.
    after = manager.registry.thread_store.turn_context(first).history
    assert is_summary_message(after[0]) and after[-1] == {
        "role": "assistant", "content": "All four parts are consistent."}
    assert CompactionServiceBudget(manager).tokens(after) < threshold
    rows = manager.registry.thread_store.display_rows(first)
    assert [r.get("turn") for r in rows if r.get("kind") == "subagent_task"] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_a_kept_summary_keeps_saved_rows_active_after_a_stopped_turn(tmp_path, home):
    """A stopped turn's closing row is said differently to the model; the rows
    the summary kept are still the saved ones (active in the display log), not
    copies, and the next task's history has them."""
    from flowly.session.archive import EVENT_ID_KEY

    provider = _Scripted([LLMResponse(content="never used"), LLMResponse(content="Part two done.")])
    gate = provider.gate(1)
    roomy = _manager(home, tmp_path, provider, window=64_000)
    first = await _spawn(roomy, context=None, task="Part one.")
    await provider.waiting[1].wait()
    await roomy.cancel(first)
    gate.set()
    await _settle(roomy, first)
    await _settle(roomy, (await _follow(roomy, first, "Part two."))["run_id"])
    base = roomy.registry.thread_store.turn_context(first).history
    assert {"role": "assistant", "content": STOPPED_REPLY_TEXT} in base

    thread = roomy.registry.get_thread(first)
    store = roomy.registry.thread_store
    key = thread_session_key(first)
    saved = store.sessions.read(key).messages
    third = "33333333-3333-4333-8333-333333333333"

    def add_turn(session: Any) -> None:
        session.metadata["subagent"]["runs"].append(third)
        session.add_message("user", "[Task from the main agent]\nPart three.", kind="subagent_task")
        session.add_message("assistant", "Part three done.")

    store.sessions.mutate(key, add_turn)
    store.sessions.evict(key)
    kept = [*base, {"role": "user", "content": "[Task from the main agent]\nPart three."},
            {"role": "assistant", "content": "Part three done."}]
    summary = {"role": "system", "content": "[Previous conversation summary]\n\nEarlier parts."}
    assert store.persist_context(thread, run_id=third, summary=summary, kept=kept)
    after = store.sessions.read(key)
    saved_ids = {row.get(EVENT_ID_KEY) for row in saved}
    kept_rows = after.messages[1:]
    # Every kept message that is a saved row is that row (same identity, its fields).
    reused = [row for row in kept_rows if row.get(EVENT_ID_KEY) in saved_ids]
    assert len(reused) == len(saved) - 1  # all but the stopped closing row, said differently now
    assert any(row.get("kind") == "subagent_task" and row.get("turn") == 2 for row in reused)
    derived = [row for row in kept_rows if row.get("_archive_derived_copy")]
    assert [row["content"] for row in derived] == [STOPPED_REPLY_TEXT]
    assert store.turn_context(first).history[1:] == kept
    states = store.sessions.get_archive_snapshot(key).by_id
    assert all(states[row[EVENT_ID_KEY]].state == "active" for row in reused if row[EVENT_ID_KEY] in states)


class CompactionServiceBudget:
    def __init__(self, manager: SubagentManager) -> None:
        self.requests = manager._requests

    def tokens(self, messages: list[dict[str, Any]]) -> int:
        return self.requests.budget(messages, None, MODEL).messages_tokens


# ── Restarts mid follow-up ───────────────────────────────────────────────────


DEAD = {"instance": "a-stopped-process", "pid": 0, "identity": None, "boot": None, "host": "h"}


def _dead_owner() -> dict[str, Any]:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return dict(DEAD, pid=child.pid)


def _upto(records: list[dict[str, Any]], kind: str, call_id: str | None = None) -> list[dict[str, Any]]:
    for index, record in enumerate(records):
        if record["type"] == kind and (call_id is None or record.get("callId") == call_id):
            return records[: index + 1]
    raise AssertionError(f"no {kind} in the checkpoint")


async def _reference_followup(tmp_path: Path, home: Path, monkeypatch):
    """Turn 1, a copy of the home as it stood idle, then a follow-up turn with
    three calls run to its end; returns (home copy, its whole checkpoint, its record)."""
    (tmp_path / "notes.txt").write_text("notes")
    kept: dict[str, list[dict[str, Any]]] = {}
    real_append = SubagentCheckpoint.append
    provider = _Scripted([LLMResponse(content="Turn one done.")])
    manager = _manager(home, tmp_path, provider)
    first = await _spawn(manager)
    await _settle(manager, first)
    manager.mark_delivered(CHAT, [first])  # its report was handled
    idle_home = tmp_path / "idle-home"
    shutil.copytree(home, idle_home)

    def keep(self: SubagentCheckpoint, kind: str, **kwargs: Any) -> None:
        real_append(self, kind, **kwargs)
        kept["records"] = read_checkpoint(self.path)

    monkeypatch.setattr(SubagentCheckpoint, "append", keep)
    provider.script = [
        LLMResponse(content="Working on it.", tool_calls=[
            _call("c1", "write_file", path=str(tmp_path / "draft.md"), content="W1"),
            _call("c2", "exec", command="echo E1"),
            _call("c3", "read_file", path=str(tmp_path / "notes.txt")),
        ], usage={"prompt_tokens": 100, "completion_tokens": 20}),
        LLMResponse(content="Turn two done.", usage={"prompt_tokens": 150, "completion_tokens": 10}),
    ]
    second = (await _follow(manager, first, "Draft it."))["run_id"]
    record = await _settle(manager, second)
    monkeypatch.setattr(SubagentCheckpoint, "append", real_append)
    from flowly.agent import subagent_owner
    subagent_owner.forget_instance(manager._instance)
    return idle_home, kept["records"], record


def _crash_followup(idle_home: Path, reference: SubagentRunRecord, prefix: list[dict[str, Any]]) -> None:
    """The follow-up registered on the idle copy, as a stopped process left it at ``prefix``."""
    registry = SubagentRegistry(idle_home / "subagents" / "runs.json")
    usage: dict[str, int] = {}
    for line in prefix:
        if line["type"] == "model":
            usage = add_usage(usage, line.get("usage") or {})
    now = time.time()
    record = SubagentRunRecord(
        run_id=reference.run_id, child_session_key=reference.child_session_key,
        parent_session_key=CHAT, parent_channel="desktop", parent_chat_id="chat-1",
        task=reference.task, label=reference.label, display_name=reference.display_name,
        model=MODEL, cleanup="keep", created_at=now - 60, started_at=now - 50, usage=usage,
        origin="agent", mark=reference.mark, batch_id=reference.batch_id, owner=_dead_owner(),
        resumable=True, thread_id=reference.thread_id, turn=2, trigger="followup",
    )
    registry.register(record)
    path = registry.checkpoint_path(record.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in prefix), encoding="utf-8")


@pytest.mark.asyncio
async def test_a_restart_mid_followup_continues_on_the_thread_and_runs_no_call_twice(
    tmp_path, home, monkeypatch, side_effects,
):
    idle_home, full, reference = await _reference_followup(tmp_path, home, monkeypatch)
    assert full[0]["baseCount"] > 0
    side_effects.write_text("")
    prefix = _upto(full, "tool_start", "c2")  # mid exec
    _crash_followup(idle_home, reference, prefix)
    monkeypatch.setenv("FLOWLY_HOME", str(idle_home))

    bus = MessageBus()
    provider = _Scripted(default="Turn two finished after the restart.")
    manager = _manager(idle_home, tmp_path, provider, bus=bus)
    base = manager.registry.thread_store.turn_context(reference.thread_id).history
    manager.resume_pending()
    record = await _settle(manager, reference.run_id)

    assert side_effects.read_text() == ""  # nothing ran again
    assert record.outcome == "ok" and record.turn == 2
    restored = restore(prefix, base)
    sent = provider.sent[0]
    assert sent[: len(restored.messages)] == restored.messages
    assert sent[1: 1 + len(base)] == base  # the thread's history, then the turn
    answers = sent[len(restored.messages):]
    assert [(m["tool_call_id"], m["status"] if "status" in m else None) for m in answers] == [
        ("c2", None), ("c3", None)]
    assert "may have finished, been cut off, or still be running" in answers[0]["content"]
    assert answers[1]["content"] == NOT_RUN_TEXT
    strict_provider_check(sent)
    notices = await _notices(bus)
    assert len(notices) == 1 and "continued after a restart, task 2" in notices[0]
    # Saved once, as the thread's second turn.
    rows = manager.registry.thread_store.display_rows(reference.thread_id)
    assert [r.get("turn") for r in rows if r.get("kind") == "subagent_task"] == [1, 2]
    assert manager.registry.thread_store.committed_runs(reference.thread_id) == [
        reference.thread_id, reference.run_id]


@pytest.mark.asyncio
async def test_a_followup_whose_history_changed_is_not_continued(tmp_path, home, monkeypatch, side_effects):
    idle_home, full, reference = await _reference_followup(tmp_path, home, monkeypatch)
    _crash_followup(idle_home, reference, _upto(full, "tool", "c1"))
    monkeypatch.setenv("FLOWLY_HOME", str(idle_home))
    registry = SubagentRegistry(idle_home / "subagents" / "runs.json")
    key = thread_session_key(reference.thread_id)

    def edit(session: Any) -> None:
        session.messages[-1]["content"] = "Someone changed this report."

    registry.thread_store.sessions.mutate(key, edit)
    bus = MessageBus()
    provider = _Scripted()
    manager = _manager(idle_home, tmp_path, provider, bus=bus)
    manager.resume_pending()
    notices = await _notices(bus)
    record = manager.registry.get(reference.run_id)
    assert provider.sent == [] and record.outcome == "interrupted"
    assert record.error_code == "resume_unsupported"
    assert len(notices) == 1 and "no longer matches the one this task started from" in notices[0]
    # The stopped turn still takes its place in the thread.
    rows = manager.registry.thread_store.display_rows(reference.thread_id)
    assert [r.get("turn") for r in rows if r.get("kind") == "subagent_task"] == [1, 2]


@pytest.mark.asyncio
async def test_a_crash_after_the_turn_was_saved_records_it_with_no_model_call(
    tmp_path, home, monkeypatch, side_effects,
):
    idle_home, full, reference = await _reference_followup(tmp_path, home, monkeypatch)
    side_effects.write_text("")
    _crash_followup(idle_home, reference, _upto(full, "final"))
    monkeypatch.setenv("FLOWLY_HOME", str(idle_home))
    # The turn was saved just before the crash (the commit precedes finish).
    registry = SubagentRegistry(idle_home / "subagents" / "runs.json")
    state = restore(_upto(full, "final"), registry.thread_store.turn_context(reference.thread_id).history)
    registry.thread_store.commit_turn(
        registry.get_thread(reference.thread_id), run_id=reference.run_id, turn=2, trigger="followup",
        system_prompt=state.system_prompt, first_message=state.first_message, brief=reference.task,
        journal=state.journal, result="Turn two done.", outcome="ok", usage={}, duration_ms=1, aborted=False,
    )
    bus = MessageBus()
    provider = _Scripted()
    manager = _manager(idle_home, tmp_path, provider, bus=bus)
    manager.resume_pending()
    record = await _settle(manager, reference.run_id)
    assert provider.sent == [] and side_effects.read_text() == ""
    assert record.outcome == "ok"
    notices = await _notices(bus)
    assert len(notices) == 1 and "Turn two done." in notices[0]
    rows = manager.registry.thread_store.display_rows(reference.thread_id)
    assert [r.get("turn") for r in rows if r.get("kind") == "subagent_task"] == [1, 2]


def test_a_version_two_checkpoint_continues_as_a_first_turn():
    v2 = [
        {"v": 2, "n": 0, "t": 1.0, "type": "header", "runId": "r", "coreVersion": "x", "model": MODEL,
         "label": "l", "maxIterations": 10, "systemPrompt": "SYS", "firstMessage": "TASK", "tools": []},
        {"v": 2, "n": 1, "t": 1.0, "type": "step", "iteration": 1, "notes": [], "mail": [], "strip": []},
        {"v": 3, "n": 2, "t": 1.0, "type": "resumed", "after": "crash", "attempt": 1},
    ]
    state = restore(v2)
    assert not isinstance(state, ResumeRefusal) and state.base_count == 0
    assert state.messages == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "TASK"}]
    assert restore([dict(v2[0], v=1)]).reason == "version"
    assert restore([dict(v2[0], v=4)]).reason == "version"
    # A v3 header naming history needs that history, digest and all.
    header = dict(v2[0], v=3, baseCount=1, baseDigest=history_digest([{"role": "user", "content": "h"}]))
    assert restore([header]).reason == "base"
    assert restore([header], [{"role": "user", "content": "other"}]).reason == "base"
    rebuilt = restore([header], [{"role": "user", "content": "h"}, {"role": "assistant", "content": "later"}])
    assert rebuilt.messages == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "h"},
                                {"role": "user", "content": "TASK"}]


# ── A follow-up turn streams ─────────────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def subagent_stream_watched(self, session_key: str) -> bool:
        return True

    def prepare_subagent_stream(self, event: str, session_key: str, data: dict[str, Any]):
        frame = (event, session_key, json.loads(json.dumps(data)))

        async def deliver() -> None:
            self.events.append(frame)

        return deliver


@pytest.mark.asyncio
async def test_a_followup_turn_streams_like_any_turn(tmp_path, home):
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Scripted([LLMResponse(content="First."), LLMResponse(content="Second.")])
    manager = _manager(home, tmp_path, provider, sink=hub)
    first = await _spawn(manager)
    await _settle(manager, first)
    await hub.drained()
    recorder.events.clear()
    second = (await _follow(manager, first, "Again, for B."))["run_id"]
    await _settle(manager, second)
    await hub.drained()
    key = f"subagent:{first}"
    assert [(e, d.get("state")) for e, _k, d in recorder.events] == [
        ("chat", "run_started"), ("agent", None), ("chat", "final")]
    for _event, session_key, data in recorder.events:
        assert session_key == key and data["sessionKey"] == key and data["runId"] == second
        assert data["turnOrigin"] == SUBAGENT_TURN_ORIGIN
    started = recorder.events[0][2]
    assert (started["trigger"], started["turn"], started["threadId"]) == ("followup", 2, first)
    assert started["prompt"] == "Again, for B." and started["withConversation"] is False
    assert recorder.events[-1][2]["message"]["content"] == [{"type": "text", "text": "Second."}]
    assert inflight.get(key) is None
