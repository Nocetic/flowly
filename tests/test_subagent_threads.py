"""Every subagent is a thread: its own conversation, stored apart, read only.

A run of a subagent working for the owner is the first turn of a thread
(``subagent_threads``): a real Session ``subagent:<threadId>`` kept in
``<home>/subagents/sessions`` and an edge in ``threads.json``. These tests pin

* where the conversation lives and that nothing reading the owner's
  conversations sees it (listing, search index, MCP read plane, voice recall,
  file tools);
* that it holds what the provider saw, uncut — every reply, every tool result
  as the model got it (a long one ends with a pointer to its full output,
  kept in the thread's spill dir), the main agent's notes, the hidden summary
  request — and a closing row with the result;
* ``subagents.history`` paging and who may read it, on the direct gateway,
  the relay and the profile host, and ``chat.history`` on the gateway;
* that every write naming a subagent's conversation is refused
  (SUBAGENT_THREAD_READ_ONLY) on each path;
* retention 30 days after close, the 7-day idle close, the 16-thread cap and
  the factual text the agent gets about it, the deleted parent;
* restarts: the journal rebuilt from the checkpoint, a turn saved once;
* migration: older runs and a broken threads file.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import stat
import time
import types
from collections import OrderedDict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from flowly.agent.delegation import MANY_CHILDREN_RUNNING, MAX_CHILDREN_PER_CONVERSATION
from flowly.agent.subagent import _MAX_CONCURRENT_SUBAGENTS, SubagentManager
from flowly.agent.subagent_checkpoint import GRACE_PROMPT, restore_child_state
from flowly.agent.subagent_observation import run_view
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord, ThreadCapReached
from flowly.agent.subagent_threads import (
    MAX_OPEN_THREADS_PER_CONVERSATION,
    THREAD_IDLE_CLOSE_SECONDS,
    THREAD_RETENTION_SECONDS,
    SubagentThreadRecord,
    thread_session_key,
)
from flowly.agent.tool_result_spill import SPILL_POINTER_MARKER
from flowly.agent.tools.discovery import annotate_search_repetition
from flowly.agent.tools.filesystem import ReadFileTool
from flowly.agent.tools.web import WebFetchTool
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer
from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from flowly.session.keys import (
    SUBAGENT_THREAD_READ_ONLY,
    SubagentThreadReadOnlyError,
    require_rpc_subagent_access,
)
from flowly.session.manager import SessionManager, iter_session_files

CHAT = "desktop:chat-1"
MODEL = "m"
POSIX = os.name == "posix"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("FLOWLY_HOME", str(path))
    return path


class _Scripted(LLMProvider):
    """Answers from a script (a default when it runs out) and keeps what it was sent."""

    def __init__(self, script: list[Any] | None = None, default: str = "All done.") -> None:
        super().__init__(api_key="unused")
        self.script = list(script or [])
        self.default = default
        self.sent: list[list[dict[str, Any]]] = []
        self.manager: SubagentManager | None = None
        self.run_id = ""

    def get_default_model(self) -> str:
        return MODEL

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.sent.append(copy.deepcopy(kwargs["messages"]))
        step = self.script.pop(0) if self.script else LLMResponse(
            content=self.default, usage={"prompt_tokens": 7, "completion_tokens": 3})
        if callable(step):
            step = await step(self)
        return step


def _manager(home: Path, workspace: Path, provider: LLMProvider, *, max_iterations: int = 30,
             bus: MessageBus | None = None, resume: bool = True) -> SubagentManager:
    manager = SubagentManager(provider=provider, workspace=workspace, bus=bus or MessageBus(),
                              registry=SubagentRegistry(home / "subagents" / "runs.json"),
                              state_dir=workspace, max_iterations=max_iterations)
    manager.resume_enabled = resume
    if isinstance(provider, _Scripted):
        provider.manager = manager
    return manager


def _call(call_id: str, name: str, **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


async def _spawn(manager: SubagentManager, *, task: str = "Look into the notes", origin: str = "agent",
                 parent: str = CHAT, context: str | None = "owner: look into it\nagent: on it") -> str:
    started = json.loads(await manager.spawn(
        task=task, origin_channel="desktop", origin_chat_id="chat-1", parent_session_key=parent,
        origin=origin, context=context,
    ))
    assert started["status"] == "started", started
    provider = manager.provider
    if isinstance(provider, _Scripted):
        provider.run_id = started["run_id"]
    return started["run_id"]


async def _settle(manager: SubagentManager, run_id: str, rounds: int = 500) -> SubagentRunRecord:
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        record = manager.registry.get(run_id)
        if record is not None and record.ended_at is not None and not manager._running_tasks:
            return record
    return manager.registry.get(run_id)


async def _run(manager: SubagentManager, **kwargs: Any) -> SubagentRunRecord:
    return await _settle(manager, await _spawn(manager, **kwargs))


def _canonical(home: Path, thread_id: str) -> list[dict[str, Any]]:
    """The thread's canonical rows (metadata line first)."""
    path = home / "subagents" / "sessions" / f"subagent_{thread_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _session(manager: SubagentManager, thread_id: str):
    return manager.registry.thread_store.sessions.read(thread_session_key(thread_id))


# ── The conversation and where it lives ──────────────────────────────────────

