"""A running subagent's checkpoint rebuilds exactly what the provider saw.

The golden test runs one child through tool calls with long arguments and
results, a Gemini ``extra_content``, an unavailable tool, a message from the
main agent, a context-overflow trim, the iteration-based compaction, the
repetition guard and the summary call — and at every model call checks that
replaying the checkpoint written so far gives the very list the provider was
sent. The rest pins each record kind, the refusals, privacy (0600, deleted
when the run ends, none for runs that are not continued) and the size cap.
"""

from __future__ import annotations

import asyncio
import copy
import json
import stat
import time
from typing import Any

import pytest

from flowly.agent import subagent_checkpoint as cp
from flowly.agent.subagent import (
    SubagentManager,
    _strip_subagent_tool_results,
    _trim_to_context_budget,
)
from flowly.agent.subagent_checkpoint import (
    CHECKPOINT_VERSION,
    GRACE_PROMPT,
    ResumeRefusal,
    SubagentCheckpoint,
    read_checkpoint,
    restore_child_state,
    tool_class,
)
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord
from flowly.agent.tools.discovery import annotate_search_repetition
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMErrorInfo, LLMProvider, LLMResponse, ToolCallRequest

CHAT = "web:chat-1"
MODEL = "m"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


def restore(records: list[dict[str, Any]], model: str = MODEL):
    return restore_child_state(
        records,
        strip=_strip_subagent_tool_results,
        trim=lambda messages, *, budget: _trim_to_context_budget(messages, model, budget=budget),
        annotate=annotate_search_repetition,
    )


def _manager(tmp_path, provider: LLMProvider, *, resume: bool = True, bus: MessageBus | None = None,
             max_iterations: int = 30) -> SubagentManager:
    manager = SubagentManager(provider=provider, workspace=tmp_path, bus=bus or MessageBus(),
                              registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path,
                              max_iterations=max_iterations)
    manager.resume_enabled = resume
    return manager


def _call(call_id: str, name: str, **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


class _Scripted(LLMProvider):
    """Answers from a script; at each call keeps what it was sent and the
    checkpoint as it stood then."""

    def __init__(self, script: list[Any]) -> None:
        super().__init__(api_key="unused")
        self.script = list(script)
        self.sent: list[list[dict[str, Any]]] = []
        self.logs: list[list[dict[str, Any]]] = []
        self.manager: SubagentManager | None = None
        self.run_id = ""

    def get_default_model(self) -> str:
        return MODEL

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.sent.append(copy.deepcopy(kwargs["messages"]))
        if self.manager is not None and self.run_id:
            path = self.manager.registry.checkpoint_path(self.run_id)
            self.logs.append(read_checkpoint(path))
        step = self.script.pop(0)
        if callable(step):
            step = await step(self)
        return step


async def _run(manager: SubagentManager, provider: _Scripted, task: str = "Look into it") -> str:
    started = json.loads(await manager.spawn(task=task, origin_channel="web", origin_chat_id="chat-1",
                                             parent_session_key=CHAT, origin="agent",
                                             context="owner: please look\nagent: on it"))
    provider.run_id = started["run_id"]
    for _ in range(400):
        await asyncio.sleep(0.01)
        if manager.registry.get(started["run_id"]).ended_at is not None and not manager._running_tasks:
            break
    return started["run_id"]


# ── The golden test ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replaying_the_checkpoint_gives_the_provider_list_at_every_step(tmp_path):
    (tmp_path / "big.txt").write_text("B" * 6_000)
    (tmp_path / "a.txt").write_text("same content every time")

    async def main_agent_writes(provider: _Scripted) -> LLMResponse:
        await provider.manager.send_to_child(provider.run_id, "Also check the year 2024.")
        return LLMResponse(content=None, tool_calls=[_call("c4", "read_file", path=str(tmp_path / "a.txt"))])

    read_a = lambda cid: LLMResponse(content=None, tool_calls=[  # noqa: E731
        _call(cid, "read_file", path=str(tmp_path / "a.txt"))])
    script = [
        LLMResponse(content="Starting.", tool_calls=[
            ToolCallRequest(id="c1", name="write_file",
                            arguments={"path": str(tmp_path / "out.txt"), "content": "x" * 3_000},
                            extra_content={"google": {"thought_signature": "sig-1"}}),
            _call("c1b", "read_file", path=str(tmp_path / "big.txt")),
        ], usage={"prompt_tokens": 10, "completion_tokens": 5}),
        read_a("c2"),
        LLMResponse(content=None, tool_calls=[_call("c3", "no_such_tool", q=1)]),
        main_agent_writes,
        LLMResponse(content="too long", finish_reason="error",
                    error_info=LLMErrorInfo(code="context_length_exceeded")),
        read_a("c6"),
        read_a("c7"),
        read_a("c8"),  # the fourth repeat in a row: tools are off next
        LLMResponse(content="Summary of what I found."),
    ]
    provider = _Scripted(script)
    manager = _manager(tmp_path, provider)
    provider.manager = manager
    run_id = await _run(manager, provider)

    record = manager.registry.get(run_id)
    assert record.outcome == "ok", record.error
    assert len(provider.sent) == 9 and not provider.script
    for index, (sent, log) in enumerate(zip(provider.sent, provider.logs)):
        state = restore(log)
        assert not isinstance(state, ResumeRefusal), state
        assert state.messages == sent, f"call {index + 1} differs"
    # What the replay rebuilt along the way.
    before_summary = restore(provider.logs[-1])
    assert before_summary.grace and before_summary.grace["reason"] == "repeating itself"
    assert before_summary.messages[-1] == {"role": "user", "content": GRACE_PROMPT}
    assert before_summary.stalled and before_summary.input_budget_recovered
    assert before_summary.unread_mail == []
    first_reply = provider.sent[1][2]
    assert first_reply["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig-1"}}
    assert json.loads(first_reply["tool_calls"][0]["function"]["arguments"])["content"] == "x" * 3_000
    big = provider.sent[1][4]["content"]
    assert big.startswith("B" * 4_000) and "truncated from" in big
    assert "[Message from the main agent]\nAlso check the year 2024." in [
        m.get("content") for m in provider.sent[4]]
    # The checkpoint is gone once the result is recorded.
    assert not manager.registry.checkpoint_path(run_id).exists()


