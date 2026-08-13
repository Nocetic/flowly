from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from flowly.goals.commands import GoalCommandHandler
from flowly.goals.manager import GoalManager
from flowly.goals.models import GoalContract, GoalStatus
from flowly.goals.store import GoalStore


class Judge:
    async def draft_contract(self, objective: str) -> GoalContract:
        return GoalContract(
            outcome=objective,
            verification="all tests pass",
            constraints="preserve public APIs",
        )


class Runtime:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_session(self, session_key: str) -> None:
        self.cancelled.append(session_key)


def _handler(tmp_path: Path) -> tuple[GoalManager, Runtime, GoalCommandHandler]:
    manager = GoalManager(GoalStore(tmp_path), Judge())  # type: ignore[arg-type]
    runtime = Runtime()
    handler = GoalCommandHandler(  # type: ignore[arg-type]
        manager,
        runtime,
        gate_timeout_seconds=45,
        gate_max_retries=2,
    )
    return manager, runtime, handler


@pytest.mark.asyncio
async def test_goal_set_parses_inline_contract_and_runs_this_turn(
    tmp_path: Path,
) -> None:
    manager, runtime, handler = _handler(tmp_path)

    result = await handler.goal(
        "web:chat",
        "Ship auth migration\nverify: auth tests pass\nconstraints: keep /login stable",
        conversation_epoch=7,
    )

    state = manager.get("web:chat")
    assert state is not None
    assert state.goal == "Ship auth migration"
    assert state.contract.verification == "auth tests pass"
    assert state.contract.constraints == "keep /login stable"
    assert state.conversation_epoch == 7
    # Setting a goal starts working: the caller runs THIS turn with the goal
    # text rather than replying with an acknowledgement.
    assert result.start_turn == state.goal
    assert result.kickoff_goal_id is None
    assert runtime.cancelled == ["web:chat"]
    assert result.metadata(user_epoch=3).get("_goal_kickoff_goal_id") is None


@pytest.mark.asyncio
async def test_plain_goal_starts_immediately_and_settles_in_the_background(
    tmp_path: Path,
) -> None:
    manager, _runtime, handler = _handler(tmp_path)

    result = await handler.goal(
        "web:chat",
        "Test the system end to end",
        conversation_epoch=0,
    )

    state = manager.get("web:chat")
    assert state is not None
    # The turn is not held back by the drafter…
    assert result.start_turn == "Test the system end to end"
    assert state.contract.is_empty
    assert result.content.startswith("⊙ Goal set")

    # …and the contract lands on the same goal once drafting finishes.
    await asyncio.gather(*handler._settling)
    settled = manager.get("web:chat")
    assert settled is not None
    assert settled.contract.outcome == "Test the system end to end"
    assert settled.contract.verification == "all tests pass"
    assert settled.goal_id == state.goal_id


@pytest.mark.asyncio
async def test_goal_draft_sets_structured_contract_and_kicks_off(tmp_path: Path) -> None:
    manager, _runtime, handler = _handler(tmp_path)

    result = await handler.goal(
        "web:chat",
        "draft Ship release",
        conversation_epoch=0,
    )

    state = manager.get("web:chat")
    assert state is not None
    assert state.contract.verification == "all tests pass"
    assert "Drafted completion contract" in result.content
    # An explicit draft is authoritative, so it is ready before the turn runs.
    assert result.start_turn == state.goal
    assert result.kickoff_goal_id is None


@pytest.mark.asyncio
async def test_goal_pause_resume_and_clear_are_explicit_lifecycle_controls(
    tmp_path: Path,
) -> None:
    manager, runtime, handler = _handler(tmp_path)
    manager.set("s", "ship")

    paused = await handler.goal("s", "pause", conversation_epoch=0)
    assert paused.state is not None and paused.state.status is GoalStatus.PAUSED
    assert runtime.cancelled == ["s"]

    resumed = await handler.goal("s", "resume", conversation_epoch=0)
    assert resumed.state is not None and resumed.state.status is GoalStatus.ACTIVE
    assert resumed.kickoff_goal_id is None

    cleared = await handler.goal("s", "done", conversation_epoch=9)
    assert cleared.state is not None and cleared.state.status is GoalStatus.CLEARED
    assert cleared.state.conversation_epoch == 9
    assert runtime.cancelled == ["s", "s"]


@pytest.mark.asyncio
async def test_goal_wait_unwait_and_gate_configuration(tmp_path: Path) -> None:
    manager, _runtime, handler = _handler(tmp_path)
    manager.set("s", "ship")

    waiting = await handler.goal("s", "wait 4242 CI", conversation_epoch=0)
    assert waiting.state is not None
    assert waiting.state.waiting_on_pid == 4242
    assert waiting.state.waiting_reason == "CI"

    unwaited = await handler.goal("s", "unwait", conversation_epoch=0)
    assert unwaited.wake_after_delivery is True
    assert unwaited.state is not None and not unwaited.state.has_wait

    gated = await handler.goal("s", "gate add pytest -q", conversation_epoch=0)
    assert gated.state is not None
    gate = gated.state.gates[0]
    assert gate.command == "pytest -q"
    assert gate.timeout_seconds == 45
    assert gate.max_retries == 2

    removed = await handler.goal("s", "gate rm 1", conversation_epoch=0)
    assert "Gate removed" in removed.content
    assert removed.state is not None and removed.state.gates == []


@pytest.mark.asyncio
async def test_subgoal_commands_update_extra_completion_criteria(tmp_path: Path) -> None:
    manager, _runtime, handler = _handler(tmp_path)
    manager.set("s", "ship")

    added = handler.subgoal("s", "Keep iOS compatible")
    assert "Added subgoal 1" in added.content
    assert added.state is not None
    assert added.state.subgoals == ["Keep iOS compatible"]

    shown = handler.subgoal("s", "")
    assert "Keep iOS compatible" in shown.content

    removed = handler.subgoal("s", "remove 1")
    assert "Removed subgoal 1" in removed.content
    assert removed.state is not None and removed.state.subgoals == []


@pytest.mark.asyncio
async def test_status_and_show_do_not_create_an_implicit_goal(tmp_path: Path) -> None:
    manager, runtime, handler = _handler(tmp_path)

    status = await handler.goal("s", "status", conversation_epoch=0)
    shown = await handler.goal("s", "show", conversation_epoch=0)

    assert status.content.startswith("No active goal")
    assert "(no active goal)" in shown.content
    assert manager.get("s") is None
    assert runtime.cancelled == []