@pytest.mark.asyncio
async def test_a_run_is_a_thread_whose_conversation_lives_apart(tmp_path, home):
    (tmp_path / "notes.txt").write_text("the notes")
    provider = _Scripted([
        LLMResponse(content="Reading.", tool_calls=[_call("c1", "read_file", path=str(tmp_path / "notes.txt"))],
                    usage={"prompt_tokens": 20, "completion_tokens": 4}),
        LLMResponse(content="Found: the notes.", usage={"prompt_tokens": 30, "completion_tokens": 5}),
    ])
    manager = _manager(home, tmp_path, provider)
    record = await _run(manager)
    run_id = record.run_id

    assert record.outcome == "ok"
    # The thread is the run's: same id, and the conversation key every client knows.
    assert (record.thread_id, record.turn, record.trigger) == (run_id, 1, "spawn")
    assert record.child_session_key == f"subagent:{run_id}" == thread_session_key(run_id)
    thread = manager.registry.get_thread(run_id)
    assert thread.status == "open" and thread.runs == [run_id] and thread.active_run_id is None
    assert thread.parent_session_key == CHAT and thread.origin == "agent"
    assert thread.mark == record.mark and thread.title == record.display_name and thread.model == MODEL
    assert thread.last_run_ended_at == record.ended_at and thread.usage == record.usage
    on_disk = json.loads((home / "subagents" / "threads.json").read_text())
    assert [t["thread_id"] for t in on_disk] == [run_id]

    sessions_dir = home / "subagents" / "sessions"
    canonical = sessions_dir / f"subagent_{run_id}.jsonl"
    display = sessions_dir / f"subagent_{run_id}.full.jsonl"
    assert canonical.is_file() and display.is_file()
    if POSIX:
        assert stat.S_IMODE(sessions_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(canonical.stat().st_mode) == 0o600
        assert stat.S_IMODE(display.stat().st_mode) == 0o600
    # Not among the owner's conversations.
    main_dir = home / "sessions"
    assert not main_dir.exists() or not any("subagent" in p.name for p in main_dir.iterdir())

    header = _canonical(home, run_id)[0]
    assert header["session_key"] == f"subagent:{run_id}"
    meta = header["metadata"]["subagent"]
    assert meta["threadId"] == run_id and meta["parentSessionKey"] == CHAT
    assert meta["runs"] == [run_id] and meta["systemPrompt"] == provider.sent[0][0]["content"]
    # The view clients read carries the thread.
    view = run_view(record, thread=thread)
    assert (view["threadId"], view["turn"], view["trigger"], view["threadStatus"], view["threadRuns"]) == (
        run_id, 1, "spawn", "open", 1)
    assert "legacy" not in view


@pytest.mark.asyncio
async def test_system_work_gets_no_thread_and_no_conversation(tmp_path, home):
    manager = _manager(home, tmp_path, _Scripted([LLMResponse(content="Nothing to save.")]))
    record = await _run(manager, origin="system", context=None)
    assert record.outcome == "ok" and record.thread_id == ""
    assert manager.registry.threads() == []
    assert not (home / "subagents" / "threads.json").exists()
    sessions_dir = home / "subagents" / "sessions"
    assert not sessions_dir.exists() or not list(sessions_dir.glob("*.jsonl"))
    view = run_view(record)
    assert view["threadId"] is None and view["threadStatus"] is None and "legacy" not in view


# ── Full fidelity ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_conversation_holds_what_the_provider_saw_uncut(tmp_path, home):
    files = []
    for index in range(8):
        path = tmp_path / f"part{index}.txt"
        path.write_text(f"part {index}: " + chr(ord("a") + index) * 2_000)
        files.append(path)
    script = [
        LLMResponse(content=f"Reading part {index}.", tool_calls=[_call(f"c{index}", "read_file", path=str(path))])
        for index, path in enumerate(files)
    ] + [LLMResponse(content="Every part read.", usage={"prompt_tokens": 50, "completion_tokens": 6})]
    provider = _Scripted(script)
    manager = _manager(home, tmp_path, provider)
    record = await _run(manager)
    assert record.outcome == "ok" and len(provider.sent) == 9

    history = _session(manager, record.run_id).get_history()
    last_sent = provider.sent[-1]
    # Same messages in the same order: the provider's last list (system prompt
    # aside) and the answer it gave.
    assert [(m["role"], m.get("tool_call_id")) for m in history] == (
        [(m["role"], m.get("tool_call_id")) for m in last_sent[1:]] + [("assistant", None)])
    assert history[0] == last_sent[1]  # the task, with the conversation it was given
    assert history[-1] == {"role": "assistant", "content": "Every part read."}
    # Each message as the provider first got it — the step cuts that later
    # shortened old tool results in the provider's list do not reach it.
    first_seen: dict[tuple, dict[str, Any]] = {}
    for sent in provider.sent:
        for position, message in enumerate(sent[1:]):
            first_seen.setdefault((position, message["role"]), message)
    for position, message in enumerate(history[:-1]):
        assert message == first_seen[(position, message["role"])], f"row {position} differs"
    cut = [m for m in last_sent if m["role"] == "tool" and len(m["content"]) < 2_000]
    assert cut, "the provider's list was cut by the step schedule"
    assert all(len(m["content"]) > 2_000 for m in history if m["role"] == "tool")
    for message in history:
        if message.get("tool_calls"):
            assert message["tool_calls"][0]["function"]["name"] == "read_file"


@pytest.mark.asyncio
async def test_a_long_tool_result_keeps_its_full_output_in_the_thread(tmp_path, home, monkeypatch):
    raw = "".join(f"line {i}: " + "w" * 60 + "\n" for i in range(800)) + "END"

    async def fetch(self, **kwargs: Any) -> str:
        return raw

    monkeypatch.setattr(WebFetchTool, "execute", fetch)
    provider = _Scripted([
        LLMResponse(content=None, tool_calls=[_call("f1", "web_fetch", url="https://example.com/page")]),
        LLMResponse(content="The page is long."),
    ])
    manager = _manager(home, tmp_path, provider)
    record = await _run(manager)
    run_id = record.run_id

    seen = provider.sent[1][-1]
    assert seen["role"] == "tool" and seen["content"].startswith(raw[:6_000])
    assert len(seen["content"]) < len(raw) and SPILL_POINTER_MARKER in seen["content"]
    spill_dir = home / "subagents" / "spill" / run_id
    spilled = list(spill_dir.glob("web_fetch-*.txt"))
    assert len(spilled) == 1 and str(spilled[0]) in seen["content"]
    assert spilled[0].read_text() == raw  # every character of it
    if POSIX:
        assert stat.S_IMODE(spilled[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(spill_dir.stat().st_mode) == 0o700
    # The conversation has exactly what the model got; the pointer leads to the rest.
    tool_rows = [m for m in _session(manager, run_id).messages if m["role"] == "tool"]
    assert len(tool_rows) == 1 and tool_rows[0]["content"] == seen["content"]
    assert tool_rows[0]["status"] == "ok" and tool_rows[0]["run_id"] == run_id
    # The transcript (older clients) is clipped as before.
    items = manager.registry.read_transcript(run_id)["items"]
    tool_item = next(i for i in items if i["kind"] == "tool")
    assert len(tool_item["content"]) < 2_100


@pytest.mark.asyncio
async def test_notes_from_the_main_agent_and_the_summary_request_are_kept(tmp_path, home):
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")

    async def main_agent_writes(provider: _Scripted) -> LLMResponse:
        await provider.manager.send_to_child(provider.run_id, "Check the year too.")
        return LLMResponse(content=None, tool_calls=[_call("c1", "read_file", path=str(tmp_path / "a.txt"))])

    provider = _Scripted([
        main_agent_writes,
        LLMResponse(content=None, tool_calls=[_call("c2", "read_file", path=str(tmp_path / "b.txt"))],
                    usage={"prompt_tokens": 9, "completion_tokens": 2}),
        LLMResponse(content="Summary: alpha and beta.", usage={"prompt_tokens": 11, "completion_tokens": 4}),
    ])
    manager = _manager(home, tmp_path, provider, max_iterations=2)
    record = await _run(manager)
    assert record.outcome == "ok"
    rows = _session(manager, record.run_id).messages
    note = next(m for m in rows if m.get("kind") == "agent_note")
    assert note["author"] == "main_agent" and note["content"].endswith("Check the year too.")
    grace = next(m for m in rows if m.get("content") == GRACE_PROMPT)
    assert grace["_display_hidden"] is True
    # The model saw it (working history); no one is shown it (display rows).
    assert {"role": "user", "content": GRACE_PROMPT} in _session(manager, record.run_id).get_history()
    display = manager.registry.thread_store.display_rows(record.run_id)
    assert GRACE_PROMPT not in [m.get("content") for m in display]
    closing = display[-1]
    assert closing["content"] == "Summary: alpha and beta." and closing["outcome"] == "ok"
    assert closing["run_id"] == record.run_id and closing["usage"]["total_tokens"] == record.usage["total"]


@pytest.mark.asyncio
async def test_a_stopped_run_is_saved_as_an_aborted_turn(tmp_path, home):
    gate = asyncio.Event()

    class _Waits(_Scripted):
        async def chat(self, **kwargs: Any) -> LLMResponse:
            self.sent.append(copy.deepcopy(kwargs["messages"]))
            await gate.wait()
            return LLMResponse(content="never")

    manager = _manager(home, tmp_path, _Waits())
    run_id = await _spawn(manager)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if manager.provider.sent:
            break
    assert json.loads(await manager.cancel(run_id))["status"] == "cancelled"
    rows = _session(manager, run_id).messages
    assert rows[0]["kind"] == "subagent_task" and rows[-1]["aborted"] is True
    assert rows[-1]["outcome"] == "cancelled"
    thread = manager.registry.get_thread(run_id)
    assert thread.active_run_id is None and thread.status == "open"


# ── subagents.history ────────────────────────────────────────────────────────

async def _thread_with_many_rows(tmp_path: Path, home: Path, monkeypatch, steps: int = 12) -> SubagentManager:
    """One finished thread of ``steps`` tool calls (2 rows each), its registry serving the RPCs."""
    script = []
    for index in range(steps):
        path = tmp_path / f"f{index}.txt"
        path.write_text(f"file {index}")
        script.append(LLMResponse(content=f"Step {index}.", tool_calls=[_call(f"c{index}", "read_file", path=str(path))]))
    script.append(LLMResponse(content="Finished every step."))
    manager = _manager(home, tmp_path, _Scripted(script), max_iterations=steps + 10)
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: manager.registry)
    record = await _run(manager)
    assert record.outcome == "ok", record.error
    return manager


@pytest.mark.asyncio
async def test_history_pages_the_whole_conversation_once(tmp_path, home, monkeypatch):
    # 223 rows in one turn: a page never splits a tool chain, but stops at 200.
    manager = await _thread_with_many_rows(tmp_path, home, monkeypatch, steps=110)
    run_id = manager.registry.all()[0].run_id
    everything = manager.registry.thread_store.display_rows(run_id)
    assert len(everything) == 222

    page = feature_rpc.subagents_history({"threadId": run_id, "limit": 5})
    assert len(page["messages"]) == 200
    assert page["schemaVersion"] == 1 and page["sessionKey"] == f"subagent:{run_id}"
    assert page["readOnly"] is True and page["historyPageVersion"] == 1 and page["hasOlder"]
    thread = page["thread"]
    assert thread["threadId"] == run_id and thread["status"] == "open" and thread["turns"] == 1
    assert [r["runId"] for r in thread["runs"]] == [run_id] and thread["runs"][0]["threadId"] == run_id
    found = page["messages"]
    while page["hasOlder"]:
        page = feature_rpc.subagents_history({"threadId": run_id, "limit": 5, "before": page["before"]})
        found = page["messages"] + found
    assert len(found) == len(everything) and len({m["id"] for m in found}) == len(found)
    assert page["before"] is None
    task = found[0]
    assert task["role"] == "user" and task["kind"] == "subagent_task" and task["author"] == "main_agent"
    assert task["withConversation"] is True and task["brief"] == "Look into the notes"
    assert task["turn"] == 1 and task["trigger"] == "spawn" and task["runId"] == run_id
    tools = [m for m in found if m["role"] == "tool"]
    assert len(tools) == 110 and {m["status"] for m in tools} == {"ok"} and tools[0]["name"] == "read_file"
    assert found[-1]["outcome"] == "ok" and found[-1]["content"] == [{"type": "text", "text": "Finished every step."}]
    # The same thread by one of its runs.
    by_run = feature_rpc.subagents_history({"runId": run_id})
    assert by_run["thread"]["threadId"] == run_id and by_run["messages"] == found[-200:]


@pytest.mark.asyncio
@pytest.mark.parametrize("params,code", [
    ({}, "INVALID"),
    ({"threadId": "short"}, "INVALID"),
    ({"threadId": "../../etc"}, "INVALID"),
    ({"threadId": "00000000-0000-4000-8000-000000000000"}, "NOT_FOUND"),
    ({"runId": "00000000-0000-4000-8000-000000000000"}, "NOT_FOUND"),
    ({"limit": 0}, "INVALID_HISTORY_CURSOR"),
    ({"before": "not-a-cursor"}, "INVALID_HISTORY_CURSOR"),
])
async def test_history_refuses_what_it_cannot_serve(tmp_path, home, monkeypatch, params, code):
    manager = await _thread_with_many_rows(tmp_path, home, monkeypatch, steps=1)
    run_id = manager.registry.all()[0].run_id
    if "limit" in params or "before" in params:
        params = {"threadId": run_id, **params}
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        feature_rpc.subagents_history(params)
    assert error.value.code == code


@pytest.mark.asyncio
async def test_history_of_an_older_run_says_its_transcript_is_what_there_is(tmp_path, home, monkeypatch):
    legacy_id = "11111111-2222-4333-8444-555555555555"
    # A record as an older build wrote it: no thread fields.
    (home / "subagents").mkdir(parents=True)
    (home / "subagents" / "runs.json").write_text(json.dumps([{
        "run_id": legacy_id, "child_session_key": f"subagent:{legacy_id}", "parent_session_key": CHAT,
        "parent_channel": "desktop", "parent_chat_id": "chat-1", "task": "Old task", "label": "Old task",
        "model": MODEL, "cleanup": "keep", "created_at": time.time() - 60, "ended_at": time.time() - 30,
        "outcome": "ok", "origin": "agent", "delivery_state": "delivered", "announced": True,
    }]))
    registry = SubagentRegistry(home / "subagents" / "runs.json")
    registry.append_transcript(legacy_id, {"kind": "brief", "content": "Old task"})
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: registry)
    record = registry.get(legacy_id)
    assert (record.thread_id, record.turn, record.trigger) == ("", 1, "spawn")
    view = feature_rpc.subagents_get({"runId": legacy_id})["task"]
    assert view["legacy"] is True and view["threadId"] == legacy_id and view["threadStatus"] == "closed"
    # Its transcript still opens; its history says why there is none.
    assert feature_rpc.subagents_transcript({"runId": legacy_id})["items"][0]["kind"] == "brief"
    for params in ({"runId": legacy_id}, {"threadId": legacy_id}):
        with pytest.raises(feature_rpc.FeatureRpcError) as error:
            feature_rpc.subagents_history(params)
        assert error.value.code == "SUBAGENT_THREAD_LEGACY"
    # Writing the index does not invent a threads file.
    registry.update(legacy_id, delivery_state="delivered")
    assert not (home / "subagents" / "threads.json").exists()


@pytest.mark.asyncio
async def test_history_is_read_by_whoever_may_read_the_conversation_that_started_it(tmp_path, home, monkeypatch):
    parent = "desktop:voice:call-1"
    sessions = SessionManager(tmp_path)
    owned = sessions.get_or_create(parent)
    owned.metadata["voiceOwner"] = {"kind": "account", "uid": "alice"}
    sessions.save(owned)
    manager = _manager(home, tmp_path, _Scripted([LLMResponse(content="Done.")]))
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: manager.registry)
    record = await _run(manager, parent=parent)

    with request_owner_scope(RequestOwner("alice")):
        result, _ = await feature_rpc.dispatch("subagents.history", {"threadId": record.run_id})
        assert result["thread"]["parentSessionKey"] == parent
    for other in (RequestOwner("bob"), HOST_OWNER):
        with request_owner_scope(other):
            with pytest.raises(feature_rpc.FeatureRpcError) as error:
                await feature_rpc.dispatch("subagents.history", {"threadId": record.run_id})
            assert error.value.code == "NOT_FOUND"


