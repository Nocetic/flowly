from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from flowly.goals.judge import GoalJudge
from flowly.goals.manager import GoalManager
from flowly.goals.runtime import DeliveredGoalTurn, GoalRuntime
from flowly.goals.store import GoalStore
from flowly.providers.base import LLMResponse


class Provider:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def chat(self, _messages, **_kwargs):
        self.started.set()
        if self.block:
            await self.release.wait()
        return LLMResponse(content=self.responses.pop(0))


class Delivery:
    def __init__(self, *continuation_responses: str):
        self.continuation_responses = list(continuation_responses)
        self.events: list[tuple[str, Any]] = []
        self.finished = asyncio.Event()

    async def run_continuation(
        self,
        *,
        session_key: str,
        goal_id: str,
        user_epoch: int,
        kickoff: bool,
    ) -> DeliveredGoalTurn | None:
        self.events.append(("run", {"goal_id": goal_id, "kickoff": kickoff}))
        return DeliveredGoalTurn(
            session_key=session_key,
            response=self.continuation_responses.pop(0),
            user_epoch=user_epoch,
        )

    async def deliver_turn(self, turn: DeliveredGoalTurn) -> None:
        self.events.append(("assistant", turn.response))

    async def deliver_notice(self, decision) -> None:
        self.events.append(("notice", decision.verdict.value))
        if decision.status and decision.status.value in {"done", "paused"}:
            self.finished.set()


def _runtime(
    tmp_path: Path,
    provider: Provider,
    epochs: dict[str, int],
    **kwargs,
) -> tuple[GoalManager, GoalRuntime]:
    manager = GoalManager(
        GoalStore(tmp_path),
        GoalJudge(provider),
        session_is_waiting=kwargs.pop("session_is_waiting", None),
    )
    runtime = GoalRuntime(
        manager,
        current_user_epoch=lambda session: epochs.get(session, 0),
        **kwargs,
    )
    return manager, runtime


