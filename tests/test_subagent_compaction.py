"""A subagent manages its context the way the main agent does.

The child shares the main agent's ``CompactionService``: the window of the
model it runs on (clamped by the proxy cap and any input ceiling a provider
taught the service), the same threshold, microcompact of old tool results,
per-request fitting with a summary as the last resort, and one retry after a
context overflow. The system prompt and the brief are never summarized. A
summary is a model call: its tokens count toward the run, clients see the
run ``compacting``, the transcript gets a divider, and the checkpoint keeps
the list it produced (``test_subagent_checkpoint.py`` replays it).
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from flowly.agent.subagent import (
    _SUBAGENT_MAX_OUTPUT_TOKENS,
    CHILD_SUMMARY_INSTRUCTIONS,
    SubagentManager,
    subagent_output_budget,
)
from flowly.agent.subagent_checkpoint import read_checkpoint, restore_child_state
from flowly.agent.subagent_registry import SubagentRegistry
from flowly.agent.tools.discovery import annotate_search_repetition
from flowly.bus.queue import MessageBus
from flowly.compaction.estimator import estimate_messages_tokens
from flowly.compaction.pruning import iteration_strip_passes
from flowly.compaction.request_guard import ProviderRequestCoordinator
from flowly.compaction.service import CompactionService
from flowly.compaction.summarizer import SUMMARIZE_SYSTEM_PROMPT, collect_summary_usage
from flowly.compaction.types import CompactionConfig, is_summary_message
from flowly.providers.base import LLMErrorInfo, LLMProvider, LLMResponse, ToolCallRequest

CHAT = "web:chat-1"
MODEL = "m"
SMALL_MODEL = "openai/gpt-3.5-turbo"  # 16,385 tokens


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


_LINE = "2026-08-04 04:12:33 INFO worker {n} finished batch {i} in 182ms; queue depth 7\n"


def _log_file(n: int, chars: int) -> str:
    return "".join(_LINE.format(n=n, i=i) for i in range(chars // 60 + 2))[:chars]


def _summary_call(messages: list[dict[str, Any]]) -> bool:
    return bool(messages) and messages[0].get("content") == SUMMARIZE_SYSTEM_PROMPT


class _Provider(LLMProvider):
    """The child's calls come from ``answer``; summary calls from ``summarize``.

    Each child call is checked against the threshold it was fitted to.
    """

    def __init__(self, answer, summarize=None) -> None:
        super().__init__(api_key="unused")
        self.answer = answer
        self.summarize = summarize or (lambda _n: LLMResponse(
            content="Record: the logs were read; batch 41 was slow.",
            usage={"prompt_tokens": 40, "completion_tokens": 9}))
        self.calls: list[dict[str, Any]] = []
        self.summary_calls: list[list[dict[str, Any]]] = []
        self.manager: SubagentManager | None = None
        self.run_id = ""

    def get_default_model(self) -> str:
        return MODEL

    async def chat(self, **kwargs: Any) -> LLMResponse:
        if _summary_call(kwargs["messages"]):
            self.summary_calls.append(copy.deepcopy(kwargs["messages"]))
            return self.summarize(len(self.summary_calls))
        call = {"messages": copy.deepcopy(kwargs["messages"]), "tools": kwargs.get("tools"),
                "max_tokens": kwargs.get("max_tokens"), "model": kwargs.get("model")}
        if self.manager is not None:
            compaction = self.manager.compaction
            call["threshold"] = compaction.compaction_threshold_for(call["model"])
            call["window"] = compaction.effective_context_window_for(call["model"])
            call["reserve"] = compaction.effective_reserve_tokens_for(call["model"])
            call["size"] = (estimate_messages_tokens(call["messages"])
                            + ProviderRequestCoordinator._tool_tokens(call["tools"]))
            call["log"] = read_checkpoint(self.manager.registry.checkpoint_path(self.run_id))
        self.calls.append(call)
        response = self.answer(len(self.calls), call)
        if asyncio.iscoroutine(response):
            response = await response
        return response


def _manager(tmp_path: Path, provider: _Provider, *, window: int | None = None,
             compaction: CompactionService | None = None) -> SubagentManager:
    if compaction is None and window is not None:
        compaction = CompactionService(provider=provider, model=MODEL, config=CompactionConfig(
            context_window=window, context_window_explicit=True))
    manager = SubagentManager(provider=provider, workspace=tmp_path, bus=MessageBus(),
                              registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path,
                              max_iterations=60, compaction=compaction)
    manager.resume_enabled = True
    provider.manager = manager
    return manager


async def _run(manager: SubagentManager, provider: _Provider, *, model: str = MODEL,
               task: str = "Find the slowest batch") -> str:
    started = json.loads(await manager.spawn(
        task=task, origin_channel="web", origin_chat_id="chat-1", parent_session_key=CHAT,
        origin="agent", model=model, context="owner: which batch was slow?\nagent: checking"))
    provider.run_id = started["run_id"]
    for _ in range(1_500):
        await asyncio.sleep(0.01)
        if manager.registry.get(started["run_id"]).ended_at is not None and not manager._running_tasks:
            break
    return started["run_id"]


def _read(call_id: str, path: Path) -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id=call_id, name="read_file", arguments={"path": str(path)})],
        usage={"prompt_tokens": 100, "completion_tokens": 5})


# ── Window, threshold and the reply budget are the main agent's ─────────────

@pytest.mark.parametrize(
    "model", [SMALL_MODEL, "openai/gpt-4o", "anthropic/claude-haiku-4.5", "google/gemini-2.5-pro",
              "unknown/model"],
)
def test_the_request_and_its_reply_fit_the_childs_model(tmp_path, model):
    """The request is fitted to the main agent's threshold for the child's
    model (window − reserve) and the reply budget stays inside the reserve,
    so the two together never exceed the window."""
    manager = _manager(tmp_path, _Provider(lambda *_: LLMResponse(content="x")))
    compaction = manager.compaction
    window = compaction.effective_context_window_for(model)
    threshold = compaction.compaction_threshold_for(model)
    reply = manager._output_budget(model)
    assert threshold + reply <= window
    assert reply >= 512 and threshold > window * 0.5, "room to think and to answer"


def test_a_large_model_keeps_the_full_reply_budget():
    assert subagent_output_budget(200_000, 32_000) == _SUBAGENT_MAX_OUTPUT_TOKENS
    assert subagent_output_budget(16_385, 5_734) == 4_096
    assert subagent_output_budget(2_000, 512) == 512


def test_a_child_uses_the_service_it_is_given_and_learns_with_it(tmp_path):
    """AgentLoop hands its own service to its subagents: an input ceiling a
    child learns from a 413 is the main agent's too, and the reverse."""
    shared = CompactionService(provider=None, model=MODEL)
    manager = _manager(tmp_path, _Provider(lambda *_: LLMResponse(content="x")), compaction=shared)
    assert manager.compaction is shared
    shared.note_observed_input_limit(MODEL, 50_000, reason="main agent")
    assert manager.compaction.compaction_threshold_for(MODEL) == 50_000 - 17_500