def _gateway(main_sessions: SessionManager | None) -> GatewayServer:
    server = object.__new__(GatewayServer)
    server.sessions = main_sessions
    server._ws_rpc_reply = AsyncMock()
    server._ws_rpc_error = AsyncMock()
    server._ws_send = AsyncMock()
    server._ws_clients = {}
    return server


@pytest.mark.asyncio
async def test_history_on_the_direct_gateway_and_chat_history_there(tmp_path, home, monkeypatch):
    manager = await _thread_with_many_rows(tmp_path, home, monkeypatch, steps=3)
    run_id = manager.registry.all()[0].run_id
    server = _gateway(SessionManager(tmp_path))
    ws = web.WebSocketResponse()

    await server._dispatch_ws_rpc(ws, "c1", {"id": "1", "method": "subagents.history",
                                            "params": {"threadId": run_id, "limit": 200}})
    server._ws_rpc_error.assert_not_awaited()
    result = server._ws_rpc_reply.await_args.args[2]
    assert result["thread"]["threadId"] == run_id and result["messages"][0]["kind"] == "subagent_task"

    server._ws_rpc_reply.reset_mock()
    await server._dispatch_ws_rpc(ws, "c1", {"id": "2", "method": "chat.history",
                                            "params": {"sessionKey": f"subagent:{run_id}", "limit": 200}})
    server._ws_rpc_error.assert_not_awaited()
    history = server._ws_rpc_reply.await_args.args[2]
    assert history["readOnly"] is True and history["sessionKey"] == f"subagent:{run_id}"
    assert [m["role"] for m in history["messages"]] == [m["role"] for m in result["messages"]]
    # Reading created nothing among the owner's conversations.
    assert not list(iter_session_files(home / "sessions"))
    assert f"subagent:{run_id}" not in server.sessions._cache


