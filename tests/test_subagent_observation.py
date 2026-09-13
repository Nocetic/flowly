"""Task history remains truthful, recoverable and responsive at failure boundaries."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from unittest.mock import AsyncMock
from weakref import WeakKeyDictionary

import pytest
from aiohttp import web

from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_observation import TaskEvents, run_view
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord
from flowly.agent.tools.delegate import DelegateTool
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.web import WebChannel
from flowly.config.schema import MultiAgentConfig, WebChannelConfig
from flowly.gateway.server import GatewayServer
from flowly.multiagent.invoke import run_subprocess
from flowly.providers.base import (
    LLMErrorInfo,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


def record(run_id="run-1", **changes):
    args = dict(run_id=run_id, child_session_key=f"subagent:{run_id}",
                parent_session_key="cli:parent", parent_channel="cli", parent_chat_id="parent",
                task="Investigate the issue", label="builtin:researcher", display_name="Investigate the issue",
                model="fake/model", cleanup="keep", created_at=time.time())
    return SubagentRunRecord(**(args | changes))


class Provider(LLMProvider):
    def __init__(self, response=None, action=None):
        super().__init__(api_key="unused")
        self.response = response or LLMResponse(content="Finished")
        self.action = action
        self.calls = 0

    def get_default_model(self):
        return "fake/model"

    async def chat(self, **kwargs):
        self.calls += 1
        if self.action:
            return await self.action(self.calls)
        return self.response


def manager(tmp_path, provider=None, **kwargs):
    return SubagentManager(provider=provider or Provider(), workspace=tmp_path, bus=MessageBus(),
                           registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path, **kwargs)


@pytest.mark.parametrize("code,status,category", [
    ("invalid_api_key", 401, "auth"),
    ("insufficient_credits", 402, "insufficient_credits"),
    ("context_length_exceeded", 400, "context_overflow"),
    ("input_too_large", 413, "input_too_large"),
])
async def test_provider_terminal_errors_are_never_success(tmp_path, code, status, category):
    provider = Provider(LLMResponse(content=code, finish_reason="error",
                                  error_info=LLMErrorInfo(status_code=status, code=code)))
    mgr = manager(tmp_path, provider)
    await mgr.spawn("Read report", wait=True)
    rec = mgr.registry.all()[0]
    assert rec.outcome == "error"
    assert rec.error_code == category
    assert rec.error
    assert rec.ended_at is not None
    assert len(mgr.registry.pending()) == 0
    await mgr._events.flush()


async def test_full_unicode_result_survives_context_cap_and_restart(tmp_path):
    from flowly.agent.assistants import AssistantRegistry
    assistants = AssistantRegistry(tmp_path / "assistants")
    assistants.reload()
    full = "Kanıt ve sonuç 🌱.\n" * 2500
    mgr = manager(tmp_path, Provider(LLMResponse(content=full)))
    inline = await mgr.spawn("Read report", wait=True, assistant=assistants.get("researcher"))
    assert len(inline) < len(full)
    restarted = SubagentRegistry(tmp_path / "runs.json")
    rec = restarted.all()[0]
    assert rec.artifact_ids
    assert len(run_view(rec)["resultPreview"]) == 1200
    offset, chunks = 0, []
    while True:
        page = restarted.read_result(rec.run_id, offset, 513)
        chunks.append(page["content"])
        if page["nextOffset"] is None:
            break
        offset = page["nextOffset"]
    assert "".join(chunks) == full
    assert rec.delivery_state == "not_required"
    await mgr._events.flush()


@pytest.mark.parametrize("how", ["cancel", "cascade", "shutdown"])
async def test_cancellation_before_worker_starts_is_terminal(tmp_path, how):
    mgr = manager(tmp_path)
    await mgr.spawn("Task", origin_channel="cli", origin_chat_id="parent", silent=True)
    rec = mgr.registry.all()[0]
    if how == "cancel":
        await mgr.cancel(rec.run_id)
    elif how == "cascade":
        assert mgr.cancel_children("cli:parent") == 1
    else:
        assert mgr.cancel_all() == 1
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await mgr._events.flush()
    assert SubagentRegistry(tmp_path / "runs.json").get(rec.run_id).outcome in ("cancelled", "interrupted")
    assert not mgr.registry.pending()
    assert mgr.get_running_count() == 0


async def test_trace_survives_cancellation_inside_tool(tmp_path, monkeypatch):
    from flowly.agent.tools.filesystem import ListDirTool
    entered = asyncio.Event()
    async def slow_tool(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(ListDirTool, "execute", slow_tool)
    response = LLMResponse(content=None, tool_calls=[ToolCallRequest(id="call-1", name="list_dir", arguments={"path": str(tmp_path)})])
    events = []
    async def capture(name, data):
        events.append((name, data))
    mgr = manager(tmp_path, Provider(response), on_event=capture)
    await mgr.spawn("Check folder", label="builtin:researcher", silent=True)
    await asyncio.wait_for(entered.wait(), 2)
    rec = mgr.registry.all()[0]
    disk = SubagentRegistry(tmp_path / "runs.json").get(rec.run_id)
    assert disk.tool_trace[0]["status"] == "running"
    assert disk.activity["phase"] == "tool"
    await mgr.cancel(rec.run_id)
    await mgr._events.flush()
    disk = SubagentRegistry(tmp_path / "runs.json").get(rec.run_id)
    assert disk.tool_trace[0]["status"] == "cancelled"
    assert len(disk.tool_trace) == 1
    completed = [d for name, d in events if name == "subagent.completed"]
    assert len(completed) == 1
    assert completed[0]["runId"] == rec.run_id
    assert completed[0]["label"] == "Check folder"
    assert completed[0]["running"] == 0
    assert [d["revision"] for _, d in events] == sorted({d["revision"] for _, d in events})


async def test_restart_preserves_tools_and_marks_interrupted_work(tmp_path):
    mgr = manager(tmp_path)
    rec = record(tool_trace=[{"tool": "read_file", "status": "running"}])
    mgr.registry.register(rec)
    mgr.resume_pending()
    await asyncio.sleep(0)
    after = SubagentRegistry(tmp_path / "runs.json").get(rec.run_id)
    assert after.outcome == "error"
    assert after.error_code == "process_restarted"
    assert after.tool_trace[0]["status"] == "error"
    assert not mgr.bus.inbound.empty()


def test_concurrent_registry_instances_do_not_lose_runs(tmp_path):
    path = tmp_path / "runs.json"
    def write(i):
        reg = SubagentRegistry(path)
        reg.register(record(str(i)))
        reg.finish(str(i), "ok", result=f"result {i}")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(30)))
    reopened = SubagentRegistry(path)
    assert len(reopened.all()) == 30
    for i in range(30):
        assert reopened.read_result(str(i))["content"] == f"result {i}"


def test_atomic_write_failure_preserves_prior_snapshot(tmp_path, monkeypatch):
    reg = SubagentRegistry(tmp_path / "runs.json")
    reg.register(record())
    old = (tmp_path / "runs.json").read_bytes()
    def failed(*args):
        raise OSError("disk full")
    monkeypatch.setattr(reg, "_atomic_write", failed)
    with pytest.raises(OSError):
        reg.finish("run-1", "ok", result="output")
    assert (tmp_path / "runs.json").read_bytes() == old
    assert reg.get("run-1").ended_at is None


def test_late_progress_cannot_reopen_or_replace_a_result(tmp_path):
    reg = SubagentRegistry(tmp_path / "runs.json")
    reg.register(record())
    reg.finish("run-1", "ok", result="original")
    revision = reg.get("run-1").revision
    reg.update("run-1", ended_at=None, outcome=None, activity={"phase": "thinking"})
    reg.finish("run-1", "error", result="stale")
    assert reg.get("run-1").revision == revision
    assert reg.read_result("run-1")["content"] == "original"


def test_retention_uses_completion_time_and_prunes_result_files(tmp_path, monkeypatch):
    reg = SubagentRegistry(tmp_path / "runs.json")
    reg.register(record(created_at=time.time() - 200000))
    reg.finish("run-1", "ok", result="recent output")
    reg.update("run-1", announced=True)
    assert len(reg.all()) == 1  # a long-running task is still recent
    now = time.time()
    monkeypatch.setattr("flowly.agent.subagent_registry.time.time", lambda: now + 86401)
    assert reg.all() == []
    assert SubagentRegistry(tmp_path / "runs.json").all() == []
    reg.register(record("new"))
    assert not list(reg._result_dir.glob("*.txt"))


def test_legacy_and_malformed_rows_do_not_invent_results(tmp_path):
    good = asdict(record())
    for key in ("result_preview", "result_chars", "result_available", "activity", "revision"):
        good.pop(key)
    path = tmp_path / "runs.json"
    path.write_text(json.dumps([None, {"run_id": "bad"}, good]))
    reg = SubagentRegistry(path)
    assert len(reg.all()) == 1
    assert not run_view(reg.get("run-1"))["resultAvailable"]
    with pytest.raises(FileNotFoundError):
        reg.read_result("run-1")


async def test_rpc_cursor_ties_and_transport_parity(tmp_path, monkeypatch):
    reg = SubagentRegistry(tmp_path / "runs.json")
    for i in range(3):
        reg.register(record(str(i), created_at=100))
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: reg)
    page, _ = await feature_rpc.dispatch("subagents.list", {"limit": 2})
    second, _ = await feature_rpc.dispatch("subagents.list", {"limit": 2, "cursor": page["nextCursor"]})
    assert [r["runId"] for r in page["tasks"] + second["tasks"]] == ["2", "1", "0"]
    direct, _ = await feature_rpc.dispatch("subagents.get", {"runId": "1"})
    ws = AsyncMock()
    channel = WebChannel(WebChannelConfig(), MessageBus())
    await channel._handle_feature_rpc(ws, "rpc-1", "browser-1", "subagents.get", {"runId": "1"})
    relay = json.loads(ws.send.call_args.args[0])
    assert relay["result"] == direct
    assert "browser-1" in channel._subagent_observers
    channel._ws = ws
    await channel.send_subagent_event("subagent.progress", direct["task"])
    assert json.loads(ws.send.call_args.args[0])["sessionId"] == "browser-1"
    channel._subagent_observers["browser-1"] = 0
    ws.reset_mock()
    await channel.send_subagent_event("subagent.progress", direct["task"])
    ws.send.assert_not_awaited()


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": True}, {"limit": 201}, {"cursor": "bad"}, {"status": "bogus"}])
def test_invalid_list_params_are_rejected(tmp_path, monkeypatch, params):
    reg = SubagentRegistry(tmp_path / "runs.json")
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: reg)
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        feature_rpc.subagents_list(params)
    assert exc.value.code == "INVALID"


def test_broken_registry_is_unavailable_never_empty(tmp_path, monkeypatch):
    reg = SubagentRegistry(tmp_path / "runs.json")
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: reg)
    (tmp_path / "runs.json").write_text('{broken')
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        feature_rpc.subagents_list({})
    assert exc.value.code == "UNAVAILABLE"
    with pytest.raises(ValueError):
        reg.register(record())
    assert (tmp_path / "runs.json").read_text() == '{broken'


async def test_slow_event_consumer_is_bounded_and_coalesced():
    delivered = []
    async def slow(event, data):
        await asyncio.sleep(0.01)
        delivered.append(data)
    events = TaskEvents(slow, capacity=2, timeout=0.1)
    for revision in range(100):
        events.publish("subagent.progress", {"runId": "a", "revision": revision})
    assert len(events._pending) == 1
    await events.flush()
    assert [d["revision"] for d in delivered] == [99]
    hanging = TaskEvents(lambda *_: asyncio.Event().wait(), timeout=0.01)
    hanging.publish("subagent.completed", {"runId": "a", "revision": 100})
    await asyncio.wait_for(hanging.flush(), 0.2)


async def test_gateway_slow_observer_does_not_hold_fast_observer():
    server = object.__new__(GatewayServer)
    server._subagent_event_versions = WeakKeyDictionary()
    slow, fast = web.WebSocketResponse(), web.WebSocketResponse()
    server._ws_clients = {"slow": slow, "fast": fast}
    seen = asyncio.Event()
    async def send(ws, data):
        if ws is slow:
            await asyncio.Event().wait()
        else:
            seen.set()
    server._ws_send = send
    broadcast = asyncio.create_task(server._broadcast_subagent_event("subagent.progress", {}))
    await asyncio.wait_for(seen.wait(), 0.1)
    await asyncio.wait_for(broadcast, 1.5)


async def test_delegate_uses_same_history_and_persists_result(tmp_path, monkeypatch):
    monkeypatch.setattr("flowly.agent.tools.delegate.invoke_agent", AsyncMock(return_value="Finished report"))
    reg = SubagentRegistry(tmp_path / "runs.json")
    delegate = DelegateTool({"writer": MultiAgentConfig()}, {}, tmp_path, MessageBus(), registry=reg)
    delegate.set_context("web", "conversation")
    await delegate.execute("writer", "Write report")
    await asyncio.gather(*list(delegate._tasks.values()))
    await delegate._events.flush()
    rec = reg.all()[0]
    assert rec.kind == "delegate"
    assert rec.outcome == "ok"
    assert rec.parent_session_key == "web:conversation"
    assert reg.read_result(rec.run_id)["content"] == "Finished report"
    assert rec.delivery_state == "queued"


@pytest.mark.parametrize("before_start", [True, False])
async def test_shared_cancel_reaches_delegate(tmp_path, monkeypatch, before_start):
    entered = asyncio.Event()
    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr("flowly.agent.tools.delegate.invoke_agent", wait)
    reg = SubagentRegistry(tmp_path / "runs.json")
    delegate = DelegateTool({"writer": MultiAgentConfig()}, {}, tmp_path, MessageBus(), registry=reg)
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: reg)
    monkeypatch.setattr(feature_rpc, "_subagent_delegate_provider", lambda: delegate)
    await delegate.execute("writer", "Write report")
    if not before_start:
        await entered.wait()
    rec = reg.all()[0]
    reply, _ = await feature_rpc.dispatch("subagents.cancel", {"runId": rec.run_id})
    assert reply["status"] == "cancelled"
    assert not reg.pending()
    again, _ = await feature_rpc.dispatch("subagents.cancel", {"runId": rec.run_id})
    assert again == reply
    await delegate._events.flush()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group teardown")
async def test_cancel_reaps_real_subprocess(tmp_path):
    pid_file = tmp_path / "pid"
    script = "import os,time,pathlib; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(60)"
    task = asyncio.create_task(run_subprocess([sys.executable, "-c", script], str(tmp_path)))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_wait_tasks_count_toward_concurrency_limit(tmp_path):
    entered = asyncio.Event()
    async def wait(_):
        entered.set()
        await asyncio.Event().wait()
    mgr = manager(tmp_path, Provider(action=wait))
    sync_task = asyncio.create_task(mgr.spawn("first", wait=True))
    await entered.wait()
    for i in range(4):
        await mgr.spawn(f"Task {i}", silent=True)
    denied = json.loads(await mgr.spawn("sixth", silent=True))
    assert denied["status"] == "rejected"
    mgr.cancel_all()
    await asyncio.gather(sync_task, return_exceptions=True)
    await asyncio.sleep(0)
    await mgr._events.flush()
    assert not mgr.registry.pending()


async def test_delegate_failure_never_has_successful_result(tmp_path, monkeypatch):
    monkeypatch.setattr("flowly.agent.tools.delegate.invoke_agent", AsyncMock(side_effect=RuntimeError("private diagnostic")))
    reg = SubagentRegistry(tmp_path / "runs.json")
    delegate = DelegateTool({"writer": MultiAgentConfig()}, {}, tmp_path, MessageBus(), registry=reg)
    await delegate.execute("writer", "Write report")
    await asyncio.gather(*list(delegate._tasks.values()))
    await delegate._events.flush()
    rec = reg.all()[0]
    assert rec.outcome == "error"
    assert not rec.result_available
    assert "private diagnostic" not in json.dumps(run_view(rec))


def test_missing_cli_final_output_is_an_error():
    from flowly.multiagent.invoke import parse_codex_jsonl
    with pytest.raises(RuntimeError):
        parse_codex_jsonl('{"type":"turn.failed"}')


@pytest.mark.parametrize("params", [{"offset": -1}, {"offset": True}, {"limit": 200001}, {"limit": 0}])
def test_result_ranges_are_bounded(tmp_path, monkeypatch, params):
    reg = SubagentRegistry(tmp_path / "runs.json")
    reg.register(record())
    reg.finish("run-1", "ok", result="result")
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: reg)
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        feature_rpc.subagents_result({"runId": "run-1", **params})
    assert exc.value.code == "INVALID"


async def test_failed_history_request_does_not_subscribe_relay(tmp_path, monkeypatch):
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: None)
    channel = WebChannel(WebChannelConfig(), MessageBus())
    ws = AsyncMock()
    await channel._handle_feature_rpc(ws, "rpc-1", "browser-1", "subagents.list", {})
    assert json.loads(ws.send.call_args.args[0])["error"]["code"] == "UNAVAILABLE"
    assert not channel._subagent_observers


async def test_cancel_prefix_is_compatible_and_ambiguous_ids_are_rejected(tmp_path, monkeypatch):
    reg = SubagentRegistry(tmp_path / "runs.json")
    reg.register(record("abcdefgh-first"))
    reg.register(record("abcdefgh-second"))
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: reg)
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        await feature_rpc.subagents_cancel({"runId": "abcdefgh"})
    assert exc.value.code == "CONFLICT"
    reg.finish("abcdefgh-first", "ok", result="done")
    reply = await feature_rpc.subagents_cancel({"runId": "abcdefgh-f"})
    assert reply["status"] == "ok"
    assert reply["runId"] == "abcdefgh-first"


async def test_corrupt_history_does_not_prevent_host_construction(tmp_path):
    (tmp_path / "runs.json").write_text('{broken')
    mgr = manager(tmp_path)
    mgr.resume_pending()
    with pytest.raises(ValueError):
        await mgr.spawn("Should not start")
    assert (tmp_path / "runs.json").read_text() == '{broken'
    assert mgr.get_running_count() == 0


async def test_terminal_state_wins_cancel_after_finish(tmp_path):
    mgr = manager(tmp_path)
    await mgr.spawn("already done", wait=True)
    rec = mgr.registry.all()[0]
    # Simulate the brief interval before the done callback removes the handle.
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    mgr._running_tasks[rec.run_id] = done
    reply = json.loads(await mgr.cancel(rec.run_id))
    assert reply["status"] == "ok"
    assert rec.outcome == "ok"
    mgr._running_tasks.clear()
    await mgr._events.flush()


async def test_timeout_preserves_finished_tool_history(tmp_path):
    async def next_response(call):
        if call == 1:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="first", name="list_dir", arguments={"path": str(tmp_path)},
            )])
        await asyncio.Event().wait()
    mgr = manager(tmp_path, Provider(action=next_response))
    await mgr.spawn("Read and think", wait=True, timeout_seconds=0.05)
    rec = mgr.registry.all()[0]
    assert rec.outcome == "timeout"
    assert len(rec.tool_trace) == 1
    assert rec.tool_trace[0]["status"] == "ok"
    await mgr._events.flush()


async def test_disk_failure_does_not_leak_concurrency_slots(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    await mgr.spawn("Task", silent=True)
    def failed(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(mgr.registry, "finish", failed)
    mgr.cancel_all()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await mgr._events.flush()
    assert mgr.get_running_count() == 0
    assert not mgr._children_by_parent