def test_the_agent_loop_shares_its_compaction_with_its_subagents(tmp_path):
    from flowly.agent.loop import AgentLoop
    from flowly.config.schema import Config

    loop = AgentLoop(bus=MessageBus(), provider=_Provider(lambda *_: LLMResponse(content="x")),
                     workspace=tmp_path, main_config=Config())
    assert loop.subagents.compaction is loop.compaction


def test_the_step_cuts_follow_the_main_loops_schedule():
    assert iteration_strip_passes(5) == []
    assert iteration_strip_passes(6) == [(3, 300)]
    assert iteration_strip_passes(9) == [(3, 300), (2, 150)]


# ── Per request ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_request_under_the_threshold_goes_out_as_it_is(tmp_path, monkeypatch):
    phases: list[str] = []
    real = SubagentManager._progress

    async def watch(self, run_id, activity):
        phases.append(activity.get("phase"))
        await real(self, run_id, activity)

    monkeypatch.setattr(SubagentManager, "_progress", watch)
    (tmp_path / "a.log").write_text("short")
    provider = _Provider(lambda n, _c: _read("c1", tmp_path / "a.log") if n == 1
                         else LLMResponse(content="Done."))
    manager = _manager(tmp_path, provider)
    run_id = await _run(manager, provider)
    assert manager.registry.get(run_id).outcome == "ok"
    assert provider.summary_calls == [] and "compacting" not in phases
    kinds = {r["type"] for r in provider.calls[-1]["log"]}
    assert not kinds & {"microcompact", "compacted"}