@pytest.mark.asyncio
async def test_history_on_the_relay(tmp_path, home, monkeypatch):
    manager = await _thread_with_many_rows(tmp_path, home, monkeypatch, steps=2)
    run_id = manager.registry.all()[0].run_id
    channel = WebChannel(WebChannelConfig(), MessageBus())
    ws = AsyncMock()
    await channel._dispatch_rpc(ws, {"type": "rpc", "id": "r1", "sessionId": "s1", "method": "subagents.history",
                                     "params": {"runId": run_id, "limit": 3}})
    frame = json.loads(ws.send.await_args.args[0])
    assert "error" not in frame, frame
    result = frame["result"]
    assert result["thread"]["threadId"] == run_id and result["readOnly"] is True
    assert [m["role"] for m in result["messages"]] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]


def _profiles(tmp_path: Path, monkeypatch) -> None:
    import flowly.profile as profiles

    monkeypatch.setattr(profiles, "_DEFAULT_HOME", tmp_path / "home")
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", tmp_path / "home" / "profiles")
    profiles.create_profile("writer", local_runtime=True)


@pytest.mark.asyncio
async def test_history_through_the_profile_host_and_writes_refused_there(tmp_path, home, monkeypatch):
    from flowly.profile_host import ProfileHost

    manager = await _thread_with_many_rows(tmp_path, home, monkeypatch, steps=2)
    run_id = manager.registry.all()[0].run_id
    _profiles(tmp_path, monkeypatch)
    host = ProfileHost()
    forwarded = []

    async def target(name, method, params, timeout, **kwargs):
        forwarded.append(method)
        result, _ = await feature_rpc.dispatch(method, params)
        return result

    host._target_rpc = target
    server = _gateway(None)
    server._profile_host = host
    server._profile_client_subscriptions = OrderedDict()
    server._profile_run_subscriptions = OrderedDict()
    server._profile_subagent_event_versions = {}
    ws = web.WebSocketResponse()
    await server._handle_profile_host_rpc(ws, "c1", "1", "profiles.rpc", {
        "name": "writer", "method": "subagents.history", "params": {"threadId": run_id, "limit": 50}})
    server._ws_rpc_error.assert_not_awaited()
    assert server._ws_rpc_reply.await_args.args[2]["thread"]["threadId"] == run_id
    assert forwarded == ["subagents.history"]

    await server._handle_profile_host_rpc(ws, "c1", "2", "profiles.rpc", {
        "name": "writer", "method": "chat.send",
        "params": {"sessionKey": f"subagent:{run_id}", "message": "hello"}})
    assert server._ws_rpc_error.await_args.args[2] == SUBAGENT_THREAD_READ_ONLY
    assert forwarded == ["subagents.history"], "a refused write never reaches the agent"