@pytest.mark.asyncio
async def test_a_reply_kept_for_a_late_message_is_replayed(tmp_path):
    async def answer_while_written_to(provider: _Scripted) -> LLMResponse:
        await provider.manager.send_to_child(provider.run_id, "One more thing.")
        return LLMResponse(content="First answer.")

    provider = _Scripted([answer_while_written_to, LLMResponse(content="Final answer.")])
    manager = _manager(tmp_path, provider)
    provider.manager = manager
    await _run(manager, provider)
    state = restore(provider.logs[-1])
    assert state.messages == provider.sent[-1]
    assert {"role": "assistant", "content": "First answer."} in state.messages


# ── Restoring each kind of stop ──────────────────────────────────────────────

def _header(**changes: Any) -> dict[str, Any]:
    return {"v": CHECKPOINT_VERSION, "n": 0, "t": 1.0, "type": "header", "runId": "r", "coreVersion": "x",
            "model": MODEL, "label": "l", "maxIterations": 10, "systemPrompt": "SYS",
            "firstMessage": "TASK", "tools": [], **changes}


def _rec(kind: str, **fields: Any) -> dict[str, Any]:
    return {"v": CHECKPOINT_VERSION, "n": 1, "t": 1.0, "type": kind, **fields}


def _model(*calls: tuple[str, str], iteration: int = 1) -> dict[str, Any]:
    return _rec("model", iteration=iteration, usage={}, message={
        "role": "assistant", "content": "",
        "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}
                       for cid, name in calls],
    })


def test_restore_refuses_what_it_cannot_continue():
    assert restore([]) == ResumeRefusal("missing")
    refused = restore([_header(v=99)])
    assert isinstance(refused, ResumeRefusal) and refused.reason == "version"
    assert restore([_rec("step", iteration=1, notes=[], mail=[], budget=1000)]).reason == "corrupt"
    assert restore([_header(), _rec("step", iteration="x")]).reason == "corrupt"


def test_a_header_alone_starts_from_the_first_step():
    state = restore([_header()])
    assert state.messages == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "TASK"}]
    assert state.iteration == 0 and not state.awaiting_model and state.max_iterations == 10


def test_a_step_without_its_reply_redoes_the_model_call():
    state = restore([_header(), _rec("step", iteration=1, notes=[], mail=[], budget=100_000)])
    assert state.iteration == 1 and state.awaiting_model


