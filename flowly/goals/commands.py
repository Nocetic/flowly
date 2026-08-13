"""Transport-neutral slash commands for explicit standing goals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flowly.goals.manager import GoalManager
from flowly.goals.models import GoalContract, GoalState, GoalStatus, parse_contract
from flowly.goals.runtime import GoalRuntime


@dataclass(frozen=True, slots=True)
class GoalCommandResult:
    content: str
    state: GoalState | None = None
    kickoff_goal_id: str | None = None
    wake_after_delivery: bool = False

    def metadata(self, *, user_epoch: int) -> dict[str, Any]:
        metadata: dict[str, Any] = {"_goal_user_epoch": user_epoch}
        if self.state is not None:
            metadata["goal"] = self.state.to_public_dict()
        if self.kickoff_goal_id:
            metadata["_goal_kickoff_goal_id"] = self.kickoff_goal_id
        if self.wake_after_delivery:
            metadata["_goal_wake_after_delivery"] = True
        return metadata


class GoalCommandHandler:
    def __init__(
        self,
        manager: GoalManager,
        runtime: GoalRuntime,
        *,
        gate_timeout_seconds: int = 300,
        gate_max_retries: int = 3,
    ) -> None:
        self.manager = manager
        self.runtime = runtime
        self.gate_timeout_seconds = max(1, min(3_600, int(gate_timeout_seconds)))
        self.gate_max_retries = max(0, min(100, int(gate_max_retries)))

    async def goal(
        self,
        session_key: str,
        arguments: str,
        *,
        conversation_epoch: int,
    ) -> GoalCommandResult:
        arguments = str(arguments or "").strip()
        lower = arguments.casefold()

        if not arguments or lower == "status":
            state = self.manager.get(session_key)
            return GoalCommandResult(_status_line(state), state)
        if lower == "show":
            state = self.manager.get(session_key)
            return GoalCommandResult(
                f"{_status_line(state)}\n{_render_contract(state)}",
                state,
            )
        if lower == "pause":
            try:
                state = self.manager.pause(session_key, reason="paused by user")
            except RuntimeError:
                return GoalCommandResult("No goal is set.")
            self.runtime.cancel_session(session_key)
            return GoalCommandResult(f"⏸ Goal paused: {state.goal}", state)
        if lower == "resume":
            try:
                state = self.manager.resume(session_key)
            except RuntimeError:
                return GoalCommandResult("No goal to resume.")
            return GoalCommandResult(f"▶ Goal resumed: {state.goal}", state)
        if lower in {"clear", "stop", "done"}:
            previous = self.manager.get(session_key)
            self.runtime.cancel_session(session_key)
            state = self.manager.clear(
                session_key,
                conversation_epoch=conversation_epoch,
            )
            if previous is None or previous.status is GoalStatus.CLEARED:
                return GoalCommandResult("No active goal.", state)
            return GoalCommandResult("✓ Goal cleared.", state)
        if lower == "wait" or lower.startswith("wait "):
            return self._wait(session_key, arguments)
        if lower == "unwait":
            try:
                state, changed = self.manager.stop_waiting(session_key)
            except RuntimeError:
                return GoalCommandResult("No active goal.")
            if not changed:
                return GoalCommandResult("No wait barrier set.", state)
            return GoalCommandResult(
                "▶ Wait barrier cleared — goal loop resumes.",
                state,
                wake_after_delivery=True,
            )
        if lower == "gate" or lower.startswith("gate "):
            return self._gate(session_key, arguments)

        drafted = lower == "draft" or lower.startswith("draft ")
        if drafted:
            objective = arguments[len("draft") :].strip()
            if not objective:
                return GoalCommandResult("Usage: /goal draft <objective in plain language>")
            try:
                contract = await self.manager.judge.draft_contract(objective)
            except Exception:
                contract = GoalContract()
            goal_text = objective
        else:
            goal_text, contract = _split_goal_contract(arguments)

        self.runtime.cancel_session(session_key)
        try:
            state = self.manager.set(
                session_key,
                goal_text,
                contract=contract,
                conversation_epoch=conversation_epoch,
            )
        except ValueError as exc:
            return GoalCommandResult(f"Invalid goal: {exc}")

        lines = [f"⊙ Goal set ({state.max_turns}-turn budget): {state.goal}"]
        if not state.contract.is_empty:
            label = "Drafted completion contract" if drafted else "Completion contract"
            lines.extend((f"{label}:", state.contract.render()))
        elif drafted:
            lines.append("Couldn't draft a completion contract; running as a free-form goal.")
        return GoalCommandResult(
            "\n".join(lines),
            state,
            kickoff_goal_id=state.goal_id,
        )

    def subgoal(self, session_key: str, arguments: str) -> GoalCommandResult:
        arguments = str(arguments or "").strip()
        state = self.manager.get(session_key)
        if state is None or state.status not in {GoalStatus.ACTIVE, GoalStatus.PAUSED}:
            return GoalCommandResult("No active goal. Set one with /goal <text>.", state)
        if not arguments:
            return GoalCommandResult(
                f"{_status_line(state)}\n{_render_subgoals(state)}",
                state,
            )

        verb, _, remainder = arguments.partition(" ")
        if verb.casefold() == "remove":
            if not remainder.strip():
                return GoalCommandResult("Usage: /subgoal remove <n>", state)
            try:
                index = int(remainder.strip().split()[0])
                state, removed = self.manager.remove_subgoal(session_key, index)
            except ValueError:
                return GoalCommandResult(
                    "/subgoal remove: <n> must be an integer (1-based index).",
                    state,
                )
            except (IndexError, RuntimeError) as exc:
                return GoalCommandResult(f"/subgoal remove: {exc}", state)
            return GoalCommandResult(f"✓ Removed subgoal {index}: {removed}", state)
        if verb.casefold() == "clear":
            try:
                state, count = self.manager.clear_subgoals(session_key)
            except RuntimeError as exc:
                return GoalCommandResult(f"/subgoal clear: {exc}", state)
            content = (
                f"✓ Cleared {count} subgoal{'s' if count != 1 else ''}."
                if count
                else "No subgoals to clear."
            )
            return GoalCommandResult(content, state)
        try:
            state = self.manager.add_subgoal(session_key, arguments)
        except (ValueError, RuntimeError) as exc:
            return GoalCommandResult(f"/subgoal: {exc}", state)
        return GoalCommandResult(
            f"✓ Added subgoal {len(state.subgoals)}: {state.subgoals[-1]}",
            state,
        )

    def _wait(self, session_key: str, arguments: str) -> GoalCommandResult:
        wait_arguments = arguments[len("wait") :].strip()
        state = self.manager.get(session_key)
        if not wait_arguments:
            return GoalCommandResult("Usage: /goal wait <pid> [reason]", state)
        pid_text, _, reason = wait_arguments.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            return GoalCommandResult("/goal wait: <pid> must be an integer process id.", state)
        try:
            state = self.manager.wait_on_pid(session_key, pid, reason=reason.strip())
        except (RuntimeError, ValueError) as exc:
            return GoalCommandResult(f"/goal wait: {exc}", state)
        suffix = f" ({reason.strip()})" if reason.strip() else ""
        return GoalCommandResult(
            f"⏳ Goal parked on pid {pid}{suffix}. Loop pauses until it exits.",
            state,
        )

    def _gate(self, session_key: str, arguments: str) -> GoalCommandResult:
        gate_arguments = arguments[len("gate") :].strip()
        gate_lower = gate_arguments.casefold()
        state = self.manager.get(session_key)
        if not gate_arguments or gate_lower == "list":
            return GoalCommandResult(_render_gates(state), state)
        if gate_lower.startswith("add "):
            command = gate_arguments[len("add") :].strip()
            try:
                state = self.manager.add_gate(
                    session_key,
                    command,
                    timeout_seconds=self.gate_timeout_seconds,
                    max_retries=self.gate_max_retries,
                )
            except (RuntimeError, ValueError) as exc:
                return GoalCommandResult(f"/goal gate add: {exc}", state)
            gate = state.gates[-1]
            return GoalCommandResult(
                f"⚿ Gate added: $ {gate.command} "
                f"({gate.max_retries} retries, {gate.timeout_seconds}s timeout). "
                "It must pass before the goal can complete.",
                state,
            )
        if gate_lower.startswith("remove ") or gate_lower.startswith("rm "):
            _, _, index_text = gate_arguments.partition(" ")
            try:
                state, removed = self.manager.remove_gate(session_key, int(index_text.strip()))
            except (RuntimeError, ValueError, IndexError) as exc:
                return GoalCommandResult(f"/goal gate remove: {exc}", state)
            return GoalCommandResult(f"✓ Gate removed: $ {removed}", state)
        if gate_lower == "clear":
            try:
                state, count = self.manager.clear_gates(session_key)
            except RuntimeError as exc:
                return GoalCommandResult(f"/goal gate clear: {exc}", state)
            return GoalCommandResult(
                f"✓ Cleared {count} gate{'s' if count != 1 else ''}.",
                state,
            )
        return GoalCommandResult(
            "Usage: /goal gate [list | add <command> | remove <N> | clear]",
            state,
        )


def _split_goal_contract(text: str) -> tuple[str, GoalContract]:
    """Extract recognized inline contract fields without mangling prose colons."""
    contract = parse_contract(text)
    aliases = {
        "outcome",
        "goal",
        "done",
        "done when",
        "verification",
        "verify",
        "verified by",
        "evidence",
        "proof",
        "constraints",
        "constraint",
        "preserve",
        "must not",
        "do not change",
        "boundaries",
        "boundary",
        "scope",
        "allowed",
        "files",
        "stop when",
        "stop_when",
        "blocked",
        "stop if blocked",
        "give up when",
    }
    headline: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        prefix, separator, value = line.partition(":")
        if separator and prefix.strip().casefold() in aliases and value.strip():
            continue
        if line:
            headline.append(line)
    goal = " ".join(headline).strip() or str(text or "").strip()
    return goal, contract


def _status_line(state: GoalState | None) -> str:
    if state is None or state.status is GoalStatus.CLEARED:
        return "No active goal. Set one with /goal <text>."
    details = [f"{state.turns_used}/{state.max_turns} turns"]
    if state.subgoals:
        details.append(f"{len(state.subgoals)} subgoal{'s' if len(state.subgoals) != 1 else ''}")
    if not state.contract.is_empty:
        details.append("contract")
    if state.gates:
        details.append(f"{len(state.gates)} gate{'s' if len(state.gates) != 1 else ''}")
    detail_text = ", ".join(details)
    if state.status is GoalStatus.ACTIVE and state.has_wait:
        reason = state.waiting_reason or "its wait condition"
        return f"⏳ Goal (parked on {reason}, {detail_text}): {state.goal}"
    if state.status is GoalStatus.ACTIVE:
        return f"⊙ Goal (active, {detail_text}): {state.goal}"
    if state.status is GoalStatus.PAUSED:
        reason = f" — {state.paused_reason}" if state.paused_reason else ""
        return f"⏸ Goal (paused, {detail_text}{reason}): {state.goal}"
    if state.status is GoalStatus.DONE:
        return f"✓ Goal done ({detail_text}): {state.goal}"
    return f"Goal ({state.status.value}, {detail_text}): {state.goal}"


def _render_contract(state: GoalState | None) -> str:
    if state is None or state.status is GoalStatus.CLEARED:
        return "(no active goal)"
    if state.contract.is_empty:
        return (
            "(no completion contract — set one with /goal draft <objective> "
            "or inline field: value lines)"
        )
    return state.contract.render()


def _render_subgoals(state: GoalState) -> str:
    if not state.subgoals:
        return "(no subgoals — use /subgoal <text> to add criteria)"
    return "\n".join(f"- {index}. {criterion}" for index, criterion in enumerate(state.subgoals, 1))


def _render_gates(state: GoalState | None) -> str:
    if state is None or state.status is GoalStatus.CLEARED:
        return "(no active goal)"
    if not state.gates:
        return "(no quality gates — use /goal gate add <command> to require one)"
    lines: list[str] = []
    for index, gate in enumerate(state.gates, 1):
        status = ""
        if gate.last_exit_code is not None:
            status = (
                " ✓ passing"
                if gate.last_exit_code == 0
                else f" ✗ failing (exit {gate.last_exit_code}, "
                f"attempt {gate.attempts}/{gate.max_retries})"
            )
        lines.append(f"- {index}. $ {gate.command}{status}")
    return "\n".join(lines)
