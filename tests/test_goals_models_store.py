from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from flowly.goals.models import (
    GoalContract,
    GoalGate,
    GoalState,
    GoalStatus,
    WaitKind,
    parse_contract,
)
from flowly.goals.store import (
    GoalStore,
    GoalStoreConflictError,
    GoalStoreCorruptError,
)


def _increment_turns(root: str, loops: int) -> None:
    store = GoalStore(Path(root))
    for _ in range(loops):
        store.update(
            "web:shared",
            lambda state: _increment_existing(state),
        )


def _increment_existing(state: GoalState | None) -> GoalState:
    assert state is not None
    state.turns_used += 1
    return state


def test_contract_alias_parser_and_round_trip() -> None:
    contract = parse_contract(
        """
        Goal: Ship the release
        Verified by: all tests pass
        Must not: change the public API
        Scope: flowly/goals
        Stop if blocked: ask for credentials
        """
    )

    assert contract == GoalContract(
        outcome="Ship the release",
        verification="all tests pass",
        constraints="change the public API",
        boundaries="flowly/goals",
        stop_when="ask for credentials",
    )
    assert GoalContract.from_dict(contract.to_dict()) == contract


def test_state_round_trip_preserves_all_behavioral_fields() -> None:
    state = GoalState(
        session_key="web:conversation",
        goal="Deliver a verified change",
        turns_used=4,
        max_turns=9,
        contract=GoalContract(outcome="ship", verification="tests pass"),
        subgoals=["No regressions"],
        gates=[GoalGate("pytest -q", timeout_seconds=12, max_retries=2)],
    )
    state.set_wait(WaitKind.SESSION, "proc-7", reason="tests are running", now=10)

    restored = GoalState.from_dict(json.loads(json.dumps(state.to_dict())))

    assert restored.to_dict() == state.to_dict()
    assert restored.wait_public_dict() == {
        "kind": "session",
        "sessionId": "proc-7",
        "reason": "tests are running",
    }
    assert restored.to_public_dict()["contract"]["verification"] == "tests pass"


def test_state_rejects_multiple_wait_targets() -> None:
    with pytest.raises(ValueError, match="multiple wait targets"):
        GoalState(
            session_key="s",
            goal="g",
            waiting_on_pid=42,
            waiting_until=9999999999,
        )


def test_store_save_is_atomic_and_compare_and_swap_rejects_stale_state(tmp_path: Path) -> None:
    store = GoalStore(tmp_path)
    first = store.save(GoalState(session_key="web:1", goal="one"))
    stale = store.get("web:1")
    assert stale is not None

    second = store.compare_and_update(first, lambda state: _rename(state, "two"))

    assert second.revision == first.revision + 1
    assert store.get("web:1").goal == "two"  # type: ignore[union-attr]
    with pytest.raises(GoalStoreConflictError, match="revision changed"):
        store.compare_and_update(stale, lambda state: _rename(state, "stale write"))


def _rename(state: GoalState, goal: str) -> GoalState:
    state.goal = goal
    return state


def test_replacing_goal_changes_generation_and_clear_leaves_a_tombstone(tmp_path: Path) -> None:
    store = GoalStore(tmp_path)
    first = store.save(GoalState(session_key="s", goal="first"))
    second = store.update("s", lambda _state: GoalState(session_key="s", goal="second"))
    assert first.goal_id != second.goal_id

    cleared = store.update("s", lambda state: _clear(state))
    assert cleared.status is GoalStatus.CLEARED
    assert cleared.goal == ""
    assert store.get("s").goal_id == second.goal_id  # type: ignore[union-attr]


def _clear(state: GoalState | None) -> GoalState:
    assert state is not None
    state.status = GoalStatus.CLEARED
    state.goal = ""
    return state


def test_corrupt_record_is_not_silently_overwritten(tmp_path: Path) -> None:
    store = GoalStore(tmp_path)
    state = store.save(GoalState(session_key="s", goal="g"))
    _lock, path = store._paths("s")
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(GoalStoreCorruptError):
        store.get("s")
    with pytest.raises(GoalStoreCorruptError):
        store.save(state)


@pytest.mark.skipif(
    multiprocessing.get_start_method(allow_none=True) == "spawn", reason="fork-only test"
)
def test_cross_process_updates_do_not_lose_writes(tmp_path: Path) -> None:
    store = GoalStore(tmp_path)
    store.save(GoalState(session_key="web:shared", goal="count", max_turns=1_000))
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_increment_turns, args=(str(tmp_path), 25)) for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    final = store.get("web:shared")
    assert final is not None
    assert final.turns_used == 100
    assert final.revision == 101