def test_calls_started_and_not_started_are_told_apart():
    state = restore([
        _header(), _rec("step", iteration=1, notes=[], mail=[], budget=100_000),
        _model(("a", "read_file"), ("b", "exec"), ("c", "write_file")),
        _rec("tool_start", callId="a", name="read_file"),
        _rec("tool", callId="a", protocolName="read_file", content="A", status="ok",
             guard={"tool": "read_file", "repeated": False}),
        _rec("tool_start", callId="b", name="exec"),
    ])
    assert [(c.id, c.started, c.stopped) for c in state.open_calls] == [("b", True, False), ("c", False, False)]
    assert state.messages[-1]["content"] == "A"


def test_a_clean_shutdown_marks_the_running_call_stopped():
    state = restore([
        _header(), _rec("step", iteration=1, notes=[], mail=[], budget=100_000),
        _model(("b", "exec")), _rec("tool_start", callId="b", name="exec"),
        _rec("suspended", reason="shutdown"),
    ])
    assert state.suspended and state.open_calls[0].stopped


def test_grace_and_final_are_restored():
    grace = restore([
        _header(), _rec("phase", phase="grace", reason="max iterations", hitMaxIter=True,
                        preserved=None, status="ok", errorCode=None),
    ])
    assert grace.grace["hitMaxIter"] and grace.messages[-1]["content"] == GRACE_PROMPT
    final = restore([_header(), _rec("final", status="ok", error=None, errorCode=None, result="R")])
    assert final.final == {"status": "ok", "error": None, "errorCode": None, "result": "R"}


def test_unread_messages_come_back_and_a_reply_read_by_an_unfinished_call_too():
    state = restore([
        _header(),
        _rec("mail", id="m1", text="read at a step"),
        _rec("step", iteration=1, notes=["read at a step"], mail=["m1"], budget=100_000),
        _model(("q", "agent_message")),
        _rec("tool_start", callId="q", name="agent_message"),
        _rec("mail", id="m2", text="the reply"),
        _rec("inbox", callId="q", ids=["m2"]),
        _rec("mail", id="m3", text="not read yet"),
    ])
    assert [m["id"] for m in state.unread_mail] == ["m2", "m3"]
    answered = restore([
        _header(), _rec("step", iteration=1, notes=[], mail=[], budget=100_000),
        _model(("q", "agent_message")), _rec("tool_start", callId="q", name="agent_message"),
        _rec("mail", id="m2", text="the reply"), _rec("inbox", callId="q", ids=["m2"]),
        _rec("tool", callId="q", protocolName="agent_message", content="{}", status="ok", guard=None),
    ])
    assert answered.unread_mail == []


def test_a_reply_read_by_a_call_a_restart_cut_off_is_read_again():
    state = restore([
        _header(), _rec("step", iteration=1, notes=[], mail=[], budget=100_000),
        _model(("q", "agent_message")), _rec("tool_start", callId="q", name="agent_message"),
        _rec("mail", id="m2", text="the reply"), _rec("inbox", callId="q", ids=["m2"]),
        _rec("tool", callId="q", protocolName="agent_message", content="Interrupted: …",
             status="interrupted", guard=None),
        _rec("resumed", after="crash", attempt=1, interrupted=["q"], notRun=[]),
    ])
    assert [m["id"] for m in state.unread_mail] == ["m2"] and state.open_calls == []


def test_the_overflow_trim_and_the_guard_are_rebuilt():
    repeated = cp.mark_repeated("read_file", "same")
    records = [_header(), _rec("step", iteration=1, notes=[], mail=[], budget=100_000),
               _model(("a", "read_file")),
               _rec("tool", callId="a", protocolName="read_file", content="same", status="ok",
                    guard={"tool": "read_file", "repeated": False})]
    for i in range(4):
        records += [_rec("step", iteration=i + 2, notes=[], mail=[], budget=100_000),
                    _model((f"r{i}", "read_file"), iteration=i + 2),
                    _rec("tool", callId=f"r{i}", protocolName="read_file", content=repeated, status="ok",
                         guard={"tool": "read_file", "repeated": True})]
    records.append(_rec("recovery", iteration=6))
    state = restore(records)
    assert state.stalled and state.repetition.repeats_in_a_row == 4
    assert state.input_budget_recovered


