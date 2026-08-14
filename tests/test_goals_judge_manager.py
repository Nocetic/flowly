from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flowly.goals.gates import GateResult
from flowly.goals.judge import (
    GoalJudge,
    GoalJudgeParseError,
    GoalJudgeTransportError,
    parse_judge_result,
)
from flowly.goals.manager import GoalManager
from flowly.goals.models import (
    GoalContract,
    GoalStatus,
    GoalVerdict,
    WaitKind,
)
from flowly.goals.store import GoalStore
from flowly.providers.base import LLMResponse


class ScriptedProvider:
    def __init__(self, *responses: str | Exception):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return LLMResponse(content=value)


class FakeGateRunner:
    def __init__(self, *results: GateResult):
        self.results = list(results)
        self.calls = 0

    async def run(self, gate, *, cwd):
        self.calls += 1
        return self.results.pop(0)


def _manager(tmp_path: Path, provider: ScriptedProvider, **kwargs) -> GoalManager:
    return GoalManager(GoalStore(tmp_path), GoalJudge(provider), **kwargs)


@pytest.mark.parametrize(
    ("payload", "verdict"),
    [
        ('{"verdict":"done","reason":"verified"}', GoalVerdict.DONE),
        (
            'prefix ```json\n{"verdict":"continue","reason":"missing test"}\n```',
            GoalVerdict.CONTINUE,
        ),
        (
            '{"verdict":"needs_input","reason":"choose the deployment target"}',
            GoalVerdict.NEEDS_INPUT,
        ),
        ('{"done":true,"reason":"legacy"}', GoalVerdict.DONE),
    ],
)
def test_judge_parser_accepts_contract_and_legacy_shapes(
    payload: str, verdict: GoalVerdict
) -> None:
    assert parse_judge_result(payload).verdict is verdict


def test_judge_parser_requires_exactly_one_wait_target() -> None:
    result = parse_judge_result('{"verdict":"wait","wait_on_session":"p1","reason":"running"}')
    assert result.wait_kind is WaitKind.SESSION
    assert result.wait_target == "p1"

    with pytest.raises(GoalJudgeParseError, match="exactly one"):
        parse_judge_result('{"verdict":"wait","reason":"vague"}')
    with pytest.raises(GoalJudgeParseError, match="exactly one"):
        parse_judge_result('{"verdict":"wait","wait_on_pid":1,"wait_for_seconds":2,"reason":"two"}')