# ── Read only, on every path ─────────────────────────────────────────────────

THREAD_KEY = "subagent:22222222-3333-4444-8555-666666666666"
WRITES = [
    ("chat.send", {"sessionKey": THREAD_KEY, "message": "hi"}),
    ("chat.steer", {"sessionKey": THREAD_KEY, "runId": "r", "message": "hi"}),
    ("chat.compact", {"sessionKey": THREAD_KEY}),
    ("chat.clear", {"sessionKey": THREAD_KEY}),
    ("chat.retry", {"sessionKey": THREAD_KEY}),
    ("chat.undo", {"sessionKey": THREAD_KEY}),
    ("chat.abort", {"sessionKey": THREAD_KEY, "runId": "r"}),
    ("sessions.delete", {"key": THREAD_KEY}),
    ("sessions.model.set", {"sessionKey": THREAD_KEY, "model": "x"}),
    ("goal.pause", {"sessionKey": THREAD_KEY}),
    ("plan.mode.set", {"sessionKey": THREAD_KEY, "mode": "plan"}),
    ("subagents.spawn", {"sessionKey": THREAD_KEY, "task": "more"}),
    ("cron.add", {"sessionKey": THREAD_KEY, "name": "x"}),
]
READS = [
    ("chat.history", {"sessionKey": THREAD_KEY}),
    ("chat.inflight", {"sessionKey": THREAD_KEY}),
    ("subagents.history", {"threadId": THREAD_KEY.split(":", 1)[1]}),
    ("goal.get", {"sessionKey": THREAD_KEY}),
    ("plan.get", {"sessionKey": THREAD_KEY}),
]


@pytest.mark.parametrize("method,params", WRITES)
def test_every_write_naming_a_subagent_conversation_is_refused(method, params):
    with pytest.raises(SubagentThreadReadOnlyError) as error:
        require_rpc_subagent_access(method, params)
    assert error.value.code == SUBAGENT_THREAD_READ_ONLY
    # Another conversation is not affected.
    require_rpc_subagent_access(method, {k: ("desktop:x" if v == THREAD_KEY else v) for k, v in params.items()})


@pytest.mark.parametrize("method,params", READS)
def test_reads_may_name_a_subagent_conversation(method, params):
    require_rpc_subagent_access(method, params)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,params", WRITES)
async def test_the_direct_gateway_refuses_writes(home, method, params):
    server = _gateway(SessionManager(home))
    await server._dispatch_ws_rpc(web.WebSocketResponse(), "c1", {"id": "1", "method": method, "params": params})
    server._ws_rpc_reply.assert_not_awaited()
    assert server._ws_rpc_error.await_args.args[2] == SUBAGENT_THREAD_READ_ONLY
    assert not list(iter_session_files(home / "sessions"))


@pytest.mark.asyncio
@pytest.mark.parametrize("method,params", WRITES)
async def test_the_relay_refuses_writes(method, params):
    channel = WebChannel(WebChannelConfig(), MessageBus())
    ws = AsyncMock()
    await channel._dispatch_rpc(ws, {"type": "rpc", "id": "r1", "sessionId": "s1", "method": method,
                                     "params": params})
    frame = json.loads(ws.send.await_args.args[0])
    assert frame["error"]["code"] == SUBAGENT_THREAD_READ_ONLY and frame["id"] == "r1"
    assert ws.send.await_count == 1 and channel.bus.inbound.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,params", [w for w in WRITES if w[0] in {
    "chat.steer", "chat.compact", "goal.pause", "plan.mode.set", "subagents.spawn", "cron.add"}])
async def test_the_shared_feature_dispatch_refuses_writes(method, params):
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch(method, params)
    assert error.value.code == SUBAGENT_THREAD_READ_ONLY


@pytest.mark.parametrize("method,params", [
    ("chat.send", {"sessionKey": THREAD_KEY, "message": "hi"}),
    ("chat.steer", {"sessionKey": THREAD_KEY, "message": "hi"}),
    ("chat.compact", {"sessionKey": THREAD_KEY}),
    ("sessions.delete", {"key": THREAD_KEY}),
    ("sessions.model.set", {"sessionKey": THREAD_KEY, "model": "x"}),
    ("goal.pause", {"sessionKey": THREAD_KEY}),
])
def test_the_profile_host_contract_refuses_writes(method, params):
    with pytest.raises(ProfileHostError) as error:
        validate_profile_rpc(method, params)
    assert error.value.code == SUBAGENT_THREAD_READ_ONLY


@pytest.mark.parametrize("method,params", READS)
def test_the_profile_host_contract_lets_reads_through(method, params):
    assert validate_profile_rpc(method, params)[0] == method


# ── Nothing that reads the owner's conversations sees a thread ──────────────

@pytest.mark.asyncio
async def test_no_reader_of_the_owners_conversations_sees_a_thread(tmp_path, home, monkeypatch):
    from flowly.live_voice.recent_chat import latest_chat
    from flowly.mcp.server.readplane import SessionReader
    from flowly.session.indexer import SessionIndexer

    marker = "zebracrossingthreadmarker"
    manager = _manager(home, tmp_path, _Scripted([LLMResponse(content=f"Found the {marker}.")]))
    record = await _run(manager, task=f"Find the {marker}")
    assert marker in (home / "subagents" / "sessions" / f"subagent_{record.run_id}.full.jsonl").read_text()
    # One owner conversation exists beside it.
    owner = SessionManager(tmp_path)
    session = owner.get_or_create("desktop:owner-chat")
    session.add_message("user", "hello there")
    owner.save(session)

    assert [s["key"] for s in owner.list_sessions()] == ["desktop:owner-chat"]
    assert [p.name for p in iter_session_files(owner.sessions_dir)] == ["desktop_owner-chat.jsonl"]
    index = SessionIndexer(tmp_path / "index.sqlite")
    index.rebuild_from_sessions_dir(owner.sessions_dir)
    assert index.search("hello") and index.search(marker) == []
    reader = SessionReader()
    keys = [c["session_key"] for c in reader.conversations_list()["conversations"]]
    assert keys == ["desktop:owner-chat"]
    assert reader.messages_search("hello")["results"] and reader.messages_search(marker)["results"] == []
    assert "error" in reader.conversation_get(f"subagent:{record.run_id}")
    recent = latest_chat(owner.sessions_dir)
    assert recent is None or marker not in str(recent)
    # The agent's own file tools cannot open a thread's conversation either.
    denied = await ReadFileTool(workspace=tmp_path).execute(
        path=str(home / "subagents" / "sessions" / f"subagent_{record.run_id}.full.jsonl"))
    assert "Access denied" in denied and marker not in denied