@pytest.mark.asyncio
async def test_a_failed_summary_is_retried_like_a_transient_error(tmp_path, monkeypatch):
    """The summary is a model call and may fail; the request is then not
    sent, and the step is tried again within the run's error budget."""
    failures = iter([True, True])

    def summarize(_n: int) -> LLMResponse:
        if next(failures, False):
            return LLMResponse(content="Error calling LLM: upstream 502", finish_reason="error",
                               usage={"prompt_tokens": 30, "completion_tokens": 0})
        return LLMResponse(content="Record: the draft was written.",
                           usage={"prompt_tokens": 40, "completion_tokens": 9})

    def answer(n: int, _call) -> LLMResponse:
        if n == 1:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="w", name="write_file",
                arguments={"path": str(tmp_path / "d.md"), "content": "R" * 12_000})])
        return LLMResponse(content="Report done.")

    from flowly.agent import error_classifier

    monkeypatch.setattr(error_classifier, "backoff_for", lambda *_a, **_k: 0.0)
    provider = _Provider(answer, summarize)
    manager = _manager(tmp_path, provider, window=12_000)
    run_id = await _run(manager, provider)
    record = manager.registry.get(run_id)
    assert record.outcome == "ok" and record.result_preview.startswith("Report done")
    assert len(provider.calls) == 2, "the unfit request was never sent"
    assert len(provider.summary_calls) >= 3
    assert is_summary_message(provider.calls[1]["messages"][1])
    # Failed summary calls cost tokens too.
    assert record.usage["input"] >= 30 * 2 + 40


@pytest.mark.asyncio
async def test_a_request_that_cannot_be_made_to_fit_is_not_sent(tmp_path):
    """A window the system prompt, the brief and the tool schemas already
    fill: nothing to compact, so no request goes out and the run ends with
    the input-limit error — as the main agent fails closed."""
    provider = _Provider(lambda *_: LLMResponse(content="never"))
    manager = _manager(tmp_path, provider, window=3_000)
    run_id = await _run(manager, provider)
    record = manager.registry.get(run_id)
    assert provider.calls == [] and provider.summary_calls == []
    assert record.outcome == "error" and record.error_code == "input_too_large"


@pytest.mark.asyncio
async def test_a_context_overflow_is_compacted_and_sent_once_more_not_forever(tmp_path):
    """Over the window by the provider's count though under it by ours: the
    request is fitted harder (a summary here) and retried once. A second
    overflow on the retry ends the run instead of looping."""
    for n in range(3):
        (tmp_path / f"f{n}.log").write_text(_log_file(n, 3_000))
    overflow = LLMResponse(content="This model's maximum context length is 8192 tokens",
                           finish_reason="error", error_info=LLMErrorInfo(code="context_length_exceeded"))

    def answer(n: int, _call) -> LLMResponse:
        if n <= 3:
            return _read(f"c{n}", tmp_path / f"f{n - 1}.log")
        return overflow

    provider = _Provider(answer)
    manager = _manager(tmp_path, provider)
    run_id = await _run(manager, provider)
    record = manager.registry.get(run_id)
    assert len(provider.calls) == 5, "the overflowed call, then one retry"
    first, retry = provider.calls[3], provider.calls[4]
    assert retry["size"] < first["size"] and is_summary_message(retry["messages"][1])
    assert retry["messages"][2] == first["messages"][1], "the brief survives verbatim"
    log = provider.calls[4]["log"]
    assert [(r["reason"], r["summarized"]) for r in log if r["type"] == "compacted"] == [("overflow", True)]
    assert record.outcome == "error" and record.error_code == "context_overflow"