@pytest.mark.asyncio
async def test_delivered_reply_is_judged_before_continuation_and_every_turn_is_ordered(
    tmp_path: Path,
) -> None:
    provider = Provider(
        '{"verdict":"continue","reason":"more"}',
        '{"verdict":"done","reason":"verified"}',
    )
    epochs = {"s": 1}
    manager, runtime = _runtime(tmp_path, provider, epochs)
    state = manager.set("s", "ship")
    delivery = Delivery("second assistant reply")

    # The original assistant reply has already been delivered by the caller.
    delivery.events.append(("assistant", "first assistant reply"))
    runtime.delivered(
        DeliveredGoalTurn("s", "first assistant reply", user_epoch=1),
        delivery,
    )
    await asyncio.wait_for(delivery.finished.wait(), timeout=1)

    assert delivery.events == [
        ("assistant", "first assistant reply"),
        ("notice", "continue"),
        ("run", {"goal_id": state.goal_id, "kickoff": False}),
        ("assistant", "second assistant reply"),
        ("notice", "done"),
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_real_user_arrival_preempts_a_queued_synthetic_turn(tmp_path: Path) -> None:
    provider = Provider('{"verdict":"continue","reason":"more"}')
    provider.block = True
    epochs = {"s": 4}
    manager, runtime = _runtime(tmp_path, provider, epochs)
    manager.set("s", "ship")
    delivery = Delivery("must not run")

    runtime.delivered(DeliveredGoalTurn("s", "first", user_epoch=4), delivery)
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    epochs["s"] = 5
    provider.release.set()
    await asyncio.sleep(0.05)

    assert delivery.events == [("notice", "continue")]
    await runtime.close()


@pytest.mark.asyncio
async def test_pause_during_judging_invalidates_the_result(tmp_path: Path) -> None:
    provider = Provider('{"verdict":"continue","reason":"more"}')
    provider.block = True
    epochs = {"s": 1}
    manager, runtime = _runtime(tmp_path, provider, epochs)
    manager.set("s", "ship")
    delivery = Delivery("must not run")

    runtime.delivered(DeliveredGoalTurn("s", "first", user_epoch=1), delivery)
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    manager.pause("s")
    provider.release.set()
    await asyncio.sleep(0.05)

    assert delivery.events == []
    assert manager.get("s").status.value == "paused"  # type: ignore[union-attr]
    await runtime.close()


@pytest.mark.asyncio
async def test_kickoff_runs_goal_text_as_first_turn_without_a_prior_judge(tmp_path: Path) -> None:
    provider = Provider('{"verdict":"done","reason":"complete"}')
    epochs = {"s": 2}
    manager, runtime = _runtime(tmp_path, provider, epochs)
    state = manager.set("s", "ship")
    delivery = Delivery("kickoff assistant")

    runtime.kickoff("s", state.goal_id, 2, delivery)
    await asyncio.wait_for(delivery.finished.wait(), timeout=1)

    assert delivery.events[0] == (
        "run",
        {"goal_id": state.goal_id, "kickoff": True},
    )
    assert delivery.events[1:] == [
        ("assistant", "kickoff assistant"),
        ("notice", "done"),
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_pending_plan_parks_without_consuming_turn_and_wakes_on_resolution(
    tmp_path: Path,
) -> None:
    provider = Provider('{"verdict":"done","reason":"complete"}')
    epochs = {"s": 1}
    plan = {"pending": True}
    manager, runtime = _runtime(
        tmp_path,
        provider,
        epochs,
        pending_plan=lambda _session: "plan-1" if plan["pending"] else None,
        session_is_waiting=lambda target: target == "plan:plan-1" and plan["pending"],
    )
    manager.set("s", "ship")
    delivery = Delivery("after approval")

    runtime.delivered(DeliveredGoalTurn("s", "plan proposed", user_epoch=1), delivery)
    await asyncio.sleep(0.05)
    state = manager.get("s")
    assert state is not None and state.turns_used == 0
    assert state.waiting_on_session == "plan:plan-1"
    assert delivery.events == [("notice", "waiting")]

    plan["pending"] = False
    assert runtime.wake("s") is True
    await asyncio.wait_for(delivery.finished.wait(), timeout=1)
    assert delivery.events[-3:] == [
        ("run", {"goal_id": state.goal_id, "kickoff": False}),
        ("assistant", "after approval"),
        ("notice", "done"),
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_parked_goal_polls_and_wakes_without_an_external_event(
    tmp_path: Path,
) -> None:
    provider = Provider('{"verdict":"done","reason":"complete"}')
    epochs = {"s": 1}
    barrier = {"active": True}
    manager, runtime = _runtime(
        tmp_path,
        provider,
        epochs,
        session_is_waiting=lambda target: target == "process-1" and barrier["active"],
    )
    state = manager.set("s", "ship")
    manager.wait_on_session("s", "process-1", reason="background work")
    delivery = Delivery("after process")

    runtime.delivered(DeliveredGoalTurn("s", "started process", user_epoch=1), delivery)
    await asyncio.sleep(0.05)
    assert delivery.events == [("notice", "waiting")]

    barrier["active"] = False
    await asyncio.wait_for(delivery.finished.wait(), timeout=2)
    assert delivery.events[-3:] == [
        ("run", {"goal_id": state.goal_id, "kickoff": False}),
        ("assistant", "after process"),
        ("notice", "done"),
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_cancel_session_drops_route_timer_and_queued_work(tmp_path: Path) -> None:
    provider = Provider('{"verdict":"done","reason":"complete"}')
    epochs = {"s": 1}
    manager, runtime = _runtime(tmp_path, provider, epochs)
    manager.set("s", "ship")
    manager.wait_for_seconds("s", 30)
    delivery = Delivery("must not run")

    runtime.delivered(DeliveredGoalTurn("s", "park", user_epoch=1), delivery)
    await asyncio.sleep(0.05)
    runtime.cancel_session("s")
    manager.clear("s")
    await asyncio.sleep(0)

    assert runtime.wake("s") is False
    assert delivery.events == [("notice", "waiting")]
    await runtime.close()