# ── Retention, idle close, the cap ───────────────────────────────────────────

def _thread_run(registry: SubagentRegistry, *, parent: str = CHAT, created: float | None = None,
                origin: str = "agent", open_cap: int | None = None) -> SubagentRunRecord:
    import uuid

    run_id = str(uuid.uuid4())
    created = created if created is not None else time.time()
    record = SubagentRunRecord(
        run_id=run_id, child_session_key=f"subagent:{run_id}", parent_session_key=parent,
        parent_channel="desktop", parent_chat_id="chat-1", task="t", label="t", display_name=f"task {run_id[:4]}",
        model=MODEL, cleanup="keep", created_at=created, origin=origin, thread_id=run_id,
    )
    thread = SubagentThreadRecord(thread_id=run_id, parent_session_key=parent,
                                  child_session_key=f"subagent:{run_id}", created_at=created,
                                  origin=origin, title=record.display_name)
    registry.register(record, thread=thread, open_cap=open_cap)
    return registry.get(run_id)


def _age(registry: SubagentRegistry, run_id: str, *, ended_ago: float, delivery: str = "delivered",
         closed_ago: float | None = None) -> None:
    """Rewrite a finished run (and its thread) as if it ended ``ended_ago`` seconds ago."""
    now = time.time()
    runs = json.loads(registry._path.read_text())
    for run in runs:
        if run["run_id"] == run_id:
            run.update(ended_at=now - ended_ago, created_at=now - ended_ago - 10,
                       delivery_state=delivery, announced=delivery == "delivered", outcome="ok")
    registry._path.write_text(json.dumps(runs))
    threads = json.loads(registry._threads_path.read_text())
    for thread in threads:
        if thread["thread_id"] == run_id:
            thread.update(active_run_id=None, last_run_ended_at=now - ended_ago,
                          created_at=now - ended_ago - 10)
            if closed_ago is not None:
                thread.update(status="closed", closed_at=now - closed_ago, closed_reason="idle")
    registry._threads_path.write_text(json.dumps(threads))


def _files(registry: SubagentRegistry, run_id: str) -> list[Path]:
    store = registry.thread_store
    thread = registry.get_thread(run_id)
    store.commit_turn(thread, run_id=run_id, turn=1, trigger="spawn", system_prompt="s", first_message="t",
                      brief="t", journal=[], result="r", outcome="ok", usage=None, duration_ms=1, aborted=False)
    spilled = store.spill_dir(run_id)
    spilled.mkdir(parents=True, exist_ok=True)
    (spilled / "exec-1.txt").write_text("raw")
    return [store.sessions_dir / f"subagent_{run_id}.jsonl", store.sessions_dir / f"subagent_{run_id}.full.jsonl",
            spilled]


def test_an_idle_thread_closes_after_seven_days(home):
    registry = SubagentRegistry(home / "subagents" / "runs.json")
    idle = _thread_run(registry)
    recent = _thread_run(registry)
    running = _thread_run(registry)
    registry.finish(idle.run_id, "ok", result="x")
    registry.finish(recent.run_id, "ok", result="y")
    _age(registry, idle.run_id, ended_ago=THREAD_IDLE_CLOSE_SECONDS + 3_600)
    _age(registry, recent.run_id, ended_ago=THREAD_IDLE_CLOSE_SECONDS - 3_600)
    registry.maintain()

    closed = registry.get_thread(idle.run_id)
    assert closed.status == "closed" and closed.closed_reason == "idle"
    assert closed.closed_at == pytest.approx(closed.last_run_ended_at + THREAD_IDLE_CLOSE_SECONDS)
    assert registry.get_thread(recent.run_id).status == "open"
    assert registry.get_thread(running.run_id).status == "open"  # a running turn is never idle
    on_disk = {t["thread_id"]: t for t in json.loads(registry._threads_path.read_text())}
    assert on_disk[idle.run_id]["status"] == "closed"


def test_a_closed_thread_and_its_files_go_thirty_days_after_close(home):
    registry = SubagentRegistry(home / "subagents" / "runs.json")
    expired, kept, waiting = (_thread_run(registry) for _ in range(3))
    for record in (expired, kept, waiting):
        registry.finish(record.run_id, "ok", result="r")
    files = {record.run_id: _files(registry, record.run_id) for record in (expired, kept, waiting)}
    _age(registry, expired.run_id, ended_ago=40 * 86_400, closed_ago=THREAD_RETENTION_SECONDS + 60)
    _age(registry, kept.run_id, ended_ago=36 * 86_400, closed_ago=THREAD_RETENTION_SECONDS - 3_600)
    # Its result never reached the conversation: the run, and so the thread, stay.
    _age(registry, waiting.run_id, ended_ago=40 * 86_400, delivery="pending",
         closed_ago=THREAD_RETENTION_SECONDS + 60)
    registry.maintain()

    assert registry.get_thread(expired.run_id) is None
    assert all(not path.exists() for path in files[expired.run_id])
    assert registry.get_thread(kept.run_id) is not None and all(p.exists() for p in files[kept.run_id])
    assert registry.get_thread(waiting.run_id) is not None and all(p.exists() for p in files[waiting.run_id])
    assert {t["thread_id"] for t in json.loads(registry._threads_path.read_text())} == {
        kept.run_id, waiting.run_id}


def test_files_of_other_names_are_never_pruned(home):
    registry = SubagentRegistry(home / "subagents" / "runs.json")
    record = _thread_run(registry)
    registry.finish(record.run_id, "ok", result="r")
    _files(registry, record.run_id)
    stray = registry.thread_store.sessions_dir / "notes.jsonl"
    stray.write_text("{}")
    registry.maintain()
    assert stray.exists() and registry.get_thread(record.run_id) is not None


