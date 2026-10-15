"""Subagents continue after a restart, and never run a tool call twice.

Crash points are simulated the way a restart finds them: a reference run is
taken to its end with its whole checkpoint kept, then for each point of the
design's crash list the checkpoint up to that point and the registry record
as it stood are written into a fresh home whose owner process is gone, and
startup recovery runs in a fresh manager. Every case checks that no tool
ran again, what the provider was sent on continuing (the rebuilt list plus
a factual result for each call left without one), one notice, a ``resumed``
transcript item, cumulative tokens, one metric line and no checkpoint left.

Then: a clean shutdown suspends instead of finishing, two processes
recovering at once continue a run once, a checkpoint of another format ends
the run with a notice, a crash loop stops after three continues, a long
downtime ends the run with a notice, a batch's held result waits for its
continued sibling, a goal keeps waiting, unread messages come back, and an
orphan can be cancelled.
"""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from flowly.agent import subagent as subagent_module
from flowly.agent import subagent_owner
from flowly.agent.subagent import (
    SubagentManager,
    _strip_subagent_tool_results,
    _trim_to_context_budget,
)
from flowly.agent.subagent_checkpoint import (
    GRACE_PROMPT,
    NOT_RUN_TEXT,
    SubagentCheckpoint,
    read_checkpoint,
    restore_child_state,
)
from flowly.agent.subagent_metrics import metrics_path, read_metrics
from flowly.agent.subagent_observation import run_view
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord, add_usage
from flowly.agent.tools.discovery import annotate_search_repetition
from flowly.agent.tools.filesystem import WriteFileTool
from flowly.agent.tools.shell import ExecTool
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

CHAT = "web:chat-1"
MODEL = "m"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


@pytest.fixture
def side_effects(tmp_path, monkeypatch) -> Path:
    """write_file and exec only note that they ran, in side_effects.log."""
    log = tmp_path / "side_effects.log"

    async def write(self, path: str, content: str, **kwargs: Any) -> str:
        with log.open("a") as stream:
            stream.write(f"write {content}\n")
        return f"Wrote {path}"

    async def run(self, command: str, **kwargs: Any) -> str:
        with log.open("a") as stream:
            stream.write(f"exec {command}\n")
        return "exit code 0"

    monkeypatch.setattr(WriteFileTool, "execute", write)
    monkeypatch.setattr(ExecTool, "execute", run)
    return log


class _Scripted(LLMProvider):
    """Answers from a script (a default when it runs out) and keeps what it was sent."""

    def __init__(self, script: list[Any] | None = None, default: str = "Done after the restart.") -> None:
        super().__init__(api_key="unused")
        self.script = list(script or [])
        self.default = default
        self.sent: list[list[dict[str, Any]]] = []
        self.tools_offered: list[Any] = []

    def get_default_model(self) -> str:
        return MODEL

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.sent.append(copy.deepcopy(kwargs["messages"]))
        self.tools_offered.append(kwargs.get("tools"))
        step = self.script.pop(0) if self.script else LLMResponse(
            content=self.default, usage={"prompt_tokens": 7, "completion_tokens": 3})
        if callable(step):
            step = await step(self)
        return step


def _manager(home: Path, workspace: Path, provider: LLMProvider, bus: MessageBus | None = None,
             *, resume: bool = True, max_iterations: int = 30) -> SubagentManager:
    manager = SubagentManager(provider=provider, workspace=workspace, bus=bus or MessageBus(),
                              registry=SubagentRegistry(home / "runs.json"), state_dir=workspace,
                              max_iterations=max_iterations)
    manager.resume_enabled = resume
    return manager


def restore(records: list[dict[str, Any]]):
    return restore_child_state(
        records, strip=_strip_subagent_tool_results,
        trim=lambda messages, *, budget: _trim_to_context_budget(messages, MODEL, budget=budget),
        annotate=annotate_search_repetition,
    )


