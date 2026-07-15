"""Approval coordination for plan mode.

Structurally a sibling of :class:`flowly.clarify.manager.ClarifyManager` and
``flowly.exec.approval_manager`` — an ``asyncio.Future`` the proposing agent
turn awaits while any surface answers. Three deliberate differences from
clarify (PLAN_MODE_PLAN.md §2, §6):

- **Three decisions, not free text:** ``approve`` / ``reject`` / ``revise``.
  "No" and "change this step" never blur together.
- **Timeout = NOT approved.** A stale proposal times out to a ``timeout``
  decision the caller treats as "don't execute" — the opposite of clarify's
  "proceed on best judgement". Nothing runs without an explicit approve.
- **Durable + idempotent + conflict-aware.** This class owns only the live
  Future; :class:`flowly.plans.manager.PlanManager` owns the persisted
  ``PlanApproval`` and calls :meth:`resolve_future` to wake the turn. That
  split means a decision recorded on disk after a restart is still coherent
  even though the Future is gone.

Background/cron runs have no human to approve, so a proposal there resolves
immediately to ``reject`` (``via="cron"``) instead of hanging the schedule.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional

from loguru import logger

from flowly.plans.models import PlanApproval

Decision = Literal["approve", "reject", "revise", "timeout"]

NotifyCallback = Callable[[PlanApproval, str], Awaitable[None]]


@dataclass
class PlanDecision:
    """The outcome the proposing turn receives."""

    decision: Decision
    feedback: Optional[str] = None
    decision_id: Optional[str] = None
    via: str = "surface"  # surface | timeout | cron

    @property
    def approved(self) -> bool:
        return self.decision == "approve"


@dataclass
class ResolveResult:
    """Result of a surface calling resolve. ``reason`` is machine-readable:
    ``resolved`` | ``already_resolved`` | ``revision_conflict`` |
    ``not_found`` | ``idempotent_replay``."""

    ok: bool
    reason: str


class PlanApprovalManager:
    """Live-Future coordinator for plan approvals (one process singleton)."""

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[PlanDecision]] = {}
        self._pending: dict[str, PlanApproval] = {}
        self._plan_of: dict[str, str] = {}  # approval_id → plan_id
        self._notify_callbacks: list[NotifyCallback] = []

    def add_notify_callback(self, cb: NotifyCallback) -> None:
        self._notify_callbacks.append(cb)

    # ── the proposing turn awaits here ──────────────────────────────────

    async def request_and_wait(
        self, approval: PlanApproval, plan_id: str
    ) -> PlanDecision:
        """Notify surfaces, then block until a decision or timeout.

        Cron short-circuit: no human at a surface, so return ``reject``
        immediately rather than hanging the schedule.
        """
        if _in_cron_context():
            logger.info(
                "[plan.approval] cron context — no approver, auto-reject "
                f"{approval.id}"
            )
            return PlanDecision("reject", via="cron")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[PlanDecision] = loop.create_future()
        self._futures[approval.id] = future
        self._pending[approval.id] = approval
        self._plan_of[approval.id] = plan_id

        for cb in self._notify_callbacks:
            try:
                await cb(approval, plan_id)
            except Exception as e:
                logger.error(f"[plan.approval] notify failed: {e}")

        timeout = max(0.0, approval.expiresAt - time.time())
        try:
            decision = await asyncio.wait_for(future, timeout=timeout)
            logger.info(f"[plan.approval] {approval.id} → {decision.decision}")
            return decision
        except asyncio.TimeoutError:
            logger.info(f"[plan.approval] {approval.id} timed out (not approved)")
            return PlanDecision("timeout", via="timeout")
        finally:
            self._futures.pop(approval.id, None)
            self._pending.pop(approval.id, None)
            self._plan_of.pop(approval.id, None)

    # ── surfaces resolve here ───────────────────────────────────────────

    def has_live_future(self, approval_id: str) -> bool:
        fut = self._futures.get(approval_id)
        return fut is not None and not fut.done()

    def resolve_future(
        self,
        approval_id: str,
        decision: Decision,
        *,
        feedback: Optional[str] = None,
        decision_id: Optional[str] = None,
    ) -> bool:
        """Wake a live proposing turn. Returns True if a Future was resolved.

        Returns False when no live Future exists (e.g. the turn died in a
        restart) — the caller (PlanManager) has already recorded the decision
        durably and will drive the ``plan.resume`` path instead.
        """
        fut = self._futures.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result(
            PlanDecision(decision, feedback=feedback, decision_id=decision_id)
        )
        return True

    def pending_approval(self, approval_id: str) -> Optional[PlanApproval]:
        return self._pending.get(approval_id)


def _in_cron_context() -> bool:
    try:
        from flowly.cron.context import in_cron_context

        return bool(in_cron_context())
    except Exception:
        return False


# ── process singleton ───────────────────────────────────────────────────

_singleton: Optional[PlanApprovalManager] = None


def get_plan_approval_manager() -> PlanApprovalManager:
    global _singleton
    if _singleton is None:
        _singleton = PlanApprovalManager()
    return _singleton


def reset_plan_approval_singleton() -> None:
    """Test hook — drop the singleton between tests."""
    global _singleton
    _singleton = None