@pytest.mark.asyncio
async def test_the_cap_closes_the_thread_idle_longest_and_says_so(tmp_path, home):
    manager = _manager(home, tmp_path, _Scripted())
    registry = manager.registry
    now = time.time()
    olds = [_thread_run(registry, created=now - 1_000 + i) for i in range(MAX_OPEN_THREADS_PER_CONVERSATION)]
    for i, record in enumerate(olds):
        registry.finish(record.run_id, "ok", result="r")
        _age(registry, record.run_id, ended_ago=86_400 - i * 60)
    registry.maintain()
    oldest = olds[0]

    started = json.loads(await manager.spawn(task="One more", origin_channel="desktop", origin_chat_id="chat-1",
                                             parent_session_key=CHAT, origin="agent"))
    assert started["status"] == "started"
    assert (f"This conversation keeps at most {MAX_OPEN_THREADS_PER_CONVERSATION} subagents open; "
            f"starting this one closed '{oldest.display_name}', idle the longest. "
            "Its history stays readable. agent_close closes a subagent that is no longer "
            "needed.") in started["message"]
    closed = registry.get_thread(oldest.run_id)
    assert closed.status == "closed" and closed.closed_reason == "cap"
    # Its run's view says so, with a newer revision, so clients update it.
    view = run_view(registry.get(oldest.run_id), thread=closed)
    assert view["threadStatus"] == "closed"
    # The closed one takes no new task, and says why.
    followed = await manager.followup(oldest.run_id, "Also check B", parent_session_key=CHAT)
    assert followed["status"] == "closed" and f"closed to keep at most {MAX_OPEN_THREADS_PER_CONVERSATION} subagents" in followed["error"]
    assert len([t for t in registry.threads_for_parent(CHAT) if t.status == "open"]) == MAX_OPEN_THREADS_PER_CONVERSATION
    # Another conversation's threads are not counted.
    other = json.loads(await manager.spawn(task="Elsewhere", origin_channel="desktop", origin_chat_id="c2",
                                           parent_session_key="desktop:c2", origin="agent"))
    assert "keeps at most" not in other["message"]
    await _settle(manager, started["run_id"])
    await _settle(manager, other["run_id"])


@pytest.mark.asyncio
async def test_the_cap_refuses_when_every_open_thread_is_working(tmp_path, home):
    manager = _manager(home, tmp_path, _Scripted())
    for _ in range(MAX_OPEN_THREADS_PER_CONVERSATION):
        _thread_run(manager.registry)  # registered running, never finished
    with pytest.raises(ThreadCapReached):
        _thread_run(manager.registry, open_cap=MAX_OPEN_THREADS_PER_CONVERSATION)  # the registry refuses …
    # … and the agent reads why.
    refused = json.loads(await manager.spawn(task="One more", origin_channel="desktop", origin_chat_id="chat-1",
                                             parent_session_key=CHAT, origin="agent"))
    assert refused == {"status": "rejected", "error": (
        f"{MAX_OPEN_THREADS_PER_CONVERSATION} subagents of this conversation are open and all of them "
        "are working; one must finish, or be closed with agent_close, before another can start.")}
    assert len(manager.registry.all()) == MAX_OPEN_THREADS_PER_CONVERSATION


def test_the_limits_leave_room_for_a_conversations_full_share():
    # Every running subagent is an open thread, and the process limit must not
    # cut one conversation's share short.
    assert MAX_OPEN_THREADS_PER_CONVERSATION >= MAX_CHILDREN_PER_CONVERSATION
    assert _MAX_CONCURRENT_SUBAGENTS >= MAX_CHILDREN_PER_CONVERSATION


@pytest.mark.asyncio
async def test_many_running_subagents_are_stated_and_the_conversations_limit_holds(tmp_path, home):
    gate = asyncio.Event()

    async def wait(provider: _Scripted) -> LLMResponse:
        await gate.wait()
        return LLMResponse(content="Done.")

    manager = _manager(home, tmp_path, _Scripted([wait] * (MAX_CHILDREN_PER_CONVERSATION + 1)))

    async def start(n: int) -> dict[str, Any]:
        return json.loads(await manager.spawn(task=f"Task {n}", origin_channel="desktop",
                                              origin_chat_id="chat-1", parent_session_key=CHAT,
                                              origin="agent"))

    results = [await start(n) for n in range(MAX_CHILDREN_PER_CONVERSATION)]
    assert all(r["status"] == "started" for r in results)
    # Up to the threshold the result says nothing about how many run …
    assert not any("running at once" in r["message"] for r in results[:MANY_CHILDREN_RUNNING])
    # … past it, each start says how many are running now.
    first_many = results[MANY_CHILDREN_RUNNING]["message"]
    assert first_many.endswith(
        f" {MANY_CHILDREN_RUNNING + 1} subagents of this conversation are running at once (at most "
        f"{MAX_CHILDREN_PER_CONVERSATION}); each one uses its own model calls and tokens.")
    assert f" {MAX_CHILDREN_PER_CONVERSATION} subagents of this conversation" in results[-1]["message"]
    # The conversation's limit refuses one more; another conversation still starts.
    refused = await start(MAX_CHILDREN_PER_CONVERSATION)
    assert refused == {"status": "rejected", "error": (
        f"{MAX_CHILDREN_PER_CONVERSATION} subagents are already running for this conversation. "
        "Wait for one to finish, or do this part yourself.")}
    other = json.loads(await manager.spawn(task="Elsewhere", origin_channel="desktop",
                                           origin_chat_id="chat-2", parent_session_key="desktop:chat-2",
                                           origin="agent"))
    assert other["status"] == "started"
    assert "running at once" not in other["message"]
    gate.set()
    for r in [*results, other]:
        await _settle(manager, r["run_id"])


# ── The conversation that started it is deleted ─────────────────────────────

@pytest.mark.asyncio
async def test_deleting_the_parent_closes_its_threads_and_removes_their_conversations(tmp_path, home):
    manager = _manager(home, tmp_path, _Scripted([LLMResponse(content="Done.")]))
    finished = await _run(manager)
    files = [home / "subagents" / "sessions" / f"subagent_{finished.run_id}.jsonl",
             home / "subagents" / "sessions" / f"subagent_{finished.run_id}.full.jsonl"]
    assert all(p.exists() for p in files)
    other = await _run(manager, parent="desktop:other")

    # Through the agent's own conversation deletion.
    from flowly.agent.loop import AgentLoop

    owner = SessionManager(tmp_path)
    session = owner.get_or_create(CHAT)
    session.add_message("user", "hi")
    owner.save(session)
    fake = types.SimpleNamespace(
        _session_turn_locks={}, _title_tasks={}, _post_turn_compaction_tasks={}, sessions=owner,
        _last_turn_total_tokens={}, _started_sessions=set(), subagents=manager,
    )
    assert await AgentLoop.delete_session(fake, CHAT) is True

    thread = manager.registry.get_thread(finished.run_id)
    assert thread.status == "closed" and thread.closed_reason == "parent_deleted"
    assert not any(p.exists() for p in files)
    assert manager.registry.get(finished.run_id) is not None  # the run's record stays
    assert manager.registry.get_thread(other.run_id).status == "open"
    assert (home / "subagents" / "sessions" / f"subagent_{other.run_id}.jsonl").exists()