def test_a_torn_last_line_is_not_a_record_and_is_cut_before_appending(tmp_path):
    path = tmp_path / "c.jsonl"
    checkpoint = SubagentCheckpoint(path)
    checkpoint.append("header", durable=True, systemPrompt="S", firstMessage="T", maxIterations=3)
    with path.open("ab") as stream:
        stream.write(b'{"v":1,"n":1,"type":"model","mess')
    assert [r["type"] for r in read_checkpoint(path)] == ["header"]
    reopened = SubagentCheckpoint(path)
    reopened.append("step", iteration=1, notes=[], mail=[], budget=10)
    records = read_checkpoint(path)
    assert [(r["type"], r["n"]) for r in records] == [("header", 0), ("step", 1)]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_tool_classes():
    assert tool_class("read_file") == "read" and tool_class("web_fetch") == "read"
    assert tool_class("write_file") == "write" and tool_class("exec") == "exec"
    assert tool_class("agent_message") == "message"
    assert tool_class("edit_file") == "change" and tool_class("memory_append") == "change"
    assert tool_class("artifact", {"action": "create"}) == "change"
    assert tool_class("artifact", {"action": "list"}) == "read"
    assert tool_class("knowledge_graph", {"action": "add"}) == "change"
    assert tool_class("knowledge_graph", {"action": "query"}) == "read"
    assert tool_class("something_new") == "change"


# ── Which runs keep one, and for how long ────────────────────────────────────

class _Answer(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="unused")
        self.paths: list[bool] = []
        self.manager: SubagentManager | None = None

    def get_default_model(self) -> str:
        return MODEL

    async def chat(self, **kwargs: Any) -> LLMResponse:
        if self.manager is not None:
            self.paths.append(any(self.manager.registry._checkpoint_dir.glob("*.jsonl"))
                              if self.manager.registry._checkpoint_dir.exists() else False)
        return LLMResponse(content="done")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "system", "wait", "silent", "not_a_gateway"])
async def test_only_runs_that_are_continued_keep_a_checkpoint(tmp_path, kind):
    provider = _Answer()
    manager = _manager(tmp_path, provider, resume=kind != "not_a_gateway")
    provider.manager = manager
    result = await manager.spawn(
        task="t", origin_channel="web", origin_chat_id="chat-1", parent_session_key=CHAT,
        origin="system" if kind == "system" else "agent",
        wait=kind == "wait", silent=kind == "silent",
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not manager._running_tasks:
            break
    run_id = manager.registry.all()[0].run_id
    assert manager.registry.get(run_id).resumable is (kind == "agent")
    assert provider.paths == [kind == "agent"]
    assert not manager.registry.checkpoint_path(run_id).exists()
    del result


@pytest.mark.asyncio
async def test_a_checkpoint_past_its_size_cap_stops_and_the_run_goes_on(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "MAX_CHECKPOINT_BYTES", 600)
    (tmp_path / "a.txt").write_text("content " * 50)
    provider = _Scripted([
        LLMResponse(content=None, tool_calls=[_call("c1", "read_file", path=str(tmp_path / "a.txt"))]),
        LLMResponse(content="done anyway"),
    ])
    manager = _manager(tmp_path, provider)
    provider.manager = manager
    run_id = await _run(manager, provider, task="short")
    record = manager.registry.get(run_id)
    assert record.outcome == "ok" and record.resumable is False
    assert not manager.registry.checkpoint_path(run_id).exists()


def test_the_registry_prunes_a_checkpoint_whose_run_ended(tmp_path):
    registry = SubagentRegistry(tmp_path / "runs.json")
    for run_id in ("running", "ended"):
        registry.register(SubagentRunRecord(
            run_id=run_id, child_session_key=f"subagent:{run_id}", parent_session_key=CHAT,
            parent_channel="web", parent_chat_id="chat-1", task="t", label="t", model=MODEL,
            cleanup="keep", created_at=time.time(), origin="agent", resumable=True))
        SubagentCheckpoint(registry.checkpoint_path(run_id)).append("header", durable=True)
    stray = registry._checkpoint_dir / ("0" * 64 + ".jsonl")
    stray.write_text("{}\n")
    registry.finish("ended", "ok", result="r")
    assert registry.checkpoint_path("running").exists()
    assert not registry.checkpoint_path("ended").exists()
    assert not stray.exists()
