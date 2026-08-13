"""Lifecycle and post-turn orchestration for explicitly activated goals."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from flowly.goals.gates import GateRunner
from flowly.goals.judge import (
    GoalJudge,
    GoalJudgeParseError,
    GoalJudgeResult,
    GoalJudgeTransportError,
)
from flowly.goals.models import (
    DEFAULT_GATE_MAX_RETRIES,
    DEFAULT_GATE_TIMEOUT_SECONDS,
    DEFAULT_MAX_TURNS,
    MAX_GATES,
    MAX_SUBGOALS,
    GoalContract,
    GoalDecision,
    GoalGate,
    GoalState,
    GoalStatus,
    GoalVerdict,
    WaitKind,
)
from flowly.goals.store import GoalStore, GoalStoreConflictError

MAX_PARSE_FAILURES = 3
MAX_TRANSPORT_FAILURES = 5
MAX_COMPACTION_FAILURES = 2

GOAL_CONTINUATION_MARKER = "[Continuing toward the explicitly set standing goal]"


class _ContractSupersededError(RuntimeError):
    """The goal changed while its contract was being drafted."""


class GoalManager:
    def __init__(
        self,
        store: GoalStore,
        judge: GoalJudge,
        *,
        gate_runner: GateRunner | None = None,
        default_max_turns: int = DEFAULT_MAX_TURNS,
        pid_is_alive: Callable[[int], bool] | None = None,
        session_is_waiting: Callable[[str], bool] | None = None,
    ) -> None:
        self.store = store
        self.judge = judge
        self.gate_runner = gate_runner or GateRunner()
        self.default_max_turns = max(1, min(10_000, int(default_max_turns)))
        self._pid_is_alive = pid_is_alive or _pid_is_alive
        self._session_is_waiting = session_is_waiting or (lambda _session_id: False)

    def get(self, session_key: str) -> GoalState | None:
        return self.store.get(session_key)

    def set(
        self,
        session_key: str,
        goal: str,
        *,
        max_turns: int | None = None,
        contract: GoalContract | None = None,
        conversation_epoch: int = 0,
    ) -> GoalState:
        def replace(_current: GoalState | None) -> GoalState:
            return GoalState(
                session_key=session_key,
                goal=goal,
                max_turns=max_turns or self.default_max_turns,
                contract=contract or GoalContract(),
                conversation_epoch=conversation_epoch,
            )

        return self.store.update(session_key, replace)

    def pause(self, session_key: str, *, reason: str = "paused by user") -> GoalState:
        def mutate(state: GoalState | None) -> GoalState:
            state = _require_goal(state)
            if state.status is GoalStatus.CLEARED:
                raise RuntimeError("no goal to pause")
            state.status = GoalStatus.PAUSED
            state.paused_reason = reason
            state.clear_wait()
            return state

        return self.store.update(session_key, mutate)

    def resume(self, session_key: str) -> GoalState:
        def mutate(state: GoalState | None) -> GoalState:
            state = _require_goal(state)
            if state.status is GoalStatus.CLEARED:
                raise RuntimeError("no goal to resume")
            state.status = GoalStatus.ACTIVE
            state.turns_used = 0
            state.paused_reason = None
            state.consecutive_parse_failures = 0
            state.consecutive_transport_failures = 0
            state.consecutive_compaction_failures = 0
            state.clear_wait()
            return state

        return self.store.update(session_key, mutate)

    def resume_for_user_input(self, session_key: str) -> GoalState | None:
        """Resume a goal parked by the judge when the user replies.

        Unlike the explicit ``/goal resume`` command, this preserves the turn
        budget and failure counters.  The reply is part of the same goal run,
        not a fresh attempt.
        """
        snapshot = self.store.get(session_key)
        if (
            snapshot is None
            or snapshot.status is not GoalStatus.PAUSED
            or snapshot.last_verdict != GoalVerdict.NEEDS_INPUT.value
        ):
            return snapshot

        def mutate(state: GoalState) -> GoalState:
            if (
                state.status is not GoalStatus.PAUSED
                or state.last_verdict != GoalVerdict.NEEDS_INPUT.value
            ):
                raise GoalStoreConflictError("goal is no longer waiting for user input")
            state.status = GoalStatus.ACTIVE
            state.paused_reason = None
            return state

        try:
            return self.store.compare_and_update(snapshot, mutate)
        except GoalStoreConflictError:
            return self.store.get(session_key)

    def clear(self, session_key: str, *, conversation_epoch: int | None = None) -> GoalState | None:
        current = self.store.get(session_key)
        if current is None:
            return None

        def mutate(state: GoalState | None) -> GoalState:
            state = _require_goal(state)
            state.status = GoalStatus.CLEARED
            state.goal = ""
            state.subgoals.clear()
            state.gates.clear()
            state.contract = GoalContract()
            state.last_verdict = GoalVerdict.INACTIVE.value
            state.last_reason = "goal cleared"
            state.paused_reason = None
            state.consecutive_parse_failures = 0
            state.consecutive_transport_failures = 0
            state.consecutive_compaction_failures = 0
            state.clear_wait()
            if conversation_epoch is not None:
                state.conversation_epoch = max(0, int(conversation_epoch))
            return state

        return self.store.update(session_key, mutate)

    def is_generation_active(self, session_key: str, goal_id: str) -> bool:
        state = self.store.get(session_key)
        return bool(state and state.is_active and state.goal_id == goal_id)

    def continuation_prompt(self, state: GoalState) -> str:
        parts = [GOAL_CONTINUATION_MARKER, f"Goal: {state.goal}"]
        if not state.contract.is_empty:
            parts.append("Completion contract:\n" + state.contract.render())
        if state.subgoals:
            subgoals = "\n".join(
                f"{index}. {criterion}" for index, criterion in enumerate(state.subgoals, 1)
            )
            parts.append("Additional criteria:\n" + subgoals)
        parts.append(
            "Continue with the next concrete step. Stay within the constraints and "
            "verify completion with concrete evidence before claiming success. If "
            "progress requires user input, explain exactly what is needed without "
            "discarding the standing goal."
        )
        return "\n\n".join(parts)

    def attach_contract(
        self,
        session_key: str,
        goal_id: str,
        contract: GoalContract,
    ) -> GoalState | None:
        """Fill in a completion contract that was drafted after the goal began.

        Settling a plain objective costs an auxiliary model call, and making
        the user wait for it before the first turn is what kept ``/goal`` from
        behaving like an ordinary message. Drafting therefore runs alongside
        that turn and lands here — but only while the SAME goal generation is
        still live, and only when nothing has since supplied a contract of its
        own. A goal cleared, replaced or hand-written in the meantime wins.
        """
        if contract is None or contract.is_empty:
            return None

        def mutate(state: GoalState | None) -> GoalState:
            state = _require_live_goal(state)
            if state.goal_id != goal_id:
                raise _ContractSupersededError()
            if not state.contract.is_empty:
                raise _ContractSupersededError()
            state.contract = contract
            return state

        try:
            return self.store.update(session_key, mutate)
        except (_ContractSupersededError, RuntimeError, ValueError):
            # Superseded or no live goal: the drafted contract is stale and
            # must never resurrect a goal the user moved on from.
            return None

    def add_subgoal(self, session_key: str, criterion: str) -> GoalState:
        def mutate(state: GoalState | None) -> GoalState:
            state = _require_live_goal(state)
            if len(state.subgoals) >= MAX_SUBGOALS:
                raise ValueError(f"a goal may have at most {MAX_SUBGOALS} subgoals")
            probe = GoalState.from_dict(
                {**state.to_dict(), "subgoals": [*state.subgoals, criterion]}
            )
            state.subgoals = probe.subgoals
            return state

        return self.store.update(session_key, mutate)

    def remove_subgoal(self, session_key: str, index_1based: int) -> tuple[GoalState, str]:
        removed: list[str] = []

        def mutate(state: GoalState | None) -> GoalState:
            state = _require_live_goal(state)
            index = int(index_1based) - 1
            if index < 0 or index >= len(state.subgoals):
                raise IndexError(f"subgoal index out of range (1..{len(state.subgoals)})")
            removed.append(state.subgoals.pop(index))
            return state

        return self.store.update(session_key, mutate), removed[0]

    def clear_subgoals(self, session_key: str) -> tuple[GoalState, int]:
        count: list[int] = []

        def mutate(state: GoalState | None) -> GoalState:
            state = _require_live_goal(state)
            count.append(len(state.subgoals))
            state.subgoals.clear()
            return state

        return self.store.update(session_key, mutate), count[0]

    def add_gate(
        self,
        session_key: str,
        command: str,
        *,
        timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_GATE_MAX_RETRIES,
    ) -> GoalState:
        gate = GoalGate(command, timeout_seconds, max_retries)

        def mutate(state: GoalState | None) -> GoalState:
            state = _require_live_goal(state)
            if len(state.gates) >= MAX_GATES:
                raise ValueError(f"a goal may have at most {MAX_GATES} gates")
            state.gates.append(gate)
            return state

        return self.store.update(session_key, mutate)

    def remove_gate(self, session_key: str, index_1based: int) -> tuple[GoalState, str]:
        removed: list[str] = []

        def mutate(state: GoalState | None) -> GoalState:
            state = _require_live_goal(state)
            index = int(index_1based) - 1
            if index < 0 or index >= len(state.gates):
                raise IndexError(f"gate index out of range (1..{len(state.gates)})")
            removed.append(state.gates.pop(index).command)
            return state

        return self.store.update(session_key, mutate), removed[0]

    def clear_gates(self, session_key: str) -> tuple[GoalState, int]:
        count: list[int] = []

        def mutate(state: GoalState | None) -> GoalState:
            state = _require_live_goal(state)
            count.append(len(state.gates))
            state.gates.clear()
            return state

        return self.store.update(session_key, mutate), count[0]

    def wait_on_pid(self, session_key: str, pid: int, *, reason: str = "") -> GoalState:
        return self._set_wait(session_key, WaitKind.PID, pid, reason=reason)

    def wait_on_session(
        self, session_key: str, process_session: str, *, reason: str = ""
    ) -> GoalState:
        return self._set_wait(session_key, WaitKind.SESSION, process_session, reason=reason)

    def wait_for_seconds(self, session_key: str, seconds: int, *, reason: str = "") -> GoalState:
        seconds = int(seconds)
        if seconds <= 0 or seconds > 7 * 24 * 60 * 60:
            raise ValueError("wait duration is outside the supported range")
        return self._set_wait(session_key, WaitKind.TIME, time.time() + seconds, reason=reason)

    def _set_wait(
        self,
        session_key: str,
        kind: WaitKind,
        target: int | str | float,
        *,
        reason: str,
    ) -> GoalState:
        def mutate(state: GoalState | None) -> GoalState:
            state = _require_active_goal(state)
            state.set_wait(kind, target, reason=reason)
            return state

        return self.store.update(session_key, mutate)

    def stop_waiting(self, session_key: str) -> tuple[GoalState, bool]:
        changed: list[bool] = []

        def mutate(state: GoalState | None) -> GoalState:
            state = _require_live_goal(state)
            changed.append(state.has_wait)
            state.clear_wait()
            return state

        return self.store.update(session_key, mutate), changed[0]

    def waiting_state(self, session_key: str) -> GoalState | None:
        """Freshly resolve a barrier; clear it atomically once satisfied."""
        state = self.store.get(session_key)
        if state is None or not state.is_active or not state.has_wait:
            return None
        still_waiting = False
        if state.waiting_on_pid:
            still_waiting = self._pid_is_alive(state.waiting_on_pid)
        elif state.waiting_on_session:
            still_waiting = self._session_is_waiting(state.waiting_on_session)
        elif state.waiting_until:
            still_waiting = time.time() < state.waiting_until
        if still_waiting:
            return state
        try:
            return self.store.compare_and_update(state, _clear_wait)
        except GoalStoreConflictError:
            return self.store.get(session_key)

    async def evaluate_after_turn(
        self,
        session_key: str,
        latest_response: str,
        *,
        turn_succeeded: bool = True,
        aborted: bool = False,
        provider_error: bool = False,
        compaction_failed: bool = False,
        background_processes: Iterable[Mapping[str, Any]] = (),
        cwd: Path | None = None,
    ) -> GoalDecision:
        state = self.store.get(session_key)
        if state is None or not state.is_active:
            return _decision(state, GoalVerdict.INACTIVE, reason="no active goal")
        if aborted:
            try:
                paused = self.store.compare_and_update(
                    state,
                    lambda current: _pause(current, "turn interrupted"),
                )
            except GoalStoreConflictError:
                return _decision(
                    self.store.get(session_key), GoalVerdict.SKIPPED, reason="goal changed"
                )
            return _decision(
                paused,
                GoalVerdict.SKIPPED,
                reason="turn interrupted",
                message="⏸ Goal paused because the turn was interrupted.",
            )
        if compaction_failed:
            try:
                failed = self.store.compare_and_update(
                    state,
                    _record_compaction_failure,
                )
            except GoalStoreConflictError:
                return _decision(
                    self.store.get(session_key), GoalVerdict.SKIPPED, reason="goal changed"
                )
            if failed.status is GoalStatus.PAUSED:
                return _decision(
                    failed,
                    GoalVerdict.SKIPPED,
                    reason="context recovery failed twice",
                    message=(
                        "⏸ Goal paused after context recovery failed twice. "
                        "Conversation history was preserved; compact the chat or "
                        "choose a larger-context model, then resume the goal."
                    ),
                )
            return _decision(
                failed,
                GoalVerdict.SKIPPED,
                should_continue=True,
                continuation_prompt=self.continuation_prompt(failed),
                reason="context recovery failed; retrying once",
                message=(
                    "⚠ Goal context recovery failed. Retrying once without "
                    "resetting or truncating the conversation."
                ),
            )
        if (
            not turn_succeeded
            or provider_error
            or not str(latest_response or "").strip()
        ):
            return _decision(
                state, GoalVerdict.SKIPPED, reason="turn did not complete successfully"
            )

        waiting = self.waiting_state(session_key)
        if waiting is not None:
            state = waiting
            if waiting.has_wait:
                return _decision(
                    waiting,
                    GoalVerdict.WAITING,
                    reason=waiting.waiting_reason or "wait barrier is active",
                    message="⏳ Goal is parked until its wait condition is satisfied.",
                )

        try:
            state = self.store.compare_and_update(state, _consume_turn)
        except GoalStoreConflictError:
            return _decision(
                self.store.get(session_key), GoalVerdict.SKIPPED, reason="goal changed"
            )

        gate_decision = await self._evaluate_gates(state, cwd or Path.cwd())
        if gate_decision is not None:
            return gate_decision
        state = self.store.get(session_key)
        if state is None or not state.is_active:
            return _decision(state, GoalVerdict.SKIPPED, reason="goal changed")

        result: GoalJudgeResult | None = None
        parse_failed = False
        transport_failed = False
        try:
            result = await self.judge.evaluate(
                state,
                latest_response,
                background_processes=background_processes,
            )
        except GoalJudgeParseError as exc:
            parse_failed = True
            result = GoalJudgeResult(GoalVerdict.CONTINUE, str(exc))
        except GoalJudgeTransportError as exc:
            transport_failed = True
            result = GoalJudgeResult(GoalVerdict.CONTINUE, str(exc))

        try:
            committed = self.store.compare_and_update(
                state,
                lambda current: _apply_judge_result(
                    current,
                    result,
                    parse_failed=parse_failed,
                    transport_failed=transport_failed,
                ),
            )
        except GoalStoreConflictError:
            return _decision(
                self.store.get(session_key),
                GoalVerdict.SKIPPED,
                reason="goal changed during judging",
            )

        if committed.status is GoalStatus.DONE:
            return _decision(
                committed,
                GoalVerdict.DONE,
                reason=result.reason,
                message=f"✓ Goal achieved: {result.reason}",
            )
        if result.verdict is GoalVerdict.NEEDS_INPUT:
            return _decision(
                committed,
                GoalVerdict.NEEDS_INPUT,
                reason=result.reason,
                message=f"◌ Goal is waiting for your input: {result.reason}",
            )
        if committed.status is GoalStatus.PAUSED:
            return _decision(
                committed,
                GoalVerdict.CONTINUE,
                reason=result.reason,
                message=f"⏸ Goal paused: {committed.paused_reason}",
            )
        if result.verdict is GoalVerdict.WAIT:
            return _decision(
                committed,
                GoalVerdict.WAIT,
                reason=result.reason,
                message=f"⏳ Goal parked: {result.reason}",
            )
        return _decision(
            committed,
            GoalVerdict.CONTINUE,
            should_continue=True,
            continuation_prompt=self.continuation_prompt(committed),
            reason=result.reason,
            message=(
                f"↻ Continuing toward goal "
                f"({committed.turns_used}/{committed.max_turns}): {result.reason}"
            ),
        )

    async def _evaluate_gates(self, state: GoalState, cwd: Path) -> GoalDecision | None:
        for index in range(len(state.gates)):
            gate = state.gates[index]
            result = await self.gate_runner.run(gate, cwd=cwd)

            def apply(current: GoalState) -> GoalState:
                if index >= len(current.gates) or current.gates[index].command != gate.command:
                    raise GoalStoreConflictError("goal gate list changed")
                target = current.gates[index]
                target.last_exit_code = result.exit_code
                target.last_output_tail = result.output_tail
                if result.passed:
                    target.attempts = 0
                    target.last_failed_fingerprint = ""
                else:
                    target.attempts += 1
                    target.last_failed_fingerprint = result.fingerprint
                    if target.attempts > target.max_retries:
                        _pause(
                            current,
                            f"quality gate exhausted {target.max_retries} retries: $ {target.command}",
                        )
                return current

            try:
                state = self.store.compare_and_update(state, apply)
            except GoalStoreConflictError:
                return _decision(
                    self.store.get(state.session_key),
                    GoalVerdict.SKIPPED,
                    reason="goal changed during gate",
                )

            if result.passed:
                continue
            failed = state.gates[index]
            if state.status is GoalStatus.PAUSED:
                return _decision(
                    state,
                    GoalVerdict.GATE_FAILED,
                    reason=f"gate exhausted retries: $ {failed.command}",
                    message=f"⏸ Goal paused — quality gate is still failing: $ {failed.command}",
                )
            if state.turns_used >= state.max_turns:
                try:
                    state = self.store.compare_and_update(
                        state,
                        lambda current: _pause(
                            current,
                            f"turn budget exhausted ({current.turns_used}/{current.max_turns})",
                        ),
                    )
                except GoalStoreConflictError:
                    return _decision(
                        self.store.get(state.session_key),
                        GoalVerdict.SKIPPED,
                        reason="goal changed",
                    )
                return _decision(
                    state,
                    GoalVerdict.GATE_FAILED,
                    reason=f"gate failed: $ {failed.command}",
                    message="⏸ Goal paused — the turn budget ended while a quality gate was failing.",
                )
            prompt = _gate_continuation(state, failed, result.skipped_unchanged)
            return _decision(
                state,
                GoalVerdict.GATE_FAILED,
                should_continue=True,
                continuation_prompt=prompt,
                reason=f"gate failed (exit {result.exit_code}): $ {failed.command}",
                message=f"✗ Quality gate failed: $ {failed.command}",
            )
        return None


def _require_goal(state: GoalState | None) -> GoalState:
    if state is None:
        raise RuntimeError("no goal is set")
    return state


def _require_live_goal(state: GoalState | None) -> GoalState:
    state = _require_goal(state)
    if state.status is GoalStatus.CLEARED:
        raise RuntimeError("no goal is set")
    return state


def _require_active_goal(state: GoalState | None) -> GoalState:
    state = _require_live_goal(state)
    if not state.is_active:
        raise RuntimeError("goal is not active")
    return state


def _clear_wait(state: GoalState) -> GoalState:
    state.clear_wait()
    return state


def _pause(state: GoalState, reason: str) -> GoalState:
    state.status = GoalStatus.PAUSED
    state.paused_reason = reason
    state.clear_wait()
    return state


def _consume_turn(state: GoalState) -> GoalState:
    if not state.is_active:
        raise GoalStoreConflictError("goal is no longer active")
    state.turns_used += 1
    state.last_turn_at = time.time()
    state.consecutive_compaction_failures = 0
    return state


def _record_compaction_failure(state: GoalState) -> GoalState:
    if not state.is_active:
        raise GoalStoreConflictError("goal is no longer active")
    state.consecutive_compaction_failures += 1
    state.last_verdict = GoalVerdict.SKIPPED.value
    state.last_reason = "context recovery failed"
    if state.consecutive_compaction_failures >= MAX_COMPACTION_FAILURES:
        _pause(state, "context recovery failed twice; conversation history preserved")
    return state


def _apply_judge_result(
    state: GoalState,
    result: GoalJudgeResult,
    *,
    parse_failed: bool,
    transport_failed: bool,
) -> GoalState:
    if not state.is_active:
        raise GoalStoreConflictError("goal is no longer active")
    state.last_verdict = result.verdict.value
    state.last_reason = result.reason
    state.consecutive_parse_failures = state.consecutive_parse_failures + 1 if parse_failed else 0
    state.consecutive_transport_failures = (
        state.consecutive_transport_failures + 1 if transport_failed else 0
    )
    if result.verdict is GoalVerdict.DONE:
        state.status = GoalStatus.DONE
        state.clear_wait()
        return state
    if result.verdict is GoalVerdict.WAIT:
        if result.wait_kind is None or result.wait_target is None:
            raise GoalJudgeParseError("wait verdict has no target")
        if result.wait_kind is WaitKind.TIME:
            state.set_wait(
                WaitKind.TIME,
                time.time() + float(result.wait_target),
                reason=result.reason,
            )
        else:
            state.set_wait(result.wait_kind, result.wait_target, reason=result.reason)
        return state
    if result.verdict is GoalVerdict.NEEDS_INPUT:
        return _pause(state, f"waiting for user input: {result.reason}")
    if state.consecutive_transport_failures >= MAX_TRANSPORT_FAILURES:
        return _pause(state, "goal judge was unreachable for five consecutive turns")
    if state.consecutive_parse_failures >= MAX_PARSE_FAILURES:
        return _pause(state, "goal judge returned invalid output for three consecutive turns")
    if state.turns_used >= state.max_turns:
        return _pause(
            state,
            f"turn budget exhausted ({state.turns_used}/{state.max_turns})",
        )
    return state


def _decision(
    state: GoalState | None,
    verdict: GoalVerdict,
    *,
    should_continue: bool = False,
    continuation_prompt: str | None = None,
    reason: str = "",
    message: str = "",
) -> GoalDecision:
    return GoalDecision(
        status=state.status if state else None,
        verdict=verdict,
        should_continue=should_continue,
        continuation_prompt=continuation_prompt,
        reason=reason,
        message=message,
        goal_id=state.goal_id if state else None,
        revision=state.revision if state else None,
    )


def _gate_continuation(state: GoalState, gate: GoalGate, skipped: bool) -> str:
    skip_note = (
        "\nThe workspace was unchanged since the previous failure, so the command "
        "was not run again."
        if skipped
        else ""
    )
    return (
        f"{GOAL_CONTINUATION_MARKER}\n\n"
        f"Goal: {state.goal}\n\n"
        f"Quality gate failed (attempt {gate.attempts}/{gate.max_retries}):\n"
        f"$ {gate.command}\nExit code: {gate.last_exit_code}{skip_note}\n\n"
        f"Output tail:\n```\n{gate.last_output_tail or '(no output)'}\n```\n\n"
        "Fix the underlying issue and verify the gate. Do not claim completion "
        "while this gate fails."
    )


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True
