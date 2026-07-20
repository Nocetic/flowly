"""PlanManager — the orchestrator tying store + approval + broadcast together.

Single ownership rule: **all durable plan mutation goes through here.** The
proposing agent turn awaits :meth:`propose`; every surface resolves through
:meth:`resolve_approval`; every step tick goes through :meth:`update_step`.
Each mutation persists (via the store) and broadcasts a full ``plan.updated``
snapshot, so the four clients stay in sync and a re-entry can rehydrate from
``plan.get``.

Restart recovery (PLAN_MODE_PLAN.md §6): on start, any plan left ``executing``
is moved to ``paused`` — the Python coroutine that was running it is gone and
cannot be resumed, but the completed steps survive and the client shows a
"Resume" affordance backed by :meth:`resume`.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from flowly.plans.approval import (
    PlanApprovalManager,
    PlanDecision,
    ResolveResult,
    get_plan_approval_manager,
)
from flowly.plans.models import (
    GeneralPlan,
    PlanApproval,
    PlanStep,
    imperative_to_gerund,
    new_approval_id,
)
from flowly.plans.store import PlanStore, get_plan_store

# Default window a proposal waits for approval before timing out (= not
# approved). Generous: the user may be away from the surface.
DEFAULT_APPROVAL_TIMEOUT_S = 600

BroadcastCallback = Callable[[str, dict], Awaitable[None]]

# Tools with real side effects — blocked while a session is gated (forced
# plan mode armed but nothing approved yet, or a plan sits awaiting_approval).
# Read-oriented tools (read_file, list_dir, search, memory_recall, web_fetch,
# screenshot, clarify, plan itself) are deliberately absent so the agent can
# still investigate while it builds and proposes a plan. This is the backend
# enforcement of "propose first, then act" — it does not depend on the prompt.
SIDE_EFFECT_TOOLS: frozenset[str] = frozenset(
    {
        # local execution + filesystem writes
        "exec",
        "process",
        "shell",
        "write_file",
        "edit_file",
        "memory_append",
        "docker",
        # messaging / outbound
        "email",
        "message",
        "voice_call",
        # external services (mutating surfaces)
        "google_calendar",
        "google_drive",
        "google_tasks",
        "google_contacts",
        "linear",
        "github",
        "sentry",
        "trello",
        "homeassistant",
        # persistent-state authoring
        "board",
        "cron",
        "flowlet",
        "artifact",
        "image_generate",
        "knowledge_graph",
        # spawning work = a side effect
        "spawn",
        "delegate",
        "builtin_agent",
    }
)


class PlanManager:
    def __init__(
        self,
        store: Optional[PlanStore] = None,
        approvals: Optional[PlanApprovalManager] = None,
    ) -> None:
        self._store = store or get_plan_store()
        self._approvals = approvals or get_plan_approval_manager()
        self._on_change: Optional[BroadcastCallback] = None
        # Sessions where /plan armed forced mode but nothing is approved yet.
        # In-memory: forced mode is a per-turn intent, not durable state.
        self._forced_pending: set[str] = set()
        # Sessions with STICKY plan mode on (the standing mode, like the
        # exec permission levels): every normal message plans first until
        # the user turns it off. Hydrated from disk so a gateway restart
        # doesn't silently drop the mode (the exec policy survives restarts;
        # this must match, or the next message runs ungated while the user
        # still believes plan mode is on).
        self._sticky: set[str] = self._store.load_sticky()

    @property
    def store(self) -> PlanStore:
        return self._store

    def set_on_change(self, cb: Optional[BroadcastCallback]) -> None:
        """Wire the gateway/relay fan-out. Called once at gateway bootstrap."""
        self._on_change = cb

    # ── reads (RPC-facing) ──────────────────────────────────────────────

    def get_current(self, session_key: str) -> Optional[GeneralPlan]:
        return self._store.current_for_session(session_key)

    def get_plan(self, plan_id: str) -> Optional[GeneralPlan]:
        return self._store.get(plan_id)

    def list_for_session(self, session_key: str) -> list[GeneralPlan]:
        return self._store.all_for_session(session_key)

    # ── propose (the proposing turn awaits) ─────────────────────────────

    async def propose(
        self,
        session_key: str,
        goal: str,
        steps: list[PlanStep],
        *,
        title: str = "",
        mode: str = "forced",
        run_id: Optional[str] = None,
        details_md: Optional[str] = None,
        timeout_s: int = DEFAULT_APPROVAL_TIMEOUT_S,
    ) -> tuple[GeneralPlan, PlanDecision]:
        """Create (or revise) a plan, push it for approval, block until decided.

        If a non-terminal plan for this session is already ``awaiting_approval``
        or ``draft``, this call *revises it in place* (same id, revision bumped)
        — which is exactly what the agent does after a "revise" decision. Any
        other active plan (executing/paused) is aborted first: re-planning
        supersedes it.
        """
        existing = self._store.current_for_session(session_key)
        if existing and existing.status in ("awaiting_approval", "draft"):
            plan = existing
            plan.goal = goal.strip() or plan.goal
            if title.strip():
                plan.title = title.strip()[:200]
            if details_md is not None:
                plan.detailsMd = details_md
            if run_id:
                plan.runId = run_id
            plan.replace_steps(steps)
            plan.status = "awaiting_approval"
            plan.touch("revised")
        else:
            if existing:
                existing.status = "aborted"
                existing.touch("superseded by a new plan")
                self._store.save(existing)
                await self._broadcast(existing)
            plan = GeneralPlan.new(
                session_key,
                goal,
                steps,
                title=title,
                mode="auto" if mode == "auto" else "forced",
                run_id=run_id,
                details_md=details_md,
            )
            plan.status = "awaiting_approval"
            plan.touch("proposed")

        now = time.time()
        approval = PlanApproval(
            id=new_approval_id(),
            revision=plan.revision,
            createdAt=now,
            expiresAt=now + timeout_s,
        )
        plan.approval = approval
        self._store.save(plan)
        await self._broadcast(plan, approval_requested=True)

        decision = await self._approvals.request_and_wait(approval, plan.id)

        # Surface decisions are applied durably by resolve_approval already.
        # Only timeout/cron come back here unapplied.
        if decision.via in ("timeout", "cron"):
            await self._apply_decision(plan, decision, live_turn=True)

        plan = self._store.get(plan.id) or plan
        return plan, decision

    # ── resolve (surface-facing RPC) ────────────────────────────────────

    async def resolve_approval(
        self,
        plan_id: str,
        decision: str,
        *,
        feedback: Optional[str] = None,
        expected_revision: Optional[int] = None,
        decision_id: Optional[str] = None,
    ) -> ResolveResult:
        plan = self._store.get(plan_id)
        if not plan or not plan.approval:
            return ResolveResult(False, "not_found")
        approval = plan.approval

        # Idempotent replay of the same decision (flaky WS retried the RPC).
        if approval.resolved:
            if decision_id and approval.decisionId == decision_id:
                return ResolveResult(True, "idempotent_replay")
            return ResolveResult(False, "already_resolved")

        # Lost a revision race (a stale client answered an old revision).
        if expected_revision is not None and approval.revision != expected_revision:
            return ResolveResult(False, "revision_conflict")

        if decision not in ("approve", "reject", "revise"):
            return ResolveResult(False, "invalid_decision")

        live = self._approvals.has_live_future(approval.id)
        pd = PlanDecision(decision, feedback=feedback, decision_id=decision_id)
        await self._apply_decision(plan, pd, live_turn=live)
        if live:
            self._approvals.resolve_future(
                approval.id, decision, feedback=feedback, decision_id=decision_id
            )
        return ResolveResult(True, "resolved")

    async def _apply_decision(
        self, plan: GeneralPlan, decision: PlanDecision, *, live_turn: bool
    ) -> None:
        if plan.approval:
            plan.approval.resolved = True
            plan.approval.decision = (
                None if decision.decision == "timeout" else decision.decision
            )
            plan.approval.decisionId = decision.decision_id
        if decision.decision == "approve":
            # A live proposing turn will execute now; without one (restart
            # window) the plan is approved-but-idle until plan.resume runs it.
            plan.status = "executing" if live_turn else "approved"
            plan.touch("approved")
        elif decision.decision == "reject":
            plan.status = "rejected"
            plan.touch("rejected")
        elif decision.decision == "revise":
            # Stays awaiting; the agent re-proposes into the same plan id.
            plan.status = "awaiting_approval"
            plan.touch("revise requested")
        elif decision.decision == "timeout":
            plan.status = "aborted"
            plan.touch("approval timed out — not approved")
        self._store.save(plan)
        await self._broadcast(plan)

    # ── execution mutations ─────────────────────────────────────────────

    async def update_step(
        self,
        plan_id: str,
        step_id: int,
        status: str,
        *,
        note: Optional[str] = None,
    ) -> Optional[GeneralPlan]:
        plan = self._store.get(plan_id)
        if not plan:
            return None
        step = plan.get_step(step_id)
        if not step:
            return None
        now = time.time()
        if status == "in_progress" and step.startedAt is None:
            step.startedAt = now
        if status in ("completed", "blocked", "skipped") and step.completedAt is None:
            step.completedAt = now
        step.status = status  # type: ignore[assignment]
        if note is not None:
            step.note = note
        if plan.status in ("approved", "paused"):
            plan.status = "executing"
        plan.touch(f"step {step_id} → {status}")
        self._store.save(plan)
        await self._broadcast(plan)
        return plan

    async def complete(
        self, plan_id: str, summary: str = ""
    ) -> Optional[GeneralPlan]:
        plan = self._store.get(plan_id)
        if not plan:
            return None
        plan.completionSummary = summary.strip() or None
        plan.status = "completed"
        plan.touch("completed")
        self._store.save(plan)
        await self._broadcast(plan)
        return plan

    async def mark_blocked(
        self, plan_id: str, reason: str = ""
    ) -> Optional[GeneralPlan]:
        plan = self._store.get(plan_id)
        if not plan:
            return None
        plan.completionSummary = reason.strip() or None
        plan.status = "blocked"
        plan.touch("blocked")
        self._store.save(plan)
        await self._broadcast(plan)
        return plan

    async def cancel(self, plan_id: str) -> Optional[GeneralPlan]:
        plan = self._store.get(plan_id)
        if not plan:
            return None
        plan.status = "aborted"
        plan.touch("cancelled")
        self._store.save(plan)
        await self._broadcast(plan)
        return plan

    async def abort_active_for_session(self, session_key: str, reason: str) -> None:
        """Abort a session's active plan — used by /clear and session deletion."""
        plan = self._store.current_for_session(session_key)
        if not plan:
            return
        plan.status = "aborted"
        plan.touch(f"aborted: {reason}")
        self._store.save(plan)
        await self._broadcast(plan)

    async def resume(self, plan_id: str) -> Optional[GeneralPlan]:
        """Mark a paused/approved plan executing again. The caller (RPC layer)
        starts a fresh agent turn seeded with the plan's goal + completed
        steps; this only flips the state and broadcasts."""
        plan = self._store.get(plan_id)
        if not plan or plan.status not in ("paused", "approved"):
            return None
        plan.status = "executing"
        plan.touch("resumed")
        self._store.save(plan)
        await self._broadcast(plan)
        return plan

    # ── restart recovery ────────────────────────────────────────────────

    def recover_on_start(self) -> int:
        """Move every ``executing`` plan to ``paused`` (its coroutine is gone).
        Returns the number recovered. No broadcast — no clients are bound yet;
        they'll fetch fresh state via ``plan.get`` on connect."""
        recovered = 0
        for plan in self._store.all_plans():
            if plan.status == "executing":
                plan.status = "paused"
                plan.touch("paused: bot restarted")
                self._store.save(plan)
                recovered += 1
        if recovered:
            logger.info(f"[plans] recovered {recovered} interrupted plan(s) → paused")
        return recovered

    # ── forced-mode gate ────────────────────────────────────────────────

    def arm_forced(self, session_key: str) -> None:
        """Mark a session as forced-plan: side-effect tools are blocked until a
        plan is approved. Called by the ``/plan`` handler."""
        self._forced_pending.add(session_key)

    def set_sticky(self, session_key: str, on: bool) -> None:
        """Turn the standing plan mode on/off for a session. Off also drops any
        armed one-shot gate so the next message runs normally. Persisted, so
        the mode survives a gateway restart like the exec policy does."""
        if on:
            self._sticky.add(session_key)
        else:
            self._sticky.discard(session_key)
            self._forced_pending.discard(session_key)
        self._store.save_sticky(self._sticky)

    def is_sticky(self, session_key: str) -> bool:
        return session_key in self._sticky

    def disarm_forced(self, session_key: str) -> None:
        self._forced_pending.discard(session_key)

    def is_forced_pending(self, session_key: str) -> bool:
        return session_key in self._forced_pending

    def is_gate_active(self, session_key: str) -> bool:
        """True while the session must not run side effects yet: either forced
        mode is armed with nothing approved, or a plan is sitting in a
        pre-execution state (draft / awaiting_approval)."""
        if session_key in self._forced_pending:
            return True
        plan = self._store.current_for_session(session_key)
        return bool(plan and plan.status in ("draft", "awaiting_approval"))

    def gate_blocks(self, session_key: str, tool_name: str) -> bool:
        """Whether this tool call must be blocked right now (backend
        enforcement of plan-before-act)."""
        if tool_name not in SIDE_EFFECT_TOOLS:
            return False
        return self.is_gate_active(session_key)

    def gate_reason(self, session_key: str) -> str:
        plan = self._store.current_for_session(session_key)
        if plan and plan.status == "awaiting_approval":
            return (
                "A plan is waiting for the user's approval. You can't take "
                "side-effecting actions until they approve it. Wait for the "
                "decision, or revise the plan."
            )
        return (
            "Plan mode is on for this task: propose a plan with "
            "plan(action='propose', ...) and get the user's approval BEFORE "
            "any side-effecting action. You may still read files and search "
            "while planning."
        )

    # ── broadcast ───────────────────────────────────────────────────────

    async def _broadcast(
        self, plan: GeneralPlan, *, approval_requested: bool = False
    ) -> None:
        if self._on_change is None:
            return
        snapshot = plan.public_view()
        try:
            await self._on_change("plan.updated", snapshot)
            if approval_requested:
                await self._on_change("plan.approval.requested", snapshot)
        except Exception as e:
            logger.debug(f"[plans] broadcast failed (non-fatal): {e}")

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def build_steps(raw_steps: list[dict[str, Any]]) -> list[PlanStep]:
        """Coerce tool-supplied step dicts into PlanStep, filling activeForm."""
        steps: list[PlanStep] = []
        seen: set[int] = set()
        for i, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, dict):
                continue
            sid = raw.get("id")
            if not isinstance(sid, int) or sid < 1:
                sid = i
            if sid in seen:
                sid = max(seen) + 1
            seen.add(sid)
            content = str(raw.get("content", "")).strip()
            if not content:
                continue
            active = str(raw.get("activeForm", "")).strip() or imperative_to_gerund(
                content
            )
            steps.append(
                PlanStep(
                    id=sid,
                    content=content,
                    activeForm=active,
                    note=(str(raw["note"]).strip() if raw.get("note") else None),
                )
            )
        return steps


# ── process singleton ───────────────────────────────────────────────────

_singleton: Optional[PlanManager] = None


def get_plan_manager() -> PlanManager:
    global _singleton
    if _singleton is None:
        _singleton = PlanManager()
    return _singleton


def reset_plan_manager_singleton() -> None:
    """Test hook — drop the singleton between tests."""
    global _singleton
    _singleton = None