def _call(call_id: str, name: str, **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


async def _settle(manager: SubagentManager, run_id: str, rounds: int = 300) -> SubagentRunRecord:
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        record = manager.registry.get(run_id)
        if record is not None and record.ended_at is not None and not manager._running_tasks:
            return record
    return manager.registry.get(run_id)


async def _notices(bus: MessageBus, wait: float = 0.4) -> list[str]:
    found = []
    while True:
        try:
            found.append((await asyncio.wait_for(bus.consume_inbound(), wait)).content)
        except asyncio.TimeoutError:
            return found


DEAD = {"instance": "a-stopped-process", "pid": 0, "identity": None, "boot": None, "host": "h"}


def _dead_owner() -> dict[str, Any]:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return dict(DEAD, pid=child.pid)


# ── A reference run, and the states a restart finds ──────────────────────────

async def _reference(tmp_path: Path, monkeypatch, script: list[Any], *, max_iterations: int = 30,
                     task: str = "Prepare the report") -> tuple[list[dict[str, Any]], SubagentRunRecord]:
    """Run a child to its end; return its whole checkpoint and its record."""
    kept: dict[str, list[dict[str, Any]]] = {}
    real_append = SubagentCheckpoint.append

    def keep(self: SubagentCheckpoint, kind: str, **kwargs: Any) -> None:
        real_append(self, kind, **kwargs)
        kept["records"] = read_checkpoint(self.path)

    monkeypatch.setattr(SubagentCheckpoint, "append", keep)
    provider = _Scripted(script)
    manager = _manager(tmp_path / "reference", tmp_path, provider, max_iterations=max_iterations)
    started = json.loads(await manager.spawn(task=task, origin_channel="web", origin_chat_id="chat-1",
                                             parent_session_key=CHAT, origin="agent",
                                             context="owner: prepare it\nagent: starting"))
    record = await _settle(manager, started["run_id"])
    monkeypatch.setattr(SubagentCheckpoint, "append", real_append)
    assert record.ended_at is not None and kept.get("records")
    subagent_owner.forget_instance(manager._instance)
    return kept["records"], record


def _upto(records: list[dict[str, Any]], kind: str, call_id: str | None = None, nth: int = 1) -> list[dict[str, Any]]:
    seen = 0
    for index, record in enumerate(records):
        if record["type"] == kind and (call_id is None or record.get("callId") == call_id):
            seen += 1
            if seen == nth:
                return records[: index + 1]
    raise AssertionError(f"no {kind} {call_id or ''} in the checkpoint")


def _crash_state(home: Path, reference: SubagentRunRecord, prefix: list[dict[str, Any]], *,
                 suspended_ago: float | None = None, last_seen_ago: float = 5.0,
                 resume_count: int = 0, owner: dict[str, Any] | None = None) -> SubagentRunRecord:
    """The registry and checkpoint as a stopped process left them at ``prefix``."""
    registry = SubagentRegistry(home / "runs.json")
    answered = {r["callId"] for r in prefix if r["type"] == "tool"}
    trace = [
        {"tool": r["name"], "status": "ok" if r["callId"] in answered else "running",
         "args_bytes": 1, "result_bytes": 0, "duration_ms": 0, "started_at": r["t"]}
        for r in prefix if r["type"] == "tool_start"
    ]
    usage: dict[str, int] = {}
    for record in prefix:
        if record["type"] == "model":
            usage = add_usage(usage, record.get("usage") or {})
    now = time.time()
    record = SubagentRunRecord(
        run_id=reference.run_id, child_session_key=reference.child_session_key,
        parent_session_key=CHAT, parent_channel="web", parent_chat_id="chat-1",
        task=reference.task, label=reference.label, display_name=reference.display_name,
        model=MODEL, cleanup="keep", created_at=now - 600, started_at=now - 590,
        activity={"phase": "tool"}, tool_trace=trace, usage=usage, origin="agent",
        mark=reference.mark, batch_id=reference.batch_id, owner=owner or _dead_owner(),
        resumable=True, resume_count=resume_count,
        suspended_at=(now - suspended_ago) if suspended_ago is not None else None,
    )
    registry.register(record)
    registry._runs[record.run_id].updated_at = now - last_seen_ago
    with registry._lock:
        registry._write_locked()
    path = registry.checkpoint_path(record.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in prefix), encoding="utf-8")
    return registry.get(record.run_id)


def _three_calls(tmp_path: Path) -> list[Any]:
    return [
        LLMResponse(content="Working on it.", tool_calls=[
            _call("c1", "write_file", path=str(tmp_path / "draft.md"), content="W1"),
            _call("c2", "exec", command="echo E1"),
            _call("c3", "read_file", path=str(tmp_path / "notes.txt")),
        ], usage={"prompt_tokens": 100, "completion_tokens": 20}),
        LLMResponse(content="Done.", usage={"prompt_tokens": 150, "completion_tokens": 10}),
    ]