@pytest.mark.asyncio
async def test_judge_is_toolless_bounded_and_contains_contract_process_evidence(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider('{"verdict":"continue","reason":"work remains"}')
    manager = _manager(tmp_path, provider)
    state = manager.set(
        "web:1",
        "ship",
        contract=GoalContract(verification="all tests pass"),
    )
    state = manager.add_subgoal("web:1", "document behavior")

    result = await manager.judge.evaluate(
        state,
        "implemented part one",
        background_processes=[
            {
                "id": "proc-1",
                "pid": 7,
                "status": "running",
                "command": "pytest",
                "watch_patterns": ["passed"],
                "output_preview": "still running",
            }
        ],
    )

    assert result.verdict is GoalVerdict.CONTINUE
    call = provider.calls[0]
    assert call["tools"] is None
    assert call["temperature"] == 0.0
    assert call["timeout"] == 30.0
    prompt = call["messages"][1]["content"]
    assert "all tests pass" in prompt
    assert "document behavior" in prompt
    assert "proc-1" in prompt


@pytest.mark.asyncio
async def test_judge_distinguishes_transport_and_parse_failures(tmp_path: Path) -> None:
    transport = GoalJudge(ScriptedProvider(TimeoutError("slow")))
    with pytest.raises(GoalJudgeTransportError):
        await transport.evaluate(
            _manager(tmp_path, ScriptedProvider("unused")).set("s1", "g"), "response"
        )

    parser = GoalJudge(ScriptedProvider("not-json"))
    with pytest.raises(GoalJudgeParseError):
        await parser.evaluate(
            _manager(tmp_path / "other", ScriptedProvider("unused")).set("s2", "g"),
            "response",
        )


@pytest.mark.asyncio
async def test_continue_then_done_lifecycle_and_resume_reset(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        '{"verdict":"continue","reason":"tests remain"}',
        '{"verdict":"done","reason":"tests passed"}',
    )
    manager = _manager(tmp_path, provider)
    initial = manager.set("web:1", "ship", max_turns=4)

    first = await manager.evaluate_after_turn("web:1", "implemented")
    assert first.should_continue is True
    assert first.goal_id == initial.goal_id
    assert "tests remain" in first.message
    assert manager.get("web:1").turns_used == 1  # type: ignore[union-attr]

    second = await manager.evaluate_after_turn("web:1", "tests passed")
    assert second.verdict is GoalVerdict.DONE
    assert second.status is GoalStatus.DONE

    resumed = manager.resume("web:1")
    assert resumed.status is GoalStatus.ACTIVE
    assert resumed.turns_used == 0
    assert resumed.goal_id == initial.goal_id


@pytest.mark.asyncio
async def test_needs_input_parks_without_spinning_and_user_reply_resumes_budget(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        '{"verdict":"needs_input","reason":"choose staging or production"}'
    )
    manager = _manager(tmp_path, provider)
    initial = manager.set("web:1", "deploy safely", max_turns=4)

    decision = await manager.evaluate_after_turn(
        "web:1", "Which environment should I deploy to?"
    )

    parked = manager.get("web:1")
    assert decision.verdict is GoalVerdict.NEEDS_INPUT
    assert decision.should_continue is False
    assert parked is not None and parked.status is GoalStatus.PAUSED
    assert parked.turns_used == 1
    assert parked.goal_id == initial.goal_id

    resumed = manager.resume_for_user_input("web:1")
    assert resumed is not None and resumed.status is GoalStatus.ACTIVE
    assert resumed.turns_used == 1
    assert resumed.goal_id == initial.goal_id


@pytest.mark.asyncio
async def test_turn_budget_still_allows_done_on_last_turn(tmp_path: Path) -> None:
    done_manager = _manager(
        tmp_path / "done", ScriptedProvider('{"verdict":"done","reason":"verified"}')
    )
    done_manager.set("s", "g", max_turns=1)
    done = await done_manager.evaluate_after_turn("s", "evidence")
    assert done.status is GoalStatus.DONE

    continue_manager = _manager(
        tmp_path / "continue",
        ScriptedProvider('{"verdict":"continue","reason":"not done"}'),
    )
    continue_manager.set("s", "g", max_turns=1)
    continued = await continue_manager.evaluate_after_turn("s", "partial")
    assert continued.status is GoalStatus.PAUSED
    assert "turn budget exhausted" in continued.message


@pytest.mark.asyncio
async def test_wait_barrier_does_not_consume_a_turn_or_call_judge(tmp_path: Path) -> None:
    provider = ScriptedProvider('{"verdict":"continue","reason":"unused"}')
    manager = _manager(tmp_path, provider, pid_is_alive=lambda pid: pid == 77)
    manager.set("s", "g")
    manager.wait_on_pid("s", 77, reason="build running")

    decision = await manager.evaluate_after_turn("s", "waiting")

    assert decision.verdict is GoalVerdict.WAITING
    assert manager.get("s").turns_used == 0  # type: ignore[union-attr]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_satisfied_wait_is_cleared_and_same_evaluation_continues(tmp_path: Path) -> None:
    provider = ScriptedProvider('{"verdict":"continue","reason":"resume work"}')
    manager = _manager(tmp_path, provider, pid_is_alive=lambda _pid: False)
    manager.set("s", "g")
    manager.wait_on_pid("s", 77)

    decision = await manager.evaluate_after_turn("s", "process ended")

    assert decision.should_continue is True
    state = manager.get("s")
    assert state is not None and not state.has_wait and state.turns_used == 1


@pytest.mark.asyncio
async def test_aborted_failed_and_provider_error_turn_guards(tmp_path: Path) -> None:
    provider = ScriptedProvider('{"verdict":"continue","reason":"unused"}')
    manager = _manager(tmp_path, provider)
    manager.set("abort", "g")
    aborted = await manager.evaluate_after_turn("abort", "partial", aborted=True)
    assert aborted.status is GoalStatus.PAUSED
    assert manager.get("abort").turns_used == 0  # type: ignore[union-attr]

    for session, kwargs in (
        ("failed", {"turn_succeeded": False}),
        ("provider", {"provider_error": True}),
        ("compact", {"compaction_failed": True}),
        ("empty", {}),
    ):
        manager.set(session, "g")
        decision = await manager.evaluate_after_turn(
            session, "" if session == "empty" else "error text", **kwargs
        )
        assert decision.verdict is GoalVerdict.SKIPPED
        assert manager.get(session).turns_used == 0  # type: ignore[union-attr]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_compaction_exhaustion_retries_once_then_pauses_without_spending_turn(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider('{"verdict":"continue","reason":"unused"}')
    manager = _manager(tmp_path, provider)
    manager.set("s", "g")

    first = await manager.evaluate_after_turn(
        "s",
        "request too large",
        turn_succeeded=False,
        provider_error=True,
        compaction_failed=True,
    )
    after_first = manager.get("s")
    assert first.should_continue is True
    assert "Retrying once" in first.message
    assert after_first is not None and after_first.is_active
    assert after_first.turns_used == 0
    assert after_first.consecutive_compaction_failures == 1

    second = await manager.evaluate_after_turn(
        "s",
        "request too large",
        turn_succeeded=False,
        provider_error=True,
        compaction_failed=True,
    )
    after_second = manager.get("s")
    assert second.status is GoalStatus.PAUSED
    assert second.should_continue is False
    assert "history was preserved" in second.message
    assert after_second is not None and after_second.turns_used == 0
    assert after_second.consecutive_compaction_failures == 2
    assert provider.calls == []


@pytest.mark.asyncio
async def test_successful_goal_turn_resets_compaction_failure_streak(tmp_path: Path) -> None:
    provider = ScriptedProvider('{"verdict":"continue","reason":"progress"}')
    manager = _manager(tmp_path, provider)
    manager.set("s", "g")
    await manager.evaluate_after_turn(
        "s",
        "request too large",
        turn_succeeded=False,
        provider_error=True,
        compaction_failed=True,
    )

    decision = await manager.evaluate_after_turn("s", "progress evidence")

    state = manager.get("s")
    assert decision.should_continue is True
    assert state is not None and state.turns_used == 1
    assert state.consecutive_compaction_failures == 0


@pytest.mark.asyncio
async def test_parse_and_transport_failure_circuit_breakers(tmp_path: Path) -> None:
    parse_provider = ScriptedProvider("bad", "bad", "bad")
    parse_manager = _manager(tmp_path / "parse", parse_provider)
    parse_manager.set("s", "g", max_turns=20)
    for _ in range(3):
        decision = await parse_manager.evaluate_after_turn("s", "progress")
    assert decision.status is GoalStatus.PAUSED
    assert "invalid output" in decision.message

    transport_provider = ScriptedProvider(*(TimeoutError("x") for _ in range(5)))
    transport_manager = _manager(tmp_path / "transport", transport_provider)
    transport_manager.set("s", "g", max_turns=20)
    for _ in range(5):
        decision = await transport_manager.evaluate_after_turn("s", "progress")
    assert decision.status is GoalStatus.PAUSED
    assert "unreachable" in decision.message


@pytest.mark.asyncio
async def test_gate_failure_short_circuits_judge_and_pauses_after_retries(tmp_path: Path) -> None:
    provider = ScriptedProvider('{"verdict":"done","reason":"must not be called"}')
    failed = GateResult(False, 1, "two tests failed", "same", False)
    skipped = GateResult(False, 1, "two tests failed", "same", True)
    runner = FakeGateRunner(failed, skipped, skipped, skipped)
    manager = _manager(tmp_path, provider, gate_runner=runner)
    manager.set("s", "ship", max_turns=10)
    manager.add_gate("s", "pytest -q", max_retries=3)

    for attempt in range(1, 5):
        decision = await manager.evaluate_after_turn("s", "made changes")
        state = manager.get("s")
        assert state is not None and state.gates[0].attempts == attempt
        if attempt < 4:
            assert decision.should_continue is True
            assert "two tests failed" in decision.continuation_prompt
        else:
            assert decision.status is GoalStatus.PAUSED
    assert provider.calls == []


@pytest.mark.asyncio
async def test_gate_failure_and_budget_end_prioritizes_budget_pause(tmp_path: Path) -> None:
    provider = ScriptedProvider('{"verdict":"done","reason":"unused"}')
    runner = FakeGateRunner(GateResult(False, 1, "red", "fp", False))
    manager = _manager(tmp_path, provider, gate_runner=runner)
    manager.set("s", "ship", max_turns=1)
    manager.add_gate("s", "pytest", max_retries=3)

    decision = await manager.evaluate_after_turn("s", "work")

    assert decision.status is GoalStatus.PAUSED
    assert "turn budget" in decision.message
    assert provider.calls == []


def test_clear_invalidates_generation_and_removes_goal_payload(tmp_path: Path) -> None:
    manager = _manager(tmp_path, ScriptedProvider("unused"))
    active = manager.set("s", "secret objective")
    manager.add_subgoal("s", "criterion")
    manager.add_gate("s", "true")

    cleared = manager.clear("s", conversation_epoch=9)

    assert cleared is not None
    assert cleared.status is GoalStatus.CLEARED
    assert cleared.goal == ""
    assert cleared.subgoals == [] and cleared.gates == []
    assert cleared.conversation_epoch == 9
    assert not manager.is_generation_active("s", active.goal_id)


def test_a_slow_judge_warns_once_with_the_setting_to_change(caplog):
    """The verdict sits between every autonomous turn — say so, once."""
    import logging

    from flowly.goals.judge import GoalJudge, SLOW_JUDGE_SECONDS

    judge = GoalJudge.__new__(GoalJudge)
    judge.model = "big/slow-model"
    judge._slow_warning_sent = False

    with caplog.at_level(logging.WARNING):
        judge._note_latency(SLOW_JUDGE_SECONDS + 1)
        judge._note_latency(SLOW_JUDGE_SECONDS + 1)

    assert judge._slow_warning_sent is True


def test_a_fast_judge_stays_silent():
    from flowly.goals.judge import GoalJudge, SLOW_JUDGE_SECONDS

    judge = GoalJudge.__new__(GoalJudge)
    judge.model = None
    judge._slow_warning_sent = False

    judge._note_latency(SLOW_JUDGE_SECONDS - 0.1)

    assert judge._slow_warning_sent is False