# ── A long run on a small-window model ──────────────────────────────────────

@pytest.mark.asyncio
async def test_a_long_run_on_a_small_window_model_compacts_and_finishes(tmp_path):
    """Forty reads of 3,000-character logs on a 16K model: far more than the
    window holds. The run crosses the threshold again and again — old results
    cut, then summaries — and returns its answer. Every request it sent fit
    the threshold, every reply budget the reserve, and the checkpoint
    replays to exactly what the provider saw at each call."""
    reads = 40
    for n in range(reads):
        (tmp_path / f"f{n}.log").write_text(_log_file(n, 3_000))

    def answer(n: int, _call) -> LLMResponse:
        if n <= reads:
            # The model thinks aloud at each step: text no cut can shorten.
            step = _read(f"c{n}", tmp_path / f"f{n - 1}.log")
            step.content = f"Log {n - 1} read. " + " ".join(
                f"Worker {n} batch {i} took {100 + i} ms." for i in range(30))
            return step
        return LLMResponse(content="Batch 41 of worker 7 was the slowest.",
                           usage={"prompt_tokens": 100, "completion_tokens": 5})

    provider = _Provider(answer)
    manager = _manager(tmp_path, provider)
    run_id = await _run(manager, provider, model=SMALL_MODEL)

    record = manager.registry.get(run_id)
    assert record.outcome == "ok", record.error
    assert record.result_preview.startswith("Batch 41")
    assert len(provider.calls) == reads + 1
    assert provider.calls[0]["window"] == 16_385
    for call in provider.calls:
        assert call["model"] == SMALL_MODEL
        assert call["size"] <= call["threshold"], "every request fit the threshold"
        assert call["max_tokens"] <= call["reserve"], "every reply fit the reserve"
        state = restore_child_state(call["log"], annotate=annotate_search_repetition)
        assert state.messages == call["messages"]
    log = provider.calls[-1]["log"]
    summaries = [r for r in log if r["type"] == "compacted" and r["summarized"]]
    assert len(summaries) >= 2 and all(r["reason"] == "threshold" for r in summaries)
    assert any(r["type"] == "step" and r["strip"] for r in log), "cheap cuts come first"
    # A later summary updates the earlier one instead of summarizing it again.
    assert any("-----BEGIN PREVIOUS RECORD-----" in c[1]["content"] for c in provider.summary_calls)
    assert sum(is_summary_message(m) for m in provider.calls[-1]["messages"]) == 1
    # The summary sees the work, never the brief.
    assert not any("## Your task" in c[1]["content"] for c in provider.summary_calls)
    assert all(CHILD_SUMMARY_INSTRUCTIONS in c[1]["content"] for c in provider.summary_calls)
    # Its tokens are the run's.
    assert record.usage["input"] == (reads + 1) * 100 + 40 * len(provider.summary_calls)


# ── Summary tokens ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summary_usage_is_collected_per_task():
    service = CompactionService(provider=_Provider(lambda *_: LLMResponse(content="x")), model=MODEL)
    history = [{"role": "user", "content": "check the logs"},
               {"role": "assistant", "content": _log_file(1, 6_000)},
               {"role": "user", "content": "and the next ones"},
               {"role": "assistant", "content": _log_file(2, 6_000)}]

    async def compact_in_own_task() -> list[dict[str, Any]]:
        with collect_summary_usage() as calls:
            await service.compact(history, track_cycle=False)
        return calls

    first, second = await asyncio.gather(compact_in_own_task(), compact_in_own_task())
    assert first and second and first == second
    assert all(u == {"prompt_tokens": 40, "completion_tokens": 9} for u in first)
    await service.compact(history, track_cycle=False)  # nothing collects outside a block


def test_a_provider_swap_moves_the_subagents_along():
    # The swap lives inside the gateway command; a new subagent must not keep
    # calling the old provider with the new model's id.
    import inspect

    from flowly.cli import gateway_cmd

    source = inspect.getsource(gateway_cmd)
    assert "subagents.provider = new_provider" in source
    assert "subagents.model = new_model" in source
