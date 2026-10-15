"""What a subagent costs, how long its siblings wait, and what is kept.

* A subagent is another Flowly on one task: the main agent's iteration
  limit and no wall clock.
* Its tokens are counted as it runs and shown live (never a price).
* A finished result waits for slow siblings only so long, then goes, naming
  who is still working; a sibling stopped on request is named, not waited for.
* Every ended run leaves a metric line that outlives the run, summarized by
  ``subagents.stats``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from flowly.agent import subagent as subagent_module
from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_metrics import metric_line, metrics_path, read_metrics, summarize
from flowly.agent.subagent_observation import run_view
from flowly.agent.subagent_registry import SubagentRegistry, add_usage
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.feature_rpc import FeatureRpcError, dispatch
from flowly.providers.base import LLMProvider, LLMResponse

CHAT = "web:chat-1"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _Children(LLMProvider):
    """A child answers when its gate opens, reporting the tokens it used."""

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
                return LLMResponse(content=f"Result of {name}.",
                                   usage={"prompt_tokens": 1200, "completion_tokens": 300, "cache_read_tokens": 100})
        return LLMResponse(content="Result.", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def _manager(tmp_path, provider: LLMProvider, bus: MessageBus, **kwargs: Any) -> SubagentManager:
    return SubagentManager(provider=provider, workspace=tmp_path, bus=bus,
                           registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path, **kwargs)


async def _start(manager: SubagentManager, name: str, batch: str = "turn-1") -> str:
    return json.loads(await manager.spawn(
        task=f"task {name}", origin_channel="web", origin_chat_id="conn",
        parent_session_key=CHAT, origin="agent", display_name=name, batch_id=batch,
    ))["run_id"]


async def _no_notice(bus: MessageBus, seconds: float = 0.3) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.consume_inbound(), seconds)


# ── Limits ──────────────────────────────────────────────────────────────────

def test_a_subagent_gets_the_main_agents_iteration_limit(tmp_path):
    from flowly.agent.loop import AgentLoop
    from flowly.config.schema import Config

    loop = AgentLoop(bus=MessageBus(), provider=_Children(), workspace=tmp_path,
                     main_config=Config(), max_iterations=73, soft_warn_at_iteration=0)
    try:
        assert loop.subagents.max_iterations == 73
    finally:
        loop.stop()


@pytest.mark.asyncio
async def test_a_subagent_has_no_wall_clock(tmp_path, monkeypatch):
    seen: list[float] = []

    class _Timed(_Children):
        async def chat(self, **kwargs: Any) -> LLMResponse:
            seen.append(kwargs["timeout"])
            return LLMResponse(content="done")

    manager = _manager(tmp_path, _Timed(), MessageBus())
    await manager.spawn("look", origin_channel="web", origin_chat_id="c", wait=True)
    # Each model call gets the main agent's fixed timeout, not a share of a run budget.
    assert seen == [subagent_module._MODEL_CALL_TIMEOUT_SECONDS]


# ── Tokens ──────────────────────────────────────────────────────────────────

def test_tokens_add_up_across_calls():
    totals = add_usage({}, {"prompt_tokens": 100, "completion_tokens": 20})
    totals = add_usage(totals, {"prompt_tokens": 50, "completion_tokens": 5, "cache_read_tokens": 40, "total_tokens": 60})
    assert totals == {"input": 150, "output": 25, "cacheRead": 40, "total": 180}
    assert add_usage(totals, {"prompt_tokens": "bad"}) == totals


@pytest.mark.asyncio
async def test_a_runs_tokens_are_counted_and_shown_in_its_view(tmp_path):
    children = _Children()
    children.gates = {"A": asyncio.Event()}
    manager = _manager(tmp_path, children, MessageBus())
    run_id = await _start(manager, "A", batch="")
    assert run_view(manager.registry.get(run_id))["usage"]["totalTokens"] == 0
    children.gates["A"].set()
    for _ in range(100):
        if manager.registry.get(run_id).ended_at:
            break
        await asyncio.sleep(0.01)
    usage = run_view(manager.registry.get(run_id))["usage"]
    assert usage == {"inputTokens": 1200, "outputTokens": 300, "cacheReadTokens": 100, "totalTokens": 1500}


# ── A slow sibling ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_held_result_goes_after_a_while_naming_who_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent_module, "_BATCH_HOLD_SECONDS", 0.3)
    bus, children = MessageBus(), _Children()
    children.gates = {"A": asyncio.Event(), "B": asyncio.Event(), "C": asyncio.Event()}
    manager = _manager(tmp_path, children, bus)
    a = await _start(manager, "A")
    b = await _start(manager, "B")
    c = await _start(manager, "C")
    children.gates["A"].set()
    children.gates["B"].set()
    first = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of A." in first.content and "Result of B." in first.content
    assert "Still working, started in the same turn: 'C'" in first.content
    assert manager.registry.get(a).announced and manager.registry.get(b).announced
    children.gates["C"].set()
    second = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of C." in second.content and "Result of A." not in second.content
    assert "Still working" not in second.content
    await _no_notice(bus)
    assert manager.registry.get(c).announced


@pytest.mark.asyncio
async def test_the_whole_batch_arriving_in_time_needs_no_early_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent_module, "_BATCH_HOLD_SECONDS", 0.5)
    bus, children = MessageBus(), _Children()
    children.gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    manager = _manager(tmp_path, children, bus)
    await _start(manager, "A")
    await _start(manager, "B")
    children.gates["A"].set()
    await asyncio.sleep(0.1)
    children.gates["B"].set()
    notice = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "have all finished" in notice.content
    await _no_notice(bus, 0.7)  # the hold's timer finds nothing left to send


@pytest.mark.asyncio
async def test_a_sibling_stopped_on_request_is_named_not_waited_for(tmp_path):
    bus, children = MessageBus(), _Children()
    children.gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    manager = _manager(tmp_path, children, bus)
    await _start(manager, "A")
    b = await _start(manager, "B")
    await manager.cancel(b)
    children.gates["A"].set()
    notice = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Stopped before finishing, on request: 'B'" in notice.content
    assert f"run_id={b}" not in notice.content


# ── Metrics ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_ended_run_leaves_a_metric_without_its_content(tmp_path):
    children = _Children()
    children.gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    manager = _manager(tmp_path, children, MessageBus())
    a = await _start(manager, "A", batch="")
    b = await _start(manager, "B", batch="")
    children.gates["A"].set()
    await manager.cancel(b)
    for _ in range(100):
        if manager.registry.get(a).ended_at:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    rows = read_metrics(metrics_path(tmp_path / "runs.json"))
    by_run = {row["runId"]: row for row in rows}
    assert by_run[a]["outcome"] == "ok" and by_run[a]["tokens"]["total"] == 1500
    assert by_run[b]["outcome"] == "cancelled"
    text = (tmp_path / "metrics.jsonl").read_text()
    assert "task A" not in text and "Result of A" not in text


def test_the_summary_counts_a_period_by_day_and_leaves_system_runs_out():
    now = 1_800_000_000.0
    rows = [
        {"runId": "1", "endedAt": now - 100, "durationS": 10, "origin": "owner", "outcome": "ok", "tokens": {"total": 100}, "toolCalls": 3},
        {"runId": "2", "endedAt": now - 200, "durationS": 30, "origin": "agent", "outcome": "error", "tokens": {"total": 50}, "toolCalls": 1},
        {"runId": "3", "endedAt": now - 300, "durationS": 5, "origin": "system", "outcome": "ok", "tokens": {"total": 999}},
        {"runId": "4", "endedAt": now - 40 * 86_400, "durationS": 1, "origin": "owner", "outcome": "ok"},
        {"runId": "1", "endedAt": now - 90, "durationS": 12, "origin": "owner", "outcome": "ok", "tokens": {"total": 120}, "toolCalls": 3},
    ]
    stats = summarize(rows, days=30, now=now)
    assert stats["runs"] == 2  # run 1 once (as last recorded), run 2; system and old ones out
    assert stats["byOrigin"] == {"owner": 1, "agent": 1}
    assert stats["outcomes"]["ok"] == 1 and stats["outcomes"]["error"] == 1
    assert stats["tokens"]["total"] == 170
    assert stats["durationS"]["average"] == 21.0
    assert stats["byDay"][0]["runs"] == 2 and stats["byDay"][0]["failed"] == 1
    assert summarize(rows, days=30, now=now, include_system=True)["runs"] == 3


@pytest.mark.asyncio
async def test_the_stats_rpc_reads_the_metrics(tmp_path, monkeypatch):
    registry = SubagentRegistry(tmp_path / "runs.json")
    monkeypatch.setattr(feature_rpc, "_subagent_registry", lambda: registry)
    from flowly.agent.subagent_registry import SubagentRunRecord

    record = SubagentRunRecord(
        run_id="r1", child_session_key="subagent:r1", parent_session_key=CHAT, parent_channel="web",
        parent_chat_id="c", task="t", label="t", model="m", cleanup="keep", created_at=time.time() - 30,
        started_at=time.time() - 30, ended_at=time.time(), outcome="ok", origin="owner",
        usage={"input": 10, "output": 2, "cacheRead": 0, "total": 12},
    )
    line = json.dumps(metric_line(record))
    (tmp_path / "metrics.jsonl").write_text(line + "\nnot json\n")
    result, _ = await dispatch("subagents.stats", {"days": 7})
    assert result["runs"] == 1 and result["tokens"]["total"] == 12
    with pytest.raises(FeatureRpcError):
        await dispatch("subagents.stats", {"days": 0})


def test_other_profiles_can_stop_a_subagent_and_read_stats():
    from flowly.profile_host_contract import PROFILE_RPC_TIMEOUTS

    assert "subagents.cancel" in PROFILE_RPC_TIMEOUTS
    assert "subagents.stats" in PROFILE_RPC_TIMEOUTS


@pytest.mark.asyncio
async def test_a_stopped_sibling_does_not_start_the_hold_clock(tmp_path, monkeypatch):
    # The owner's case: one of three stopped at once, the other two finishing a
    # few minutes apart. The clock used to start at the stop, so the first result
    # went out alone just before the last one finished.
    monkeypatch.setattr(subagent_module, "_BATCH_HOLD_SECONDS", 0.5)
    bus, children = MessageBus(), _Children()
    children.gates = {"A": asyncio.Event(), "B": asyncio.Event(), "C": asyncio.Event()}
    manager = _manager(tmp_path, children, bus)
    await _start(manager, "A")
    b = await _start(manager, "B")
    await _start(manager, "C")
    await manager.cancel(b)
    await asyncio.sleep(0.3)
    children.gates["A"].set()
    await asyncio.sleep(0.3)  # past the stop + hold, within A + hold
    children.gates["C"].set()
    notice = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of A." in notice.content and "Result of C." in notice.content
    assert "Stopped before finishing, on request: 'B'" in notice.content
    await _no_notice(bus, 0.8)


@pytest.mark.asyncio
async def test_the_last_part_of_a_split_batch_asks_for_one_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent_module, "_BATCH_HOLD_SECONDS", 0.2)
    bus, children = MessageBus(), _Children()
    children.gates = {"A": asyncio.Event(), "C": asyncio.Event()}
    manager = _manager(tmp_path, children, bus)
    await _start(manager, "A")
    await _start(manager, "C")
    children.gates["A"].set()
    first = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of A." in first.content and "Still working" in first.content
    children.gates["C"].set()
    last = await asyncio.wait_for(bus.consume_inbound(), 3)
    assert "Result of C." in last.content
    assert "This completes the work started together with 'A'" in last.content
