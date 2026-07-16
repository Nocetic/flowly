"""Dataclasses + wire schema for the general plan system.

The wire schema (``GeneralPlan.public_view``) is the frozen contract every
client speaks — see ``PLAN_MODE_PLAN.md`` §3. Two invariants matter most:

- ``revision`` is a monotonic counter bumped on *every* mutation. Clients
  drop any snapshot whose revision is ≤ the one they already hold, so a
  reordered or duplicated ``plan.updated`` can never regress the UI.
- ``steps`` is the canonical source of truth for progress; ``detailsMd`` is
  cosmetic (the plan-document card body) and is never parsed for state.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

# ── Status vocabularies ─────────────────────────────────────────────────

# Plan lifecycle. See PLAN_MODE_PLAN.md §1 for the transition diagram.
#   draft ─▶ awaiting_approval ─▶ approved ─▶ executing ─▶ completed
#   reject ▶ rejected · revise ▶ awaiting_approval · restart ▶ paused
#   anything ▶ aborted
PlanStatus = Literal[
    "draft",
    "awaiting_approval",
    "approved",
    "executing",
    "paused",
    "completed",
    "blocked",
    "rejected",
    "aborted",
]

# Steps done or not-applicable count as "settled" for completion checks.
StepStatus = Literal["pending", "in_progress", "completed", "blocked", "skipped"]

# Terminal plan states never mutate further (except an explicit archive).
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "blocked", "rejected", "aborted"}
)

# The decision a surface sends for a pending approval.
Decision = Literal["approve", "reject", "revise"]


def new_plan_id() -> str:
    return f"plan_{uuid.uuid4().hex[:12]}"


def new_approval_id() -> str:
    return f"pa_{secrets.token_hex(6)}"


def new_decision_id() -> str:
    """Idempotency token a client stamps on ``plan.resolve`` so a retried
    decision (flaky WS) is applied at most once."""
    return f"pd_{secrets.token_hex(8)}"


# ── Building blocks ─────────────────────────────────────────────────────


@dataclass
class Revision:
    """One entry in a plan's append-only revision history.

    Kept small on purpose — mirrored to ``<plan>.revisions.log`` one JSON
    line per mutation for an operator-facing audit trail.
    """

    revision: int
    timestamp: float
    reason: str
    status: PlanStatus


@dataclass
class PlanStep:
    """One concrete unit of work.

    ``content`` is imperative ("Add the RPC handler"); ``activeForm`` is the
    gerund a UI shows while the step runs ("Adding the RPC handler"). Unlike
    the browser planner, a general step carries no mandatory ``successCriteria``
    or evidence — ``note`` is a free-form annotation only.
    """

    id: int
    content: str
    activeForm: str = ""
    status: StepStatus = "pending"
    note: Optional[str] = None
    startedAt: Optional[float] = None
    completedAt: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanApproval:
    """A pending approval attached to a specific plan revision.

    Persisted inside the plan JSON (not only in an in-memory Future) so that
    a bot restart doesn't strand a question the user can still answer.
    ``decisionId`` records the idempotency token of the decision that
    resolved it (``None`` while still pending).
    """

    id: str
    revision: int
    createdAt: float
    expiresAt: float
    decisionId: Optional[str] = None
    resolved: bool = False
    decision: Optional[Decision] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision": self.revision,
            "expiresAt": self.expiresAt,
        }


# ── The plan ────────────────────────────────────────────────────────────


@dataclass
class GeneralPlan:
    """A session-scoped task plan.

    One active plan per ``sessionKey`` (a re-plan aborts the previous one).
    ``revision`` bumps on every mutation via :meth:`touch`.
    """

    id: str
    sessionKey: str
    goal: str
    title: str
    steps: list[PlanStep]
    kind: Literal["general", "browser"] = "general"
    mode: Literal["auto", "forced"] = "forced"
    status: PlanStatus = "draft"
    revision: int = 0
    runId: Optional[str] = None
    detailsMd: Optional[str] = None
    approval: Optional[PlanApproval] = None
    completionSummary: Optional[str] = None
    createdAt: float = field(default_factory=time.time)
    updatedAt: float = field(default_factory=time.time)
    revisions: list[Revision] = field(default_factory=list)

    # ── factory ─────────────────────────────────────────────────────────

    @classmethod
    def new(
        cls,
        session_key: str,
        goal: str,
        steps: list[PlanStep],
        *,
        title: str = "",
        mode: Literal["auto", "forced"] = "forced",
        run_id: Optional[str] = None,
        details_md: Optional[str] = None,
        kind: Literal["general", "browser"] = "general",
    ) -> "GeneralPlan":
        plan = cls(
            id=new_plan_id(),
            sessionKey=session_key,
            goal=goal.strip(),
            title=(title.strip() or goal.strip())[:200],
            steps=steps,
            kind=kind,
            mode=mode,
            runId=run_id,
            detailsMd=details_md,
        )
        plan._record_revision("created")
        return plan

    # ── mutation helpers ────────────────────────────────────────────────

    def touch(self, reason: str = "") -> None:
        """Bump revision + updatedAt and append an audit entry. EVERY mutation
        that clients should see must funnel through here so ``revision`` stays
        monotonic and no snapshot regresses the UI."""
        self.revision += 1
        self.updatedAt = time.time()
        self._record_revision(reason)

    def _record_revision(self, reason: str) -> None:
        self.revisions.append(
            Revision(
                revision=self.revision,
                timestamp=self.updatedAt,
                reason=reason or self.status,
                status=self.status,
            )
        )

    def get_step(self, step_id: int) -> Optional[PlanStep]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def replace_steps(self, steps: list[PlanStep]) -> None:
        self.steps = steps

    def progress_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "blocked": 0,
            "skipped": 0,
        }
        for s in self.steps:
            counts[s.status] = counts.get(s.status, 0) + 1
        counts["total"] = len(self.steps)
        return counts

    def current_step(self) -> Optional[PlanStep]:
        """First in_progress, else first pending — drives ``_planContext``."""
        for s in self.steps:
            if s.status == "in_progress":
                return s
        for s in self.steps:
            if s.status == "pending":
                return s
        return None

    def all_settled(self) -> bool:
        """Every step is completed or skipped (ready to declare complete)."""
        return all(s.status in ("completed", "skipped") for s in self.steps)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    # ── serialisation ───────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Full state for disk persistence (round-trips via :meth:`from_dict`)."""
        return {
            "id": self.id,
            "sessionKey": self.sessionKey,
            "goal": self.goal,
            "title": self.title,
            "steps": [s.to_dict() for s in self.steps],
            "kind": self.kind,
            "mode": self.mode,
            "status": self.status,
            "revision": self.revision,
            "runId": self.runId,
            "detailsMd": self.detailsMd,
            "approval": self.approval.to_dict() if self.approval else None,
            "completionSummary": self.completionSummary,
            "createdAt": self.createdAt,
            "updatedAt": self.updatedAt,
            "revisions": [asdict(r) for r in self.revisions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GeneralPlan":
        # Filter to known fields so a schema drift (or a stray browser-plan
        # file that landed in the dir) is tolerated, not fatal.
        _step_fields = set(PlanStep.__dataclass_fields__)
        steps = [
            PlanStep(**{k: v for k, v in s.items() if k in _step_fields})
            for s in (data.get("steps") or [])
        ]
        approval_raw = data.get("approval")
        _appr_fields = set(PlanApproval.__dataclass_fields__)
        approval = (
            PlanApproval(**{k: v for k, v in approval_raw.items() if k in _appr_fields})
            if approval_raw
            else None
        )
        _rev_fields = set(Revision.__dataclass_fields__)
        revisions = [
            Revision(**{k: v for k, v in r.items() if k in _rev_fields})
            for r in (data.get("revisions") or [])
        ]
        return cls(
            id=data["id"],
            sessionKey=data["sessionKey"],
            goal=data.get("goal", ""),
            title=data.get("title", ""),
            steps=steps,
            kind=data.get("kind", "general"),
            mode=data.get("mode", "forced"),
            status=data.get("status", "draft"),
            revision=int(data.get("revision", 0)),
            runId=data.get("runId"),
            detailsMd=data.get("detailsMd"),
            approval=approval,
            completionSummary=data.get("completionSummary"),
            createdAt=float(data.get("createdAt", time.time())),
            updatedAt=float(data.get("updatedAt", time.time())),
            revisions=revisions,
        )

    def public_view(self) -> dict[str, Any]:
        """The frozen wire snapshot (PLAN_MODE_PLAN.md §3). Identical shape
        for ``plan.get`` and ``plan.updated``."""
        return {
            "id": self.id,
            "sessionKey": self.sessionKey,
            "runId": self.runId,
            "kind": self.kind,
            "mode": self.mode,
            "status": self.status,
            "revision": self.revision,
            "title": self.title,
            "goal": self.goal,
            "detailsMd": self.detailsMd,
            "steps": [
                {
                    "id": s.id,
                    "content": s.content,
                    "activeForm": s.activeForm or s.content,
                    "status": s.status,
                    "note": s.note,
                }
                for s in self.steps
            ],
            "progress": self.progress_summary(),
            "approval": (
                self.approval.public_view()
                if self.approval and not self.approval.resolved
                else None
            ),
            "completionSummary": self.completionSummary,
            "createdAt": self.createdAt,
            "updatedAt": self.updatedAt,
        }


def imperative_to_gerund(text: str) -> str:
    """Cheap "Click X" → "Clicking X" for a step's activeForm when omitted."""
    if not text:
        return ""
    words = text.split()
    if not words:
        return text
    verb = words[0]
    suffix = ""
    while verb and not verb[-1].isalpha():
        suffix = verb[-1] + suffix
        verb = verb[:-1]
    if not verb:
        return text
    lower = verb.lower()
    if lower.endswith("e") and not lower.endswith("ee"):
        gerund = verb[:-1] + "ing"
    elif lower.endswith("ie"):
        gerund = verb[:-2] + "ying"
    else:
        gerund = verb + "ing"
    if verb[0].isupper():
        gerund = gerund[0].upper() + gerund[1:]
    return " ".join([gerund + suffix, *words[1:]]).strip()
