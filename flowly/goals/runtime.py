"""Delivery-ordered runtime for autonomous goal continuations.

The main reply must reach the user before auxiliary judging begins.  This
runtime therefore accepts only *delivered* turns, serializes evaluation per
session, and asks a transport adapter to run/deliver any synthetic follow-up.
Real-user arrival is represented by a monotonically increasing epoch; a
continuation is discarded whenever that epoch changes before execution.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol

from loguru import logger

from flowly.goals.manager import GoalManager
from flowly.goals.models import GoalDecision, GoalStatus, GoalVerdict


@dataclass(slots=True)
class DeliveredGoalTurn:
    session_key: str
    response: str
    user_epoch: int
    succeeded: bool = True
    aborted: bool = False
    provider_error: bool = False
    compaction_failed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class GoalDelivery(Protocol):
    """One conversation surface's continuation and delivery operations."""

    async def run_continuation(
        self,
        *,
        session_key: str,
        goal_id: str,
        user_epoch: int,
        kickoff: bool,
    ) -> DeliveredGoalTurn | None: ...

    async def deliver_turn(self, turn: DeliveredGoalTurn) -> None: ...

    async def deliver_notice(self, decision: GoalDecision) -> None: ...


@dataclass(frozen=True, slots=True)
class _DeliveredWork:
    turn: DeliveredGoalTurn
    delivery: GoalDelivery


@dataclass(frozen=True, slots=True)
class _KickoffWork:
    session_key: str
    goal_id: str
    user_epoch: int
    delivery: GoalDelivery


@dataclass(frozen=True, slots=True)
class _WakeWork:
    session_key: str
    delivery: GoalDelivery


_Work = _DeliveredWork | _KickoffWork | _WakeWork