def _expected(restored_messages: list[dict[str, Any]], answers: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return restored_messages + [
        {"role": "tool", "tool_call_id": cid, "name": name, "content": text} for cid, name, text in answers
    ]


WRITE_TEXT = "Interrupted: Flowly restarted while this was writing {path}."
EXEC_CRASH = "It may have finished, been cut off, or still be running"
EXEC_STOPPED = "was stopped when Flowly shut down"


CRASH_POINTS = {
    # name: (prefix, expected answers for the open calls: (id, tool, text fragment))
    "after_header": (lambda r: _upto(r, "header"), []),
    "during_model_call": (lambda r: _upto(r, "step"), []),
    "after_model_record": (lambda r: _upto(r, "model"),
                           [("c1", "write_file", NOT_RUN_TEXT), ("c2", "exec", NOT_RUN_TEXT),
                            ("c3", "read_file", NOT_RUN_TEXT)]),
    "after_tool_start": (lambda r: _upto(r, "tool_start", "c1"),
                         [("c1", "write_file", "Interrupted: Flowly restarted while this was writing"),
                          ("c2", "exec", NOT_RUN_TEXT), ("c3", "read_file", NOT_RUN_TEXT)]),
    "after_tool_record": (lambda r: _upto(r, "tool", "c1"),
                          [("c2", "exec", NOT_RUN_TEXT), ("c3", "read_file", NOT_RUN_TEXT)]),
    "mid_exec": (lambda r: _upto(r, "tool_start", "c2"),
                 [("c2", "exec", EXEC_CRASH), ("c3", "read_file", NOT_RUN_TEXT)]),
    "after_last_tool": (lambda r: _upto(r, "tool", "c3"), []),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("point", sorted(CRASH_POINTS))
async def test_a_run_continues_from_each_crash_point_and_runs_no_call_twice(
    tmp_path, monkeypatch, side_effects, point,
):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    side_effects.write_text("")  # what the reference run did
    cut, answers = CRASH_POINTS[point]
    prefix = cut(full)
    home = tmp_path / "home2"
    before = _crash_state(home, reference, prefix)
    usage_before = dict(before.usage)

    bus = MessageBus()
    provider = _Scripted()
    manager = _manager(home, tmp_path, provider, bus)
    manager.resume_pending()
    record = await _settle(manager, reference.run_id)

    # Nothing ran again: the continued run only asked the model.
    assert side_effects.read_text() == ""
    assert record.outcome == "ok" and record.result_available
    restored = restore(prefix)
    sent = provider.sent[0]
    assert sent[: len(restored.messages)] == restored.messages
    synthetic = sent[len(restored.messages):]
    assert [(m["tool_call_id"], m["name"]) for m in synthetic] == [(cid, name) for cid, name, _ in answers]
    for message, (_, _, fragment) in zip(synthetic, answers):
        assert fragment in message["content"], message["content"]
    if point == "after_tool_start":
        assert str(tmp_path / "draft.md") in synthetic[0]["content"]
    assert len(provider.sent) == 1
    # One notice, saying it continued.
    notices = await _notices(bus)
    assert len(notices) == 1 and "Done after the restart." in notices[0]
    assert "continued after a restart" in notices[0]
    # The transcript shows the restart and the calls it cut off.
    items = manager.registry.read_transcript(reference.run_id)["items"]
    assert [i["seq"] for i in items] == list(range(1, len(items) + 1))
    resumed = [i for i in items if i["kind"] == "resumed"]
    assert len(resumed) == 1 and resumed[0]["attempt"] == 1 and resumed[0]["after"] == "crash"
    cut_off = [(i["toolCallId"], i["status"]) for i in items if i["kind"] == "tool"]
    assert cut_off == [(cid, "not_run" if text == NOT_RUN_TEXT else "interrupted") for cid, _, text in answers]
    # Tokens keep counting; the trace has no running step left.
    assert record.usage["input"] == usage_before.get("input", 0) + 7
    assert record.usage["output"] == usage_before.get("output", 0) + 3
    assert all(step["status"] != "running" for step in record.tool_trace)
    # One metric line, no checkpoint left.
    assert [row["runId"] for row in read_metrics(metrics_path(home / "runs.json"))] == [reference.run_id]
    assert not manager.registry.checkpoint_path(reference.run_id).exists()


@pytest.mark.asyncio
async def test_a_result_decided_before_the_crash_is_recorded_without_a_model_call(
    tmp_path, monkeypatch, side_effects,
):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    side_effects.write_text("")
    home = tmp_path / "home2"
    _crash_state(home, reference, _upto(full, "final"))
    bus = MessageBus()
    provider = _Scripted()
    manager = _manager(home, tmp_path, provider, bus)
    manager.resume_pending()
    record = await _settle(manager, reference.run_id)
    assert provider.sent == [] and side_effects.read_text() == ""
    assert record.outcome == "ok"
    assert manager.registry.read_result(reference.run_id)["content"] == "Done."
    notices = await _notices(bus)
    assert len(notices) == 1 and "Done." in notices[0]


@pytest.mark.asyncio
async def test_a_run_stopped_before_its_summary_call_makes_only_that_call(tmp_path, monkeypatch, side_effects):
    script = [LLMResponse(content=None, tool_calls=[_call("c1", "exec", command="echo once")])]
    full, reference = await _reference(tmp_path, monkeypatch, script, max_iterations=1)
    assert any(r["type"] == "phase" for r in full)
    side_effects.write_text("")
    home = tmp_path / "home2"
    prefix = _upto(full, "phase")
    _crash_state(home, reference, prefix)
    provider = _Scripted(default="Summary after the restart.")
    manager = _manager(home, tmp_path, provider, max_iterations=1)
    manager.resume_pending()
    record = await _settle(manager, reference.run_id)
    assert provider.tools_offered == [None]  # the summary call, without tools
    assert provider.sent[0] == restore(prefix).messages
    assert provider.sent[0][-1] == {"role": "user", "content": GRACE_PROMPT}
    assert record.outcome == "ok" and side_effects.read_text() == ""


@pytest.mark.asyncio
async def test_a_finished_run_whose_checkpoint_was_left_is_only_reported(tmp_path, monkeypatch, side_effects):
    """Crash after the registry recorded the result, before the checkpoint was deleted."""
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home = tmp_path / "home2"
    _crash_state(home, reference, full)
    registry = SubagentRegistry(home / "runs.json")
    path = registry.checkpoint_path(reference.run_id)
    registry._runs[reference.run_id].ended_at = time.time()
    registry._runs[reference.run_id].outcome = "ok"
    registry._atomic_write(registry._result_path(reference.run_id), b"Done.")
    registry._runs[reference.run_id].result_available = True
    registry._runs[reference.run_id].result_chars = 5
    with registry._lock:
        registry._write_locked()  # the prune removes the stale checkpoint
    assert not path.exists()
    path.write_text("".join(json.dumps(r) + "\n" for r in full))  # …or a crash kept it
    bus = MessageBus()
    provider = _Scripted()
    manager = _manager(home, tmp_path, provider, bus)
    manager.resume_pending()
    notices = await _notices(bus)
    assert provider.sent == [] and len(notices) == 1 and "Done." in notices[0]
    assert not path.exists()


# ── Clean shutdown suspends ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_clean_shutdown_suspends_and_the_next_start_continues(tmp_path, monkeypatch):
    started_exec = asyncio.Event()
    ran: list[str] = []

    async def slow_exec(self, command: str, **kwargs: Any) -> str:
        ran.append(command)
        started_exec.set()
        await asyncio.Event().wait()  # cancelled by the shutdown
        return "never"

    monkeypatch.setattr(ExecTool, "execute", slow_exec)
    home = tmp_path / "h"
    first_bus = MessageBus()
    first_provider = _Scripted([LLMResponse(content=None, tool_calls=[
        _call("c1", "exec", command="make build"), _call("c2", "read_file", path=str(tmp_path / "x")),
    ], usage={"prompt_tokens": 50, "completion_tokens": 5})])
    first = _manager(home, tmp_path, first_provider, first_bus)
    started = json.loads(await first.spawn(task="Build it", origin_channel="web", origin_chat_id="chat-1",
                                           parent_session_key=CHAT, origin="agent"))
    run_id = started["run_id"]
    await asyncio.wait_for(started_exec.wait(), 3)
    assert await first.shutdown(wait_seconds=1.5) == 1

    record = SubagentRegistry(home / "runs.json").get(run_id)
    assert record.ended_at is None and record.suspended_at is not None
    assert record.activity == {"phase": "suspended", "reason": "shutdown"}
    assert [r["type"] for r in read_checkpoint(first.registry.checkpoint_path(run_id))][-1] == "suspended"
    assert await _notices(first_bus, 0.2) == []
    assert read_metrics(metrics_path(home / "runs.json")) == []
    assert first.pending_for_goal(CHAT) == [record.display_name]

    subagent_owner.forget_instance(first._instance)  # the process exits
    bus = MessageBus()
    provider = _Scripted()
    second = _manager(home, tmp_path, provider, bus)
    second.resume_pending()
    assert second.registry.get(run_id).activity == {"phase": "resuming"}
    assert second.pending_for_goal(CHAT) == [record.display_name]  # the goal keeps waiting
    done = await _settle(second, run_id)
    assert ran == ["make build"]  # never again
    tool_messages = [m for m in provider.sent[0] if m.get("role") == "tool"]
    assert EXEC_STOPPED in tool_messages[0]["content"] and tool_messages[1]["content"] == NOT_RUN_TEXT
    assert done.outcome == "ok" and done.suspended_seconds >= 0 and done.resumed_at is not None
    assert done.usage["input"] == 57
    items = second.registry.read_transcript(run_id)["items"]
    assert [i["after"] for i in items if i["kind"] == "resumed"] == ["shutdown"]
    assert len(await _notices(bus)) == 1


@pytest.mark.asyncio
async def test_shutdown_interrupts_what_is_not_continued_and_refuses_new_runs(tmp_path):
    gate = asyncio.Event()

    class _Waits(_Scripted):
        async def chat(self, **kwargs: Any) -> LLMResponse:
            await gate.wait()
            return LLMResponse(content="x")

    manager = _manager(tmp_path / "h", tmp_path, _Waits())
    await manager.spawn(task="bookkeeping", origin_channel="web", origin_chat_id="chat-1",
                        parent_session_key=CHAT, origin="system")
    await asyncio.sleep(0.05)
    await manager.shutdown(wait_seconds=1.0)
    record = manager.registry.all()[0]
    assert record.outcome == "interrupted" and record.delivery_state == "not_required"
    refused = json.loads(await manager.spawn(task="more", origin_channel="web", origin_chat_id="chat-1",
                                             parent_session_key=CHAT, origin="agent"))
    assert refused["status"] == "rejected"
    assert await manager.shutdown() == 0


@pytest.mark.asyncio
async def test_a_run_that_cannot_be_continued_is_reported_after_the_shutdown(tmp_path):
    """Its checkpoint could not be kept: the shutdown interrupts it, the next start says so."""
    gate = asyncio.Event()

    class _Waits(_Scripted):
        async def chat(self, **kwargs: Any) -> LLMResponse:
            await gate.wait()
            return LLMResponse(content="x")

    home = tmp_path / "h"
    manager = _manager(home, tmp_path, _Waits())
    started = json.loads(await manager.spawn(task="Long task", origin_channel="web", origin_chat_id="chat-1",
                                             parent_session_key=CHAT, origin="agent"))
    await asyncio.sleep(0.05)
    manager._log(started["run_id"], "step", too_big="x" * (17 * 1024 * 1024))  # the cap drops it
    assert manager.registry.get(started["run_id"]).resumable is False
    await manager.shutdown(wait_seconds=1.0)
    record = manager.registry.get(started["run_id"])
    assert record.outcome == "interrupted" and record.error_code == "shutdown"
    assert record.delivery_state != "not_required"
    subagent_owner.forget_instance(manager._instance)
    bus = MessageBus()
    _manager(home, tmp_path, _Scripted(), bus).resume_pending()
    notices = await _notices(bus)
    assert len(notices) == 1 and "Flowly shut down while this task ran" in notices[0]


def test_the_gateway_continues_runs_and_the_cli_does_not():
    import inspect

    from flowly.cli import agent_cmd, gateway_cmd

    assert "agent.subagents.resume_enabled = True" in inspect.getsource(gateway_cmd)
    assert "resume_enabled" not in inspect.getsource(agent_cmd)


# ── Who continues it, and when not ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_of_two_processes_recovering_at_once_one_continues_the_run(tmp_path, monkeypatch, side_effects):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home = tmp_path / "home2"
    _crash_state(home, reference, _upto(full, "model"))
    buses, providers = [MessageBus(), MessageBus()], [_Scripted(), _Scripted()]
    managers = [_manager(home, tmp_path, p, b) for p, b in zip(providers, buses)]
    for manager in managers:
        manager.resume_pending()
    for manager in managers:
        await _settle(manager, reference.run_id)
    assert sorted(len(p.sent) for p in providers) == [0, 1]
    counts = [len(await _notices(bus)) for bus in buses]
    assert sorted(counts) == [0, 1]


@pytest.mark.asyncio
async def test_a_process_that_does_not_continue_runs_leaves_them_to_the_gateway(tmp_path, monkeypatch, side_effects):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home = tmp_path / "home2"
    _crash_state(home, reference, _upto(full, "model"))
    cli = _manager(home, tmp_path, _Scripted(), resume=False)
    cli.resume_pending()
    record = cli.registry.get(reference.run_id)
    assert record.ended_at is None and record.owner["instance"] == "a-stopped-process"


@pytest.mark.asyncio
async def test_a_checkpoint_of_another_format_ends_the_run_with_a_notice(tmp_path, monkeypatch, side_effects):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home = tmp_path / "home2"
    newer = [dict(r, v=2) for r in _upto(full, "model")]
    _crash_state(home, reference, newer)
    bus = MessageBus()
    provider = _Scripted()
    manager = _manager(home, tmp_path, provider, bus)
    manager.resume_pending()
    notices = await _notices(bus)
    record = manager.registry.get(reference.run_id)
    assert provider.sent == []
    assert record.outcome == "interrupted" and record.error_code == "resume_unsupported"
    assert len(notices) == 1 and "cannot continue work saved by the other one" in notices[0]
    assert "interrupted" in notices[0].splitlines()[0]


@pytest.mark.asyncio
async def test_a_run_without_a_checkpoint_fails_as_before(tmp_path):
    home = tmp_path / "h"
    registry = SubagentRegistry(home / "runs.json")
    registry.register(SubagentRunRecord(
        run_id="legacy", child_session_key="subagent:legacy", parent_session_key=CHAT,
        parent_channel="web", parent_chat_id="chat-1", task="t", label="t", model=MODEL,
        cleanup="keep", created_at=time.time(), origin="agent"))
    bus = MessageBus()
    manager = _manager(home, tmp_path, _Scripted(), bus)
    manager.resume_pending()
    notices = await _notices(bus)
    assert manager.registry.get("legacy").error_code == "process_restarted"
    assert len(notices) == 1 and "process restarted" in notices[0].lower()


@pytest.mark.asyncio
async def test_after_a_day_down_the_run_ends_and_the_conversation_is_told_once(tmp_path, monkeypatch, side_effects):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home = tmp_path / "home2"
    _crash_state(home, reference, _upto(full, "tool", "c1"), suspended_ago=25 * 3600)
    bus = MessageBus()
    provider = _Scripted()
    manager = _manager(home, tmp_path, provider, bus)
    manager.resume_pending()
    notices = await _notices(bus)
    record = manager.registry.get(reference.run_id)
    assert provider.sent == [] and side_effects.read_text().count("W1") == 1
    assert record.outcome == "interrupted" and record.error_code == "suspended_too_long"
    assert len(notices) == 1 and "Flowly was not running for 1 d 1 h" in notices[0]
    assert record.delivery_state == "queued"
    # Told once: the next start does not tell it again once handled.
    manager.mark_delivered(CHAT, [reference.run_id])
    subagent_owner.forget_instance(manager._instance)
    again = MessageBus()
    _manager(home, tmp_path, _Scripted(), again).resume_pending()
    assert await _notices(again) == []
    assert not manager.registry.checkpoint_path(reference.run_id).exists()


@pytest.mark.asyncio
async def test_a_crash_loop_ends_after_three_continues(tmp_path, monkeypatch):
    """A call that takes the process down every time: three continues, then it ends."""
    home = tmp_path / "h"
    killed = asyncio.Event()

    async def fatal_exec(self, command: str, **kwargs: Any) -> str:
        killed.set()
        await asyncio.Event().wait()  # the process dies here
        return "never"

    monkeypatch.setattr(ExecTool, "execute", fatal_exec)
    always_exec = lambda: _Scripted(  # noqa: E731
        [LLMResponse(content=None, tool_calls=[_call("k", "exec", command="crash")])] * 5)
    first = _manager(home, tmp_path, always_exec())
    started = json.loads(await first.spawn(task="Risky", origin_channel="web", origin_chat_id="chat-1",
                                           parent_session_key=CHAT, origin="agent"))
    run_id = started["run_id"]
    await asyncio.wait_for(killed.wait(), 3)
    stopped = [first]
    for attempt in (1, 2, 3):
        subagent_owner.forget_instance(stopped[-1]._instance)  # SIGKILL
        killed.clear()
        manager = _manager(home, tmp_path, always_exec())
        manager.resume_pending()
        assert manager.registry.get(run_id).resume_count == attempt
        await asyncio.wait_for(killed.wait(), 3)
        stopped.append(manager)
    subagent_owner.forget_instance(stopped[-1]._instance)
    bus = MessageBus()
    last = _manager(home, tmp_path, _Scripted(), bus)
    last.resume_pending()
    notices = await _notices(bus)
    record = last.registry.get(run_id)
    assert record.outcome == "interrupted" and record.error_code == "resume_failed"
    assert len(notices) == 1 and "restarted 3 times" in notices[0]
    last.mark_delivered(CHAT, [run_id])
    assert last.pending_for_goal(CHAT) == []  # the goal goes on
    for manager in stopped:
        for task in manager._running_tasks.values():
            task.cancel()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_finished_step_after_a_restart_starts_the_count_over(tmp_path, monkeypatch, side_effects):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home = tmp_path / "home2"
    _crash_state(home, reference, _upto(full, "model"), resume_count=2)
    gate = asyncio.Event()

    async def hold(provider: _Scripted) -> LLMResponse:
        await gate.wait()
        return LLMResponse(content="ok")

    provider = _Scripted([
        LLMResponse(content=None, tool_calls=[_call("n1", "read_file", path=str(tmp_path / "notes.txt"))]),
        hold,
    ])
    manager = _manager(home, tmp_path, provider)
    manager.resume_pending()
    assert manager.registry.get(reference.run_id).resume_count == 3
    for _ in range(100):
        await asyncio.sleep(0.01)
        if manager.registry.get(reference.run_id).resume_count == 0:
            break
    assert manager.registry.get(reference.run_id).resume_count == 0
    gate.set()
    await _settle(manager, reference.run_id)


# ── Batches, goals, messages, cancelling, the wire ───────────────────────────

@pytest.mark.asyncio
async def test_a_held_result_waits_for_its_continued_sibling_and_they_report_together(
    tmp_path, monkeypatch, side_effects,
):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home = tmp_path / "home2"
    running = _crash_state(home, reference, _upto(full, "tool", "c3"))
    registry = SubagentRegistry(home / "runs.json")
    registry.update(running.run_id, batch_id="turn-1")
    registry.register(SubagentRunRecord(
        run_id="held-sibling", child_session_key="subagent:held", parent_session_key=CHAT,
        parent_channel="web", parent_chat_id="chat-1", task="the other part", label="other",
        display_name="Other part", model=MODEL, cleanup="keep", created_at=time.time() - 900,
        origin="agent", batch_id="turn-1", owner=_dead_owner()))
    registry.finish("held-sibling", "ok", result="Other result.")
    gate = asyncio.Event()

    async def later(provider: _Scripted) -> LLMResponse:
        await gate.wait()
        return LLMResponse(content="Continued result.")

    bus = MessageBus()
    manager = _manager(home, tmp_path, _Scripted([later]), bus)
    manager.resume_pending()
    assert await _notices(bus, 0.3) == []  # held while its sibling continues
    gate.set()
    notices = await _notices(bus, 1.0)
    assert len(notices) == 1
    assert "Other result." in notices[0] and "Continued result." in notices[0]


@pytest.mark.asyncio
async def test_a_held_result_is_delivered_anyway_when_its_continued_sibling_is_slow(
    tmp_path, monkeypatch, side_effects,
):
    monkeypatch.setattr(subagent_module, "_RESUMED_BATCH_HOLD_SECONDS", 0.2)
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home = tmp_path / "home2"
    running = _crash_state(home, reference, _upto(full, "tool", "c3"))
    registry = SubagentRegistry(home / "runs.json")
    registry.update(running.run_id, batch_id="turn-1")
    registry.register(SubagentRunRecord(
        run_id="held-sibling", child_session_key="subagent:held", parent_session_key=CHAT,
        parent_channel="web", parent_chat_id="chat-1", task="the other part", label="other",
        display_name="Other part", model=MODEL, cleanup="keep", created_at=time.time() - 3600,
        origin="agent", batch_id="turn-1", owner=_dead_owner()))
    registry.finish("held-sibling", "ok", result="Other result.")
    registry._runs["held-sibling"].ended_at = time.time() - 3000  # its 300 s passed while down
    with registry._lock:
        registry._write_locked()
    gate = asyncio.Event()

    async def never(provider: _Scripted) -> LLMResponse:
        await gate.wait()
        return LLMResponse(content="late")

    bus = MessageBus()
    manager = _manager(home, tmp_path, _Scripted([never]), bus)
    manager.resume_pending()
    assert await _notices(bus, 0.1) == []  # a fresh moment for the continued sibling
    notices = await _notices(bus, 1.0)
    assert len(notices) == 1 and "Other result." in notices[0]
    assert "Still working, started in the same turn" in notices[0]
    gate.set()
    await _settle(manager, reference.run_id)


@pytest.mark.asyncio
async def test_messages_the_child_had_not_read_come_back(tmp_path, monkeypatch, side_effects):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home = tmp_path / "home2"
    prefix = _upto(full, "tool", "c3") + [
        {"v": 1, "n": 99, "t": time.time(), "type": "mail", "id": "m1", "text": "Use the 2024 numbers."},
    ]
    _crash_state(home, reference, prefix)
    provider = _Scripted()
    manager = _manager(home, tmp_path, provider)
    manager.resume_pending()
    await _settle(manager, reference.run_id)
    assert provider.sent[0][-1] == {"role": "user", "content": "[Message from the main agent]\nUse the 2024 numbers."}


@pytest.mark.asyncio
async def test_an_orphaned_run_can_be_cancelled(tmp_path):
    home = tmp_path / "h"
    registry = SubagentRegistry(home / "runs.json")
    registry.register(SubagentRunRecord(
        run_id="11111111-0000-0000-0000-000000000000", child_session_key="s", parent_session_key=CHAT,
        parent_channel="web", parent_chat_id="chat-1", task="t", label="t", model=MODEL,
        cleanup="keep", created_at=time.time(), origin="agent", resumable=True, owner=_dead_owner()))
    manager = _manager(home, tmp_path, _Scripted(), resume=False)
    result = json.loads(await manager.cancel("11111111"))
    assert result["status"] == "cancelled"
    assert manager.registry.get("11111111-0000-0000-0000-000000000000").outcome == "cancelled"


@pytest.mark.asyncio
async def test_a_live_processs_run_cannot_be_cancelled_from_another(tmp_path):
    home = tmp_path / "h"
    registry = SubagentRegistry(home / "runs.json")
    live = subagent_owner.process_owner()  # this process, alive
    registry.register(SubagentRunRecord(
        run_id="22222222-0000-0000-0000-000000000000", child_session_key="s", parent_session_key=CHAT,
        parent_channel="web", parent_chat_id="chat-1", task="t", label="t", model=MODEL,
        cleanup="keep", created_at=time.time(), origin="agent", owner=live))
    manager = _manager(home, tmp_path, _Scripted())
    assert json.loads(await manager.cancel("22222222"))["status"] == "error"


def test_the_wire_view_leaves_the_downtime_out_of_the_duration():
    now = time.time()
    record = SubagentRunRecord(
        run_id="r", child_session_key="s", parent_session_key=CHAT, parent_channel="web",
        parent_chat_id="c", task="t", label="t", model=MODEL, cleanup="keep", created_at=now - 4000,
        started_at=now - 4000, ended_at=now, suspended_seconds=3600.0, resumed_at=now - 400)
    view = run_view(record)
    assert view["duration"] == pytest.approx(400, abs=1)
    assert view["resumedAt"] == record.resumed_at and view["suspendedSeconds"] == 3600.0
