"""A subagent's running turn streams live, like the owner's turn (phase 2).

A subagent working for the owner is a thread whose conversation is
``subagent:<threadId>``. While a turn of it runs, the same live events an
owner's turn has go out under that key and the run's id — ``run_started``,
``agent`` deltas, ``iteration_step``, ``tool_progress``, ``agent_note``,
``compaction`` and one terminal — to whoever watches it
(``subagents.watch``), and ``chat.inflight`` / the watch reply hold the turn
so far. These tests pin

* the events and their order, under the child's own key and run id, with one
  terminal (``final``, ``final`` + ``aborted``, or ``error``);
* each path: a real authenticated direct-gateway socket, the relay
  (``subagent.stream`` envelopes, never a ``chat`` event for the parent's
  conversation), the profile host (``profile.event``) for named agents and
  the default one;
* who may watch, the lease, ``subagents.unwatch`` and cleanup on disconnect;
* re-entry mid-turn and after a restart;
* that nothing is built for the wire when nobody watches;
* that the parent's own chat stream and its report turns are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flowly.agent import compaction_status, inflight
from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord
from flowly.agent.subagent_stream import (
    RELAY_STREAM_EVENT,
    SUBAGENT_TURN_ORIGIN,
    ChildStream,
    SubagentStreamHub,
    child_event_access,
)
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer
from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.profile_host_contract import (
    PROFILE_RPC_TIMEOUTS,
    ProfileHostError,
    profile_rpc_session_key,
    profile_rpc_unwatched_key,
    validate_profile_rpc,
)
from flowly.providers.base import LLMErrorInfo, LLMProvider, LLMResponse, ToolCallRequest
from flowly.session.manager import SessionManager

CHAT = "desktop:chat-1"
MODEL = "m"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("FLOWLY_HOME", str(path))
    inflight._runs.clear()
    inflight._control_scopes.clear()
    yield path
    inflight._runs.clear()
    inflight._control_scopes.clear()


# ── Helpers ──────────────────────────────────────────────────────────────────


class _Gated(LLMProvider):
    """Answers from a script; a step may wait on ``gates[n]`` (the n-th call) first."""

    def __init__(self, script: list[Any], default: str = "All done.") -> None:
        super().__init__(api_key="unused")
        self.script = list(script)
        self.default = default
        self.calls = 0
        self.gates: dict[int, asyncio.Event] = {}
        self.waiting: dict[int, asyncio.Event] = {}

    def get_default_model(self) -> str:
        return MODEL

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        n = self.calls
        if n in self.gates:
            self.waiting.setdefault(n, asyncio.Event()).set()
            await self.gates[n].wait()
        step = self.script.pop(0) if self.script else LLMResponse(
            content=self.default, usage={"prompt_tokens": 7, "completion_tokens": 3})
        return step

    def gate(self, n: int) -> asyncio.Event:
        self.gates[n] = asyncio.Event()
        self.waiting.setdefault(n, asyncio.Event())
        return self.gates[n]


class _Recorder:
    """A transport that watches everything and keeps what it was sent, in order."""

    def __init__(self, watching: bool = True) -> None:
        self.watching = watching
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.prepared = 0

    def subagent_stream_watched(self, session_key: str) -> bool:
        return self.watching

    def prepare_subagent_stream(self, event: str, session_key: str, data: dict[str, Any]):
        self.prepared += 1
        if not self.watching:
            return None
        frame = (event, session_key, json.loads(json.dumps(data)))

        async def deliver() -> None:
            self.events.append(frame)

        return deliver


def _manager(home: Path, workspace: Path, provider: LLMProvider, *, sink: Any = None,
             resume: bool = True) -> SubagentManager:
    manager = SubagentManager(provider=provider, workspace=workspace, bus=MessageBus(),
                              registry=SubagentRegistry(home / "subagents" / "runs.json"),
                              state_dir=workspace, max_iterations=10)
    manager.resume_enabled = resume
    manager.stream_sink = sink
    return manager


async def _spawn(manager: SubagentManager, *, task: str = "Look into the notes", parent: str = CHAT,
                 context: str | None = "owner: look into it\nagent: on it") -> str:
    channel, chat_id = parent.split(":", 1)
    started = json.loads(await manager.spawn(
        task=task, origin_channel=channel, origin_chat_id=chat_id, parent_session_key=parent,
        origin="agent", context=context,
    ))
    assert started["status"] == "started", started
    return started["run_id"]


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


async def _settle(manager: SubagentManager, run_id: str, hub: SubagentStreamHub | None = None) -> SubagentRunRecord:
    await _until(lambda: manager.registry.get(run_id).ended_at is not None and not manager._running_tasks)
    if hub is not None:
        await hub.drained()
    return manager.registry.get(run_id)


def _read(call_id: str, path: Path, text: str = "Reading the notes.") -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[
        ToolCallRequest(id=call_id, name="read_file", arguments={"path": str(path)})],
        usage={"prompt_tokens": 10, "completion_tokens": 2})


def _states(events: list[tuple[str, str, dict[str, Any]]]) -> list[str]:
    out = []
    for name, _key, data in events:
        if name == "chat":
            state = data["state"]
            if state == "tool_progress":
                state = f"tool_progress:{data['call']['state']}"
            elif state == "iteration_step":
                state = f"iteration_step:{data['role']}"
            out.append(state)
        else:
            out.append(name)
    return out


# ── The events of a running turn ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_running_turn_streams_start_steps_deltas_and_one_final(tmp_path, home):
    (tmp_path / "notes.txt").write_text("the answer is 42")
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Gated([_read("c1", tmp_path / "notes.txt"),
                       LLMResponse(content="The notes say 42.", usage={"prompt_tokens": 9, "completion_tokens": 4})])
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    record = await _settle(manager, run_id, hub)
    assert record.outcome == "ok"
    key = f"subagent:{run_id}"

    assert _states(recorder.events) == [
        "run_started",
        "agent",                       # what the model said with its call
        "tool_progress:queued",
        "iteration_step:assistant",
        "tool_progress:running",
        "tool_progress:completed",
        "iteration_step:tool",
        "agent",                       # the closing reply
        "final",
    ]
    for name, session_key, data in recorder.events:
        assert session_key == key and data["sessionKey"] == key and data["runId"] == run_id
        assert data["turnOrigin"] == SUBAGENT_TURN_ORIGIN, name
    started = recorder.events[0][2]
    assert started["prompt"] == "Look into the notes" and started["promptAuthor"] == "main_agent"
    assert started["hiddenPrompt"] is False and started["steerable"] is False
    assert started["threadId"] == run_id and started["turn"] == 1 and started["trigger"] == "spawn"
    assert started["parentSessionKey"] == CHAT and started["withConversation"] is True
    assert started["mark"] == record.mark and started["model"] == MODEL and "resumed" not in started
    assert recorder.events[1][2]["data"] == {"text": "Reading the notes."}
    step = recorder.events[3][2]
    assert step["iterationIdx"] == 0 and step["content"] == "Reading the notes."
    assert step["tool_calls"][0]["id"] == "c1" and step["tool_calls"][0]["function"]["name"] == "read_file"
    result = recorder.events[6][2]
    assert result["tool_call_id"] == "c1" and result["iterationIdx"] == 1 and "42" in result["content"]
    assert "status" not in result
    assert recorder.events[7][2]["data"] == {"text": "The notes say 42."}
    final = recorder.events[-1][2]
    assert final["outcome"] == "ok" and "aborted" not in final
    assert final["message"]["content"] == [{"type": "text", "text": "The notes say 42."}]
    assert final["message"]["usage"] == record.usage and final["model"] == MODEL
    assert isinstance(final["durationMs"], int) and final["completedAt"]
    # The turn is saved before its terminal goes out, and the live entry is gone.
    rows = manager.registry.thread_store.display_rows(run_id)
    assert rows[-1]["content"] == "The notes say 42."
    assert inflight.get(key) is None


@pytest.mark.asyncio
async def test_a_note_from_the_main_agent_streams_as_an_agent_note(tmp_path, home):
    (tmp_path / "notes.txt").write_text("notes")
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Gated([_read("c1", tmp_path / "notes.txt"), LLMResponse(content="Looks done."),
                       LLMResponse(content="Dates checked too.")])
    gate = provider.gate(2)
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    await provider.waiting[2].wait()
    await manager.send_to_child(run_id, "Also check the dates.")
    gate.set()
    await _settle(manager, run_id, hub)
    states = _states(recorder.events)
    # The reply written while the note waited is a step of its own (its text
    # leaves the live bubble), then the note, then the closing reply.
    assert states[-5:] == ["agent", "iteration_step:assistant", "agent_note", "agent", "final"]
    boundary = [e[2] for e in recorder.events if e[2].get("state") == "iteration_step"][-1]
    assert boundary["role"] == "assistant" and boundary["content"] == "Looks done."
    assert boundary["tool_calls"] == []
    note = next(e[2] for e in recorder.events if e[2].get("state") == "agent_note")
    assert note["author"] == "main_agent" and note["content"].endswith("Also check the dates.")
    assert note["sessionKey"] == f"subagent:{run_id}" and note["iterationIdx"] == boundary["iterationIdx"] + 1
    assert recorder.events[-1][2]["message"]["content"][0]["text"] == "Dates checked too."
    # The saved turn has the same rows the stream showed.
    rows = manager.registry.thread_store.display_rows(run_id)
    assert [r.get("kind") for r in rows].count("agent_note") == 1


@pytest.mark.asyncio
async def test_a_stopped_turn_ends_with_an_aborted_final(tmp_path, home):
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Gated([LLMResponse(content="never")])
    provider.gate(1)
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    await provider.waiting[1].wait()
    await manager.cancel(run_id)
    await _settle(manager, run_id, hub)
    terminals = [e[2] for e in recorder.events if e[2].get("state") in ("final", "error")]
    assert len(terminals) == 1
    assert terminals[0]["state"] == "final" and terminals[0]["aborted"] is True
    assert terminals[0]["outcome"] == "cancelled"
    assert terminals[0]["message"]["content"] == [{"type": "text", "text": ""}]
    assert inflight.get(f"subagent:{run_id}") is None


@pytest.mark.asyncio
async def test_a_failed_turn_ends_with_one_error(tmp_path, home):
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Gated([LLMResponse(content="bad key", finish_reason="error",
                                   error_info=LLMErrorInfo(code="invalid_api_key", status_code=401))])
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    record = await _settle(manager, run_id, hub)
    assert record.outcome == "error"
    terminals = [e[2] for e in recorder.events if e[2].get("state") in ("final", "error")]
    assert len(terminals) == 1 and terminals[0]["state"] == "error"
    error = terminals[0]
    assert error["outcome"] == "error" and error["errorCode"] == record.error_code
    assert error["errorMessage"].startswith("Error:") and error["retryable"] is False
    assert error["sessionKey"] == f"subagent:{run_id}" and error["runId"] == run_id


@pytest.mark.asyncio
async def test_a_summary_streams_as_the_conversations_compaction(tmp_path, home):
    from tests.test_subagent_compaction import _log_file
    from tests.test_subagent_compaction import _manager as compaction_manager
    from tests.test_subagent_compaction import _Provider as CompactionProvider
    from tests.test_subagent_compaction import _read as read_log
    from tests.test_subagent_compaction import _run as compaction_run

    for n in range(3):
        (tmp_path / f"f{n}.log").write_text(_log_file(n, 3_000))
    overflow = LLMResponse(content="This model's maximum context length is 8192 tokens",
                           finish_reason="error", error_info=LLMErrorInfo(code="context_length_exceeded"))

    def answer(n: int, _call) -> LLMResponse:
        return read_log(f"c{n}", tmp_path / f"f{n - 1}.log") if n <= 3 else overflow

    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = CompactionProvider(answer)
    manager = compaction_manager(tmp_path, provider)
    manager.stream_sink = hub
    run_id = await compaction_run(manager, provider)
    await hub.drained()
    key = f"subagent:{run_id}"
    phases = [e[2] for e in recorder.events if e[0] == "compaction"]
    assert [p["phase"] for p in phases] == ["started", "completed"]
    assert {p["compactionId"] for p in phases} == {phases[0]["compactionId"]}
    assert all(p["sessionKey"] == key and p["runId"] == run_id for p in phases)
    assert phases[1]["tokensAfter"] < phases[1]["tokensBefore"] and phases[1]["messagesRemoved"] > 0
    assert compaction_status.get(key, time.time())["phase"] == "completed"
    # The run then failed on the retried overflow: one error terminal.
    terminals = [e[2] for e in recorder.events if e[2].get("state") in ("final", "error")]
    assert [t["state"] for t in terminals] == ["error"] and terminals[0]["errorCode"] == "context_overflow"


# ── Nobody watching ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nobody_watching_sends_nothing_and_queues_nothing(tmp_path, home):
    recorder = _Recorder(watching=False)
    hub = SubagentStreamHub(lambda: [recorder, None])
    (tmp_path / "notes.txt").write_text("notes")
    provider = _Gated([_read("c1", tmp_path / "notes.txt"), LLMResponse(content="Done.")])
    gate = provider.gate(2)
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    await provider.waiting[2].wait()
    # The in-flight entry is kept for whoever comes to watch.
    live = inflight.get(f"subagent:{run_id}")
    assert live["runId"] == run_id and live["turnOrigin"] == SUBAGENT_TURN_ORIGIN
    gate.set()
    await _settle(manager, run_id, hub)
    assert recorder.events == [] and recorder.prepared > 0
    assert not hub._workers and not hub._queues


@pytest.mark.asyncio
async def test_no_sink_still_keeps_the_inflight_entry(tmp_path, home):
    provider = _Gated([LLMResponse(content="Done.")])
    gate = provider.gate(1)
    manager = _manager(home, tmp_path, provider, sink=None)
    run_id = await _spawn(manager)
    await provider.waiting[1].wait()
    assert inflight.get(f"subagent:{run_id}")["prompt"] == "Look into the notes"
    gate.set()
    record = await _settle(manager, run_id)
    assert record.outcome == "ok" and inflight.get(f"subagent:{run_id}") is None


@pytest.mark.asyncio
async def test_a_failing_transport_never_stops_the_run(tmp_path, home):
    class Broken:
        def subagent_stream_watched(self, _key):
            return True

        def prepare_subagent_stream(self, *_args):
            async def deliver():
                raise ConnectionResetError("gone")
            return deliver

    class Refuses:
        def subagent_stream_watched(self, _key):
            raise RuntimeError("no")

        def prepare_subagent_stream(self, *_args):
            raise RuntimeError("no")

    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [Broken(), Refuses(), recorder])
    manager = _manager(home, tmp_path, _Gated([LLMResponse(content="Done.")]), sink=hub)
    run_id = await _spawn(manager)
    record = await _settle(manager, run_id, hub)
    assert record.outcome == "ok"
    assert _states(recorder.events) == ["run_started", "agent", "final"]


def test_system_work_does_not_stream(tmp_path, home):
    async def scenario():
        recorder = _Recorder()
        hub = SubagentStreamHub(lambda: [recorder])
        manager = _manager(home, tmp_path, _Gated([LLMResponse(content="Done.")]), sink=hub)
        started = json.loads(await manager.spawn(
            task="Review the turn", origin_channel="desktop", origin_chat_id="chat-1",
            parent_session_key=CHAT, origin="system"))
        await _settle(manager, started["run_id"], hub)
        assert recorder.events == [] and not inflight._runs

    asyncio.run(scenario())


# ── Re-entry ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reentry_mid_turn_gets_the_turn_so_far(tmp_path, home, monkeypatch):
    (tmp_path / "notes.txt").write_text("the answer is 42")
    provider = _Gated([_read("c1", tmp_path / "notes.txt"), LLMResponse(content="42.")])
    gate = provider.gate(2)
    manager = _manager(home, tmp_path, provider, sink=SubagentStreamHub(lambda: []))
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: manager.registry)
    run_id = await _spawn(manager)
    await provider.waiting[2].wait()
    key = f"subagent:{run_id}"

    for method, params in (("chat.inflight", {"sessionKey": key}), ("subagents.watch", {"threadId": run_id})):
        result, _ = await feature_rpc.dispatch(method, params)
        live = result["inflight"]
        assert live["runId"] == run_id and live["user"] == "" and live["text"] == ""
        assert live["turnOrigin"] == SUBAGENT_TURN_ORIGIN
        assert live["prompt"] == "Look into the notes" and live["promptAuthor"] == "main_agent"
        assert live["subagent"]["threadId"] == run_id and live["subagent"]["parentSessionKey"] == CHAT
        assert [(e["state"], e["role"]) for e in live["iterations"]] == [
            ("iteration_step", "assistant"), ("iteration_step", "tool")]
        assert all(e["sessionKey"] == key for e in live["iterations"])
        assert [p["call"]["state"] for p in live["toolProgress"]] == ["completed"]
    watch, _ = await feature_rpc.dispatch("subagents.watch", {"threadId": run_id})
    assert watch["sessionKey"] == key and watch["activeRunId"] == run_id and watch["status"] == "open"
    assert watch["leaseSeconds"] == 120 and watch["watching"] is True and watch["streamVersion"] == 1
    gate.set()
    await _settle(manager, run_id)
    after, _ = await feature_rpc.dispatch("subagents.watch", {"threadId": run_id})
    assert after["inflight"] is None and after["activeRunId"] is None


@pytest.mark.asyncio
async def test_a_turn_continued_after_a_restart_rebuilds_its_entry_without_resending(home):
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    journal = [
        {"role": "assistant", "content": "Reading.", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "notes", "status": "ok"},
        {"role": "tool", "tool_call_id": "c2", "name": "exec", "content": "Not run", "status": "not_run"},
        {"role": "user", "content": "[Message from the main agent]\nhurry", "kind": "agent_note",
         "author": "main_agent"},
        {"role": "user", "content": "summarize", "_display_hidden": True},
    ]
    key = "subagent:11111111-2222-4333-8444-555555555555"
    stream = ChildStream(hub, run_id="r1", session_key=key, thread_id=key.split(":", 1)[1],
                         parent_session_key=CHAT, turn=1, trigger="spawn", title="Notes", mark={},
                         model=MODEL, brief="Look", with_conversation=False, access=child_event_access(CHAT))
    await stream.start(journal, resumed=True)
    await hub.drained()
    assert _states(recorder.events) == ["run_started"]
    assert recorder.events[0][2]["resumed"] is True
    live = inflight.get(key)
    assert [(e["state"], e.get("role"), e["iterationIdx"]) for e in live["iterations"]] == [
        ("iteration_step", "assistant", 0), ("iteration_step", "tool", 1), ("iteration_step", "tool", 2),
        ("agent_note", None, 3)]
    assert live["iterations"][2]["status"] == "not_run"
    assert {p["call"]["id"]: p["call"]["state"] for p in live["toolProgress"]} == {
        "c1": "completed", "c2": "stopped"}
    # What follows streams from where the journal left off.
    journal.append({"role": "assistant", "content": "Next.", "tool_calls": [
        {"id": "c3", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
    await stream.sync(journal)
    await hub.drained()
    steps = [e[2] for e in recorder.events if e[2].get("state") == "iteration_step"]
    assert [s["iterationIdx"] for s in steps] == [5]
    stream.end("ok", "Done.")
    await hub.drained()
    assert recorder.events[-1][2]["state"] == "final" and inflight.get(key) is None


@pytest.mark.asyncio
async def test_a_run_resumed_from_its_checkpoint_streams_with_its_earlier_steps(tmp_path, monkeypatch):
    from tests.test_subagent_resume import _manager as resume_manager
    from tests.test_subagent_resume import _reference, _three_calls, _upto
    from tests.test_subagent_resume import _Scripted as ResumeScripted
    from tests.test_subagent_resume import _settle as resume_settle
    from tests.test_subagent_threads import _crash_with_thread, side_effects  # noqa: F401

    (tmp_path / "notes.txt").write_text("notes")
    monkeypatch.setattr("flowly.agent.tools.filesystem.WriteFileTool.execute",
                        AsyncMock(return_value="Wrote"))
    monkeypatch.setattr("flowly.agent.tools.shell.ExecTool.execute", AsyncMock(return_value="exit code 0"))
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home2 = tmp_path / "home2"
    _crash_with_thread(home2, reference, _upto(full, "tool", "c1"))
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = ResumeScripted()
    manager = resume_manager(home2, tmp_path, provider)
    manager.stream_sink = hub
    seen: list[dict | None] = []
    key = f"subagent:{reference.run_id}"
    original = provider.chat

    async def chat(**kwargs):
        seen.append(inflight.get(key))
        return await original(**kwargs)

    provider.chat = chat
    manager.resume_pending()
    record = await resume_settle(manager, reference.run_id)
    await hub.drained()
    assert record.outcome == "ok"
    live = seen[0]
    tools = [(e["tool_call_id"], e.get("status", "ok")) for e in live["iterations"]
             if e.get("role") == "tool"]
    assert tools == [("c1", "ok"), ("c2", "not_run"), ("c3", "not_run")]
    started = [e[2] for e in recorder.events if e[2].get("state") == "run_started"]
    assert len(started) == 1 and started[0]["resumed"] is True
    # Steps from before the restart are not sent again; one terminal.
    sent_tools = [e[2]["tool_call_id"] for e in recorder.events if e[2].get("state") == "iteration_step"
                  and e[2]["role"] == "tool"]
    assert not {"c1", "c2", "c3"} & set(sent_tools)
    assert [e[2]["state"] for e in recorder.events if e[2].get("state") in ("final", "error")] == ["final"]


# ── Who may watch, and for how long ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_watching_is_for_whoever_may_read_the_conversation_that_started_it(tmp_path, home, monkeypatch):
    parent = "desktop:voice:call-1"
    sessions = SessionManager(home / "sessions")
    owned = sessions.get_or_create(parent)
    owned.metadata["voiceOwner"] = {"kind": "account", "uid": "alice"}
    sessions.save(owned)
    provider = _Gated([LLMResponse(content="Done.")])
    gate = provider.gate(1)
    manager = _manager(home, tmp_path, provider)
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: manager.registry)
    run_id = await _spawn(manager, parent=parent)
    await provider.waiting[1].wait()
    key = f"subagent:{run_id}"

    with request_owner_scope(RequestOwner("alice")):
        result, _ = await feature_rpc.dispatch("subagents.watch", {"threadId": run_id})
        assert result["inflight"]["runId"] == run_id
        inflight_reply, _ = await feature_rpc.dispatch("chat.inflight", {"sessionKey": key})
        assert inflight_reply["inflight"]["runId"] == run_id
    for other in (RequestOwner("bob"), HOST_OWNER):
        with request_owner_scope(other):
            for method, params in (("subagents.watch", {"threadId": run_id}),
                                   ("chat.inflight", {"sessionKey": key})):
                with pytest.raises(feature_rpc.FeatureRpcError) as error:
                    await feature_rpc.dispatch(method, params)
                assert error.value.code == "NOT_FOUND"
    # Its events are the account's own: no other recipient may take them.
    access = child_event_access(parent)
    assert access.permits(RequestOwner("alice"))
    assert not access.permits(RequestOwner("bob")) and not access.permits(HOST_OWNER)
    assert child_event_access(CHAT).permits(HOST_OWNER)
    gate.set()
    await _settle(manager, run_id)


@pytest.mark.asyncio
async def test_watch_refuses_what_it_cannot_serve(tmp_path, home, monkeypatch):
    manager = _manager(home, tmp_path, _Gated([]))
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: manager.registry)
    for params, code in (({}, "INVALID"), ({"threadId": "nope"}, "INVALID"),
                         ({"threadId": "11111111-2222-4333-8444-555555555555"}, "NOT_FOUND")):
        with pytest.raises(feature_rpc.FeatureRpcError) as error:
            await feature_rpc.dispatch("subagents.watch", params)
        assert error.value.code == code
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch("subagents.unwatch", {"threadId": "nope"})
    # A conversation with no thread has no running turn to read.
    result, _ = await feature_rpc.dispatch(
        "chat.inflight", {"sessionKey": "subagent:11111111-2222-4333-8444-555555555555"})
    assert result["inflight"] is None
    capabilities = feature_rpc.system_capabilities()
    assert {"subagents.watch", "subagents.unwatch"} <= set(capabilities["featureMethods"])
    assert capabilities["subagentStreamVersion"] == 1


def _bare_gateway() -> GatewayServer:
    server = object.__new__(GatewayServer)
    server.sessions = None
    server._ws_clients = {}
    server._session_ws = {}
    server._subagent_event_versions = {}
    server._ws_rpc_reply = AsyncMock()
    server._ws_rpc_error = AsyncMock()
    return server


class _Socket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(json.loads(json.dumps(data)))


@pytest.mark.asyncio
async def test_the_gateway_lease_unwatch_and_bounds(tmp_path, home, monkeypatch):
    server = _bare_gateway()
    key = "subagent:11111111-2222-4333-8444-555555555555"
    a, b = _Socket(), _Socket()
    server._watch_subagent_stream(a, key)
    server._watch_subagent_stream(b, key)
    server._watch_subagent_stream(a, "desktop:chat-1")  # only subagent conversations
    assert server.subagent_stream_watched(key) and set(server._subagent_stream_targets(key)) == {a, b}
    assert "desktop:chat-1" not in server._stream_watchers()
    server._unwatch_subagent_stream(a, key)
    assert server._subagent_stream_targets(key) == [b]
    # A lease not renewed runs out.
    server._stream_watchers()[key][b] = time.monotonic() - 1
    assert server.prepare_subagent_stream("chat", key, {"sessionKey": key}) is None
    assert not server.subagent_stream_watched(key)
    # A socket watches at most 32 conversations; the oldest lease gives way.
    keys = [f"subagent:11111111-2222-4333-8444-{n:012d}" for n in range(33)]
    for n, watched in enumerate(keys):
        server._watch_subagent_stream(a, watched)
        server._stream_watchers()[watched][a] = time.monotonic() + 100 + n
    assert keys[0] not in server._stream_watchers() and keys[32] in server._stream_watchers()
    server._forget_subagent_stream_socket(a)
    assert server._stream_watchers() == {}


@pytest.mark.asyncio
async def test_a_real_gateway_socket_streams_only_what_it_watches(tmp_path, home, monkeypatch):
    """Two authenticated sockets; one watches. Its stream continues its snapshot exactly."""
    (tmp_path / "notes.txt").write_text("the answer is 42")
    provider = _Gated([_read("c1", tmp_path / "notes.txt"), LLMResponse(content="It is 42.")])
    gate = provider.gate(2)
    server = GatewayServer(host="127.0.0.1", auth_token="fixture-only", require_loopback_auth=True,
                           on_chat_message=AsyncMock())
    hub = SubagentStreamHub(lambda: [server])
    manager = _manager(home, tmp_path, provider, sink=hub)
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: manager.registry)

    async with TestServer(server._create_app()) as http, aiohttp.ClientSession() as session:
        async def connect(client_id: str):
            async with session.post(http.make_url("/api/auth/ws-ticket"),
                                    headers={"Authorization": "Bearer fixture-only"}) as reply:
                ticket = (await reply.json())["ticket"]
            return await session.ws_connect(http.make_url(f"/ws?ticket={ticket}&clientId={client_id}"))

        async def frames_until(ws, predicate, timeout=5.0):
            seen = []
            async with asyncio.timeout(timeout):
                while True:
                    frame = await ws.receive_json()
                    seen.append(frame)
                    if predicate(frame):
                        return seen

        watcher, bystander = await connect("watcher"), await connect("bystander")
        try:
            run_id = await _spawn(manager)
            key = f"subagent:{run_id}"
            await provider.waiting[2].wait()
            await watcher.send_json({"type": "rpc", "id": "w1", "method": "subagents.watch",
                                     "params": {"threadId": run_id}})
            reply = (await frames_until(watcher, lambda f: f.get("id") == "w1"))[-1]
            snapshot = reply["result"]["inflight"]
            assert [e["iterationIdx"] for e in snapshot["iterations"]] == [0, 1]
            gate.set()
            frames = await frames_until(watcher, lambda f: f.get("event") == "chat"
                                        and f["data"].get("state") == "final")
            live = [f for f in frames if f.get("event") in ("chat", "agent")]
            assert all(f["data"]["sessionKey"] == key and f["data"]["runId"] == run_id for f in live)
            # Exactly what came after the snapshot: the closing delta and the final.
            assert [(f["event"], f["data"].get("state")) for f in live] == [("agent", None), ("chat", "final")]
            assert live[0]["data"]["data"] == {"text": "It is 42."}
            await _settle(manager, run_id, hub)

            # The bystander got no event of the subagent's conversation.
            await bystander.send_json({"type": "rpc", "id": "h", "method": "health", "params": {}})
            others = await frames_until(bystander, lambda f: f.get("id") == "h")
            assert not [f for f in others if isinstance(f.get("data"), dict)
                        and f["data"].get("sessionKey") == key]

            # Unwatched: the next turn's events do not come.
            await watcher.send_json({"type": "rpc", "id": "u1", "method": "subagents.unwatch",
                                     "params": {"threadId": run_id}})
            await frames_until(watcher, lambda f: f.get("id") == "u1")
            assert not server.subagent_stream_watched(key)

            # Watching again, then disconnecting, leaves nothing bound.
            await watcher.send_json({"type": "rpc", "id": "w2", "method": "subagents.watch",
                                     "params": {"threadId": run_id}})
            await frames_until(watcher, lambda f: f.get("id") == "w2")
            assert server.subagent_stream_watched(key)
            await watcher.close()
            await _until(lambda: not server.subagent_stream_watched(key))
        finally:
            await watcher.close()
            await bystander.close()


# ── The relay ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_relay_wraps_the_turn_in_subagent_stream_envelopes(tmp_path, home, monkeypatch):
    (tmp_path / "notes.txt").write_text("notes")
    channel = WebChannel(WebChannelConfig(), MessageBus())
    relay = AsyncMock()
    channel._ws = relay
    provider = _Gated([_read("c1", tmp_path / "notes.txt"), LLMResponse(content="Done.")])
    gate = provider.gate(1)
    hub = SubagentStreamHub(lambda: [channel])
    manager = _manager(home, tmp_path, provider, sink=hub)
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: manager.registry)
    run_id = await _spawn(manager)
    key = f"subagent:{run_id}"
    await provider.waiting[1].wait()

    await channel._dispatch_rpc(relay, {"type": "rpc", "id": "w", "sessionId": "s1",
                                        "method": "subagents.watch", "params": {"threadId": run_id}})
    reply = json.loads(relay.send.await_args.args[0])
    assert reply["result"]["sessionKey"] == key and reply["sessionId"] == "s1"
    relay.send.reset_mock()
    gate.set()
    await _settle(manager, run_id, hub)

    frames = [json.loads(call.args[0]) for call in relay.send.await_args_list]
    assert frames, "the watcher got the turn"
    for frame in frames:
        # Never a chat event: the relay would write it into the conversation
        # of the session that receives it (the parent's).
        assert frame["event"] == RELAY_STREAM_EVENT and frame["sessionId"] == "s1"
        assert frame["data"]["sessionKey"] == key
        assert frame["data"]["event"] in ("chat", "agent")
        assert frame["data"]["data"]["sessionKey"] == key and frame["data"]["data"]["runId"] == run_id
    inner = [f["data"]["data"].get("state") or f["data"]["event"] for f in frames]
    assert inner[-1] == "final" and inner.count("final") == 1
    assert "iteration_step" in inner

    # Unwatch, disconnect and a reconnect all release the session.
    await channel._dispatch_rpc(relay, {"type": "rpc", "id": "u", "sessionId": "s1",
                                        "method": "subagents.unwatch", "params": {"threadId": run_id}})
    assert not channel.subagent_stream_watched(key)
    channel._watch_subagent_stream("s1", key)
    channel._watch_subagent_stream("s2", key)
    await channel._handle_relay_message(relay, {"type": "browser-disconnected", "sessionId": "s1"})
    assert set(channel._stream_watchers()[key]) == {"s2"}
    channel._stream_watchers()[key]["s2"] = time.monotonic() - 1
    assert channel.prepare_subagent_stream("chat", key, {"sessionKey": key}) is None
    assert not channel.subagent_stream_watched(key)


@pytest.mark.asyncio
async def test_the_relay_sends_nothing_without_a_watcher_or_a_connection(home):
    channel = WebChannel(WebChannelConfig(), MessageBus())
    key = "subagent:11111111-2222-4333-8444-555555555555"
    assert channel.prepare_subagent_stream("chat", key, {"sessionKey": key}) is None
    channel._watch_subagent_stream("s1", key)
    channel._ws = None
    assert channel.prepare_subagent_stream("chat", key, {"sessionKey": key}) is None


# ── Named agents (the profile host) ─────────────────────────────────────────


def test_the_profile_contract_admits_watch_and_unwatch_with_a_thread_id_only():
    thread_id = "11111111-2222-4333-8444-555555555555"
    key = f"subagent:{thread_id}"
    for method in ("subagents.watch", "subagents.unwatch"):
        assert method in PROFILE_RPC_TIMEOUTS
        assert validate_profile_rpc(method, {"threadId": thread_id}) == (method, {"threadId": thread_id})
        for bad in ({}, {"threadId": "x"}, {"threadId": thread_id, "sessionKey": key}):
            with pytest.raises(ProfileHostError):
                validate_profile_rpc(method, bad)
    assert profile_rpc_session_key("subagents.watch", {"threadId": thread_id}) == key
    assert profile_rpc_session_key("subagents.unwatch", {"threadId": thread_id}) == ""
    assert profile_rpc_unwatched_key("subagents.unwatch", {"threadId": thread_id}) == key
    assert profile_rpc_unwatched_key("subagents.watch", {"threadId": thread_id}) == ""


def _profiles(tmp_path: Path, monkeypatch) -> None:
    import flowly.profile as profiles

    monkeypatch.setattr(profiles, "_DEFAULT_HOME", tmp_path / "home")
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", tmp_path / "home" / "profiles")
    profiles.create_profile("writer", local_runtime=True)


@pytest.mark.asyncio
async def test_a_named_agents_subagent_streams_through_the_profile_host(tmp_path, home, monkeypatch):
    from flowly.profile_host import ProfileHost

    _profiles(tmp_path, monkeypatch)
    thread_id = "11111111-2222-4333-8444-555555555555"
    key = f"subagent:{thread_id}"
    host = ProfileHost()
    forwarded: list[tuple[str, dict]] = []

    async def target(name, method, params, timeout, **kwargs):
        # The agent's runtime: it answers the watch and binds the host's socket.
        forwarded.append((method, dict(params)))
        return {"ok": True, "watching": True, "threadId": thread_id, "sessionKey": key, "inflight": None}

    host._target_rpc = target
    server = _bare_gateway()
    server._profile_host = host
    server._profile_client_subscriptions = OrderedDict()
    server._profile_run_subscriptions = OrderedDict()
    server._profile_subagent_event_versions = {}
    server._ws_send = AsyncMock()
    watcher, other = web.WebSocketResponse(), web.WebSocketResponse()
    server._ws_clients = {"watcher": watcher, "other": other}
    host.subscribe_events(server._broadcast_profile_host_event)

    channel = WebChannel(WebChannelConfig(), MessageBus())
    channel._ws = AsyncMock()
    channel.set_profile_host(host)

    await server._handle_profile_host_rpc(watcher, "watcher", "1", "profiles.rpc", {
        "name": "writer", "method": "subagents.watch", "params": {"threadId": thread_id}})
    server._ws_rpc_error.assert_not_awaited()
    await channel._handle_profile_rpc(channel._ws, {"type": "rpc", "id": "r1", "sessionId": "s1",
        "method": "profiles.rpc", "params": {"name": "writer", "method": "subagents.watch",
                                             "params": {"threadId": thread_id}}})
    assert [m for m, _ in forwarded] == ["subagents.watch", "subagents.watch"]

    class Runtime:
        profile = "writer"
        active_runs: set[str] = set()
        last_used_at = 0.0

    runtime = Runtime()
    channel._ws.reset_mock()
    payload = {"state": "iteration_step", "runId": "r-child", "sessionKey": key,
               "turnOrigin": SUBAGENT_TURN_ORIGIN, "iterationIdx": 0, "role": "assistant", "content": "x"}
    await host._handle_profile_event("writer", "chat", payload, runtime=runtime)
    final = {"state": "final", "runId": "r-child", "sessionKey": key, "turnOrigin": SUBAGENT_TURN_ORIGIN,
             "message": {"role": "assistant", "content": []}}
    await host._handle_profile_event("writer", "chat", final, runtime=runtime)

    sent = [call.args for call in server._ws_send.await_args_list]
    assert [ws for ws, _ in sent] == [watcher, watcher], "only the client that watches"
    assert all(frame["event"] == "profile.event" and frame["data"]["profile"] == "writer"
               and frame["data"]["type"] == "chat" and frame["data"]["data"]["sessionKey"] == key
               for _, frame in sent)
    relay_frames = [json.loads(call.args[0]) for call in channel._ws.send.await_args_list]
    assert [f["event"] for f in relay_frames] == ["profile.event", "profile.event"]
    assert all(f["sessionId"] == "s1" and f["data"]["data"]["sessionKey"] == key for f in relay_frames)
    # Not the agent's own turn: no busy state, no roster activity.
    assert runtime.active_runs == set() and runtime.last_used_at > 0

    # Unwatching releases this client only; the runtime is not asked.
    await server._handle_profile_host_rpc(watcher, "watcher", "2", "profiles.rpc", {
        "name": "writer", "method": "subagents.unwatch", "params": {"threadId": thread_id}})
    reply = server._ws_rpc_reply.await_args.args[2]
    assert reply["watching"] is False and reply["sessionKey"] == key
    assert [m for m, _ in forwarded] == ["subagents.watch", "subagents.watch"]
    await channel._handle_profile_rpc(channel._ws, {"type": "rpc", "id": "r2", "sessionId": "s1",
        "method": "profiles.rpc", "params": {"name": "writer", "method": "subagents.unwatch",
                                             "params": {"threadId": thread_id}}})
    server._ws_send.reset_mock()
    channel._ws.reset_mock()
    await host._handle_profile_event("writer", "chat", payload, runtime=runtime)
    assert server._ws_send.await_count == 0
    assert not [c for c in channel._ws.send.await_args_list
                if json.loads(c.args[0]).get("event") == "profile.event"]


@pytest.mark.asyncio
async def test_the_default_agent_streams_through_the_hosts_own_socket(tmp_path, home, monkeypatch):
    """profiles.rpc for "default" reaches the primary gateway through its private socket."""
    provider = _Gated([LLMResponse(content="Done.")])
    gate = provider.gate(1)
    server = GatewayServer(host="127.0.0.1", auth_token="fixture-only", require_loopback_auth=True,
                           on_chat_message=AsyncMock(), enable_profile_host=True)
    hub = SubagentStreamHub(lambda: [server])
    manager = _manager(home, tmp_path, provider, sink=hub)
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: manager.registry)
    client = _Socket()
    server._ws_clients["client"] = client
    try:
        run_id = await _spawn(manager)
        await provider.waiting[1].wait()
        await server._handle_profile_host_rpc(client, "client", "1", "profiles.rpc", {
            "name": "default", "method": "subagents.watch", "params": {"threadId": run_id}})
        assert client.sent[-1]["result"]["inflight"]["runId"] == run_id
        gate.set()
        await _settle(manager, run_id, hub)
        await _until(lambda: any(f.get("event") == "profile.event" and f["data"]["data"].get("state") == "final"
                                 for f in client.sent))
        events = [f["data"] for f in client.sent if f.get("event") == "profile.event"]
        assert all(e["profile"] == "default" and e["data"]["sessionKey"] == f"subagent:{run_id}"
                   for e in events)
        assert [e["type"] for e in events][-1] == "chat"
    finally:
        await server.profile_host.shutdown()


# ── The parent's own stream is untouched ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_parents_report_turn_and_the_childs_stream_stay_apart(tmp_path, home):
    from flowly.agent.report_turn import REPORT_TURN_ORIGIN
    from tests.test_subagent_report_streaming import CHAT as REPORT_CHAT
    from tests.test_subagent_report_streaming import (
        _agent,
        _chat,
        _FakeWS,
        _gateway,
        _Provider,
        _spawn,
    )
    from tests.test_subagent_report_streaming import _until as report_until

    provider = _Provider()
    agent = _agent(tmp_path, provider)
    server = _gateway(agent)
    hub = SubagentStreamHub(lambda: [server])
    agent.subagents.stream_sink = hub
    parent_ws, child_ws = _FakeWS(), _FakeWS()
    server.bind_session_ws(REPORT_CHAT, parent_ws)

    real_spawn = agent.subagents.spawn

    async def spawn_and_watch(*args, **kwargs):
        raw = await real_spawn(*args, **kwargs)
        server._watch_subagent_stream(child_ws, f"subagent:{json.loads(raw)['run_id']}")
        return raw

    agent.subagents.spawn = spawn_and_watch
    run_id = await _spawn(agent)
    child_key = f"subagent:{run_id}"
    await report_until(lambda: any(e.get("state") == "final" for e in _chat(parent_ws)))
    await hub.drained()

    parent_events = [e["data"] for e in parent_ws.sent if isinstance(e.get("data"), dict)]
    assert parent_events and all(e.get("sessionKey") != child_key for e in parent_events)
    report = _chat(parent_ws)
    assert report[0]["state"] == "run_started" and report[0]["turnOrigin"] == REPORT_TURN_ORIGIN
    assert [e["state"] for e in report].count("final") == 1
    assert all(e["sessionKey"] == REPORT_CHAT for e in report)

    child_events = [e["data"] for e in child_ws.sent if isinstance(e.get("data"), dict)]
    assert child_events and all(e["sessionKey"] == child_key and e["runId"] == run_id for e in child_events)
    assert all(e.get("turnOrigin") == SUBAGENT_TURN_ORIGIN for e in child_events)
    assert [e.get("state") for e in child_events if e.get("state") in ("final", "error")] == ["final"]
    assert inflight.get(child_key) is None