class GoalRuntime:
    """Session-FIFO evaluator with low-priority synthetic continuations."""

    def __init__(
        self,
        manager: GoalManager,
        *,
        current_user_epoch: Callable[[str], int],
        background_processes: Callable[[str], Awaitable[Iterable[Mapping[str, Any]]]] | None = None,
        session_cwd: Callable[[str], Path] | None = None,
        pending_plan: Callable[[str], str | None] | None = None,
    ) -> None:
        self.manager = manager
        self._current_user_epoch = current_user_epoch
        self._background_processes = background_processes or _empty_processes
        self._session_cwd = session_cwd or (lambda _session_key: Path.cwd())
        self._pending_plan = pending_plan or (lambda _session_key: None)
        self._queues: dict[str, asyncio.Queue[_Work]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._routes: dict[str, GoalDelivery] = {}
        self._wake_timers: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    def delivered(self, turn: DeliveredGoalTurn, delivery: GoalDelivery) -> None:
        """Queue a turn after its assistant response was delivered."""
        if self._closed:
            return
        self._routes[turn.session_key] = delivery
        self._enqueue(turn.session_key, _DeliveredWork(turn, delivery))

    def kickoff(
        self,
        session_key: str,
        goal_id: str,
        user_epoch: int,
        delivery: GoalDelivery,
    ) -> None:
        """Run a newly set goal's text as its immediate first agent turn."""
        if self._closed:
            return
        self._routes[session_key] = delivery
        self._enqueue(
            session_key,
            _KickoffWork(session_key, goal_id, user_epoch, delivery),
        )

    def wake(self, session_key: str, delivery: GoalDelivery | None = None) -> bool:
        """Recheck a parked goal after a process, plan or timer changed."""
        if self._closed:
            return False
        route = delivery or self._routes.get(session_key)
        if route is None:
            return False
        self._enqueue(session_key, _WakeWork(session_key, route))
        return True

    def cancel_wait_timer(self, session_key: str) -> None:
        timer = self._wake_timers.pop(session_key, None)
        if timer and not timer.done():
            timer.cancel()

    def cancel_session(self, session_key: str) -> None:
        """Cancel queued work and wake polling for one conversation."""
        self.cancel_wait_timer(session_key)
        worker = self._workers.pop(session_key, None)
        if worker and not worker.done():
            worker.cancel()
        self._queues.pop(session_key, None)
        self._routes.pop(session_key, None)

    async def wait_idle(self, session_key: str) -> None:
        """Wait until all currently queued work for a session is settled."""
        queue = self._queues.get(session_key)
        if queue is not None:
            await queue.join()

    def cancel(self) -> None:
        """Synchronously stop all runtime-owned workers and timers."""
        self._closed = True
        tasks = [*self._workers.values(), *self._wake_timers.values()]
        self._workers.clear()
        self._wake_timers.clear()
        self._queues.clear()
        self._routes.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

    async def close(self) -> None:
        tasks = [*self._workers.values(), *self._wake_timers.values()]
        self.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _enqueue(self, session_key: str, work: _Work) -> None:
        queue = self._queues.setdefault(session_key, asyncio.Queue())
        queue.put_nowait(work)
        worker = self._workers.get(session_key)
        if worker is None or worker.done():
            worker = asyncio.create_task(
                self._worker(session_key, queue),
                name=f"goal-runtime:{session_key}",
            )
            self._workers[session_key] = worker

    async def _worker(self, session_key: str, queue: asyncio.Queue[_Work]) -> None:
        current = asyncio.current_task()
        try:
            while not self._closed:
                try:
                    work = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if queue.empty():
                        return
                    continue
                try:
                    if isinstance(work, _DeliveredWork):
                        await self._after_turn(work.turn, work.delivery)
                    elif isinstance(work, _KickoffWork):
                        await self._run_next(
                            work.session_key,
                            work.goal_id,
                            work.user_epoch,
                            work.delivery,
                            kickoff=True,
                        )
                    else:
                        await self._resume_wait(work.session_key, work.delivery)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("goal runtime work failed for {}", session_key)
                finally:
                    queue.task_done()
        finally:
            if self._workers.get(session_key) is current:
                self._workers.pop(session_key, None)
            if queue.empty():
                if self._queues.get(session_key) is queue:
                    self._queues.pop(session_key, None)
            elif not self._closed and session_key not in self._workers:
                # A wake can land at the same instant as the idle timeout.
                # Removing the retiring worker before this check closes the
                # otherwise-stranded-queue race without keeping one task alive
                # forever for every historical conversation.
                replacement = asyncio.create_task(
                    self._worker(session_key, queue),
                    name=f"goal-runtime:{session_key}",
                )
                self._workers[session_key] = replacement

    async def _after_turn(
        self,
        turn: DeliveredGoalTurn,
        delivery: GoalDelivery,
    ) -> None:
        state = self.manager.get(turn.session_key)
        if state is None or not state.is_active:
            return

        plan_id = self._pending_plan(turn.session_key)
        if plan_id and not state.has_wait:
            try:
                state = self.manager.wait_on_session(
                    turn.session_key,
                    f"plan:{plan_id}",
                    reason="waiting for plan approval",
                )
            except RuntimeError:
                return
            await delivery.deliver_notice(
                GoalDecision(
                    status=state.status,
                    verdict=GoalVerdict.WAITING,
                    reason="waiting for plan approval",
                    message="⏳ Goal parked — waiting for plan approval.",
                    goal_id=state.goal_id,
                    revision=state.revision,
                )
            )
            self._schedule_wait_wake(turn.session_key, delivery)
            return

        try:
            processes = list(await self._background_processes(turn.session_key))
        except Exception:
            logger.exception("goal process snapshot failed for {}", turn.session_key)
            processes = []
        decision = await self.manager.evaluate_after_turn(
            turn.session_key,
            turn.response,
            turn_succeeded=turn.succeeded,
            aborted=turn.aborted,
            provider_error=turn.provider_error,
            compaction_failed=turn.compaction_failed,
            background_processes=processes,
            cwd=self._session_cwd(turn.session_key),
        )
        if decision.verdict not in {GoalVerdict.INACTIVE, GoalVerdict.SKIPPED} or decision.message:
            await delivery.deliver_notice(decision)
        if decision.status is GoalStatus.PAUSED or decision.status is GoalStatus.DONE:
            self.cancel_wait_timer(turn.session_key)
            return
        if decision.verdict in {GoalVerdict.WAIT, GoalVerdict.WAITING}:
            self._schedule_wait_wake(turn.session_key, delivery)
            return
        if not decision.should_continue or not decision.goal_id:
            return
        await self._run_next(
            turn.session_key,
            decision.goal_id,
            turn.user_epoch,
            delivery,
            kickoff=False,
        )

    async def _run_next(
        self,
        session_key: str,
        goal_id: str,
        user_epoch: int,
        delivery: GoalDelivery,
        *,
        kickoff: bool,
    ) -> None:
        # Yield once so real inbound work already made runnable can publish its
        # arrival epoch before this low-priority synthetic turn starts.
        await asyncio.sleep(0)
        if self._current_user_epoch(session_key) != user_epoch:
            return
        if not self.manager.is_generation_active(session_key, goal_id):
            return
        turn = await delivery.run_continuation(
            session_key=session_key,
            goal_id=goal_id,
            user_epoch=user_epoch,
            kickoff=kickoff,
        )
        if turn is None:
            return
        await delivery.deliver_turn(turn)
        await self._after_turn(turn, delivery)

    async def _resume_wait(self, session_key: str, delivery: GoalDelivery) -> None:
        state = self.manager.waiting_state(session_key)
        if state is None:
            # A polling timer may have atomically cleared the barrier before it
            # enqueued this wake item. Reload the live goal instead of treating
            # that already-resolved wait as an inactive goal.
            state = self.manager.get(session_key)
        if state is None or not state.is_active:
            return
        if state.has_wait:
            # Still parked. Re-arm the poller rather than returning: a wake
            # that arrives without one — the restart sweep is the normal case —
            # would otherwise leave the barrier with nothing watching it, and
            # the goal would wait for a message the user should not have to
            # send. Idempotent: scheduling replaces any existing timer.
            self._schedule_wait_wake(session_key, delivery)
            return
        self.cancel_wait_timer(session_key)
        await self._run_next(
            session_key,
            state.goal_id,
            self._current_user_epoch(session_key),
            delivery,
            kickoff=False,
        )

    def _schedule_wait_wake(self, session_key: str, delivery: GoalDelivery) -> None:
        state = self.manager.get(session_key)
        if state is None or not state.is_active or not state.has_wait:
            return
        self.cancel_wait_timer(session_key)

        async def wake_later() -> None:
            try:
                while not self._closed:
                    fresh = self.manager.waiting_state(session_key)
                    if fresh is None or not fresh.is_active:
                        return
                    if not fresh.has_wait:
                        self.wake(session_key, delivery)
                        return
                    delay = 1.0
                    if fresh.waiting_until:
                        # ``waiting_until`` is wall-clock time, whereas sleeps
                        # are duration based. Recompute on every pass so clock
                        # adjustments cannot strand the goal.
                        import time

                        delay = min(1.0, max(0.0, fresh.waiting_until - time.time()))
                    await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("goal wait polling failed for {}", session_key)
            finally:
                if self._wake_timers.get(session_key) is asyncio.current_task():
                    self._wake_timers.pop(session_key, None)

        self._wake_timers[session_key] = asyncio.create_task(
            wake_later(), name=f"goal-wake:{session_key}"
        )


async def _empty_processes(_session_key: str) -> Iterable[Mapping[str, Any]]:
    return ()


@contextlib.asynccontextmanager
async def goal_runtime_context(runtime: GoalRuntime):
    """Small integration/test helper that guarantees background cleanup."""
    try:
        yield runtime
    finally:
        await runtime.close()