@pytest.mark.asyncio
async def test_a_run_still_working_for_a_deleted_conversation_saves_nothing(tmp_path, home):
    gate = asyncio.Event()

    async def wait(provider: _Scripted) -> LLMResponse:
        await gate.wait()
        return LLMResponse(content="Late result.")

    manager = _manager(home, tmp_path, _Scripted([wait]))
    run_id = await _spawn(manager)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if manager.provider.sent:
            break
    assert manager.forget_conversation(CHAT) == 1
    gate.set()
    record = await _settle(manager, run_id)
    assert record.outcome == "ok"
    assert not (home / "subagents" / "sessions" / f"subagent_{run_id}.jsonl").exists()
    assert manager.registry.get_thread(run_id).closed_reason == "parent_deleted"


# ── Restarts ─────────────────────────────────────────────────────────────────

from flowly.agent.tools.filesystem import WriteFileTool  # noqa: E402
from flowly.agent.tools.shell import ExecTool  # noqa: E402
from tests.test_subagent_resume import _dead_owner, _reference, _three_calls, _upto  # noqa: E402
from tests.test_subagent_resume import _manager as _resume_manager  # noqa: E402
from tests.test_subagent_resume import _Scripted as _ResumeScripted  # noqa: E402
from tests.test_subagent_resume import _settle as _resume_settle  # noqa: E402


@pytest.fixture
def side_effects(tmp_path, monkeypatch) -> Path:
    """write_file and exec only note that they ran (as in test_subagent_resume)."""
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


def _crash_with_thread(home: Path, reference: SubagentRunRecord, prefix: list[dict[str, Any]]) -> SubagentRunRecord:
    """The registry, thread and checkpoint as a stopped process left them at ``prefix``."""
    registry = SubagentRegistry(home / "runs.json")
    answered = {r["callId"] for r in prefix if r["type"] == "tool"}
    now = time.time()
    record = SubagentRunRecord(
        run_id=reference.run_id, child_session_key=reference.child_session_key,
        parent_session_key=reference.parent_session_key, parent_channel="web", parent_chat_id="chat-1",
        task=reference.task, label=reference.label, display_name=reference.display_name,
        model=MODEL, cleanup="keep", created_at=now - 600, started_at=now - 590,
        activity={"phase": "tool"}, origin="agent", mark=reference.mark, owner=_dead_owner(),
        resumable=True, thread_id=reference.run_id,
        tool_trace=[{"tool": r["name"], "status": "ok" if r["callId"] in answered else "running",
                     "args_bytes": 1, "result_bytes": 0, "duration_ms": 0, "started_at": r["t"]}
                    for r in prefix if r["type"] == "tool_start"],
    )
    thread = SubagentThreadRecord(thread_id=reference.run_id, parent_session_key=reference.parent_session_key,
                                  child_session_key=reference.child_session_key, created_at=now - 600,
                                  title=reference.display_name)
    registry.register(record, thread=thread)
    registry._runs[record.run_id].updated_at = now - 5
    with registry._lock:
        registry._write_locked()
    path = registry.checkpoint_path(record.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in prefix), encoding="utf-8")
    return registry.get(record.run_id)


def _rows(store_root: Path, run_id: str) -> list[dict[str, Any]]:
    path = store_root / "sessions" / f"subagent_{run_id}.jsonl"
    return [row for row in (json.loads(line) for line in path.read_text().splitlines() if line.strip())
            if row.get("_type") != "metadata"]


def _protocol(rows: list[dict[str, Any]]) -> list[tuple]:
    return [(r["role"], r.get("content"), r.get("tool_call_id"), json.dumps(r.get("tool_calls"), sort_keys=True),
             r.get("kind"), r.get("status"), r.get("_display_hidden")) for r in rows]


@pytest.mark.asyncio
async def test_the_checkpoint_rebuilds_the_journal_the_conversation_saved(tmp_path, monkeypatch, side_effects):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    saved = _rows(tmp_path / "reference", reference.run_id)
    restored = restore_child_state(full, annotate=annotate_search_repetition)
    # Task row, the journal, the closing row.
    assert _protocol(saved[1:-1]) == _protocol(restored.journal)
    assert saved[0]["content"] == restored.first_message and saved[0]["kind"] == "subagent_task"
    assert restored.system_prompt and restored.system_prompt == restore_child_state(
        _upto(full, "header"), annotate=annotate_search_repetition).system_prompt


@pytest.mark.asyncio
async def test_a_run_continued_after_a_crash_saves_its_whole_turn_once(tmp_path, monkeypatch, side_effects):
    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    side_effects.write_text("")
    home2 = tmp_path / "home2"
    _crash_with_thread(home2, reference, _upto(full, "tool", "c1"))
    provider = _ResumeScripted()
    manager = _resume_manager(home2, tmp_path, provider)
    manager.resume_pending()
    record = await _resume_settle(manager, reference.run_id)
    assert record.outcome == "ok" and side_effects.read_text() == ""

    rows = _rows(home2, reference.run_id)
    reference_rows = _rows(tmp_path / "reference", reference.run_id)
    assert [r.get("kind") for r in rows].count("subagent_task") == 1
    # Up to the call that finished before the crash, what the reference run saved.
    assert _protocol(rows[:3]) == _protocol(reference_rows[:3])
    assert [(r["tool_call_id"], r["status"]) for r in rows if r["role"] == "tool"] == [
        ("c1", "ok"), ("c2", "not_run"), ("c3", "not_run")]
    assert rows[-1]["content"] == "Done after the restart." and rows[-1]["outcome"] == "ok"
    thread = manager.registry.get_thread(reference.run_id)
    assert thread.active_run_id is None and thread.last_run_ended_at == record.ended_at


@pytest.mark.asyncio
async def test_a_turn_saved_just_before_a_crash_is_not_saved_again(tmp_path, monkeypatch, side_effects):
    import shutil

    (tmp_path / "notes.txt").write_text("notes")
    full, reference = await _reference(tmp_path, monkeypatch, _three_calls(tmp_path))
    home2 = tmp_path / "home2"
    _crash_with_thread(home2, reference, _upto(full, "final"))
    # The conversation already has the turn (saved before the registry recorded the result).
    shutil.copytree(tmp_path / "reference" / "sessions", home2 / "sessions")
    before = _rows(home2, reference.run_id)
    provider = _ResumeScripted()
    manager = _resume_manager(home2, tmp_path, provider)
    manager.resume_pending()
    record = await _resume_settle(manager, reference.run_id)
    assert record.outcome == "ok" and provider.sent == []
    assert _rows(home2, reference.run_id) == before
