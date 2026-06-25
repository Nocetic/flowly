"""Dataclasses for the planning subsystem.

Schema-rich on purpose: each Step carries its own success criteria
and an evidence slot that must be filled before status can flip to
"done". Skyvern's Validator pattern (research finding: pure self-
reflection fails; external verification is the only thing that
catches "claimed done but not done" hallucinations) reads these
fields.

Phase 1: just the data model. No validator, no enforcement.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Literal, Optional

# Step lifecycle states. Diagram:
#
#   pending ──(start)──> in_progress ──(evidence + validator OK)──> done
#                            │
#                            ├──(validator NO, retries < cap)──> in_progress (retry)
#                            ├──(retries >= cap or genuine block)──> blocked
#                            └──(agent decides not applicable)────> skipped
#
# We don't enforce the lifecycle in the dataclass — the tool layer
# does the gating so unit tests can construct any combination
# without ceremony.
StepStatus = Literal["pending", "in_progress", "done", "blocked", "skipped"]

# Plan-level status. "active" while at least one step is pending or
# in_progress; "complete" when complete() is called with final
# evidence; "blocked" when the agent calls mark_blocked at the plan
# level; "aborted" when the agent gives up and tells the user.
PlanStatus = Literal["active", "complete", "blocked", "aborted"]


@dataclass
class Revision:
    """One snapshot of a plan revision — append-only history.

    Every time the agent calls `revise(...)` we keep the previous
    step list here for audit. Lets the user (and us, in logs) see
    how the strategy evolved during a session. Phase 2 will mirror
    this to disk.
    """

    timestamp: float
    reason: str
    previous_steps_count: int
    new_steps_count: int


@dataclass
class Step:
    """One concrete unit of work in a plan.

    Field choices:
    - `content` (imperative) and `activeForm` (gerund) let a UI render
      "Clicking Format menu" while in_progress vs "Click Format menu"
      while pending, without the LLM having to emit both.
    - `successCriteria` is observable and validator-checkable. The Step
      is not "done" until this can be verified in the actual page state.
    - `evidence` slot stays None until done. Required when flipping
      to done. Free-form string — typically "screenshot shows ...",
      "DOM contains ...", or "URL changed to ...".
    - `validatorReasoning` records the validator's verdict so the
      audit log shows WHY a step was accepted/rejected.
    - `dependsOn` and `parallelGroup` reserved for Phase 3
      (conditional + parallel execution); Phase 1 ignores them.
    - `retries` counts validator failures so we know when to give up.
    """

    id: int
    content: str
    activeForm: str
    successCriteria: str
    status: StepStatus = "pending"
    evidence: Optional[str] = None
    validatorReasoning: Optional[str] = None
    dependsOn: list[int] = field(default_factory=list)
    runIf: Optional[str] = None
    parallelGroup: Optional[int] = None
    timeBudgetMs: int = 60_000
    retries: int = 0
    startedAt: Optional[float] = None
    completedAt: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict for JSON serialisation (tool result, disk persistence)."""
        return asdict(self)

    def short_label(self) -> str:
        """One-line label suitable for log lines and `_planContext` injection."""
        return f"step {self.id}: {self.content!r} [{self.status}]"


@dataclass
class Plan:
    """A complete decomposition of one user task.

    Created via `browser_plan(action="create", ...)`. Lives in the
    PlanStateManager keyed by session ID. Phase 2 will mirror to
    disk; Phase 1 is in-memory only — restart loses the plan, which
    is acceptable since plans are per-conversation-turn anyway.
    """

    id: str
    sessionId: str
    goal: str
    domains: list[str]
    steps: list[Step]
    status: PlanStatus = "active"
    createdAt: float = field(default_factory=time.time)
    updatedAt: float = field(default_factory=time.time)
    revisions: list[Revision] = field(default_factory=list)
    finalEvidence: Optional[str] = None

    @classmethod
    def new(
        cls,
        sessionId: str,
        goal: str,
        steps: list[Step],
        domains: Optional[list[str]] = None,
    ) -> "Plan":
        return cls(
            id=f"plan_{uuid.uuid4().hex[:12]}",
            sessionId=sessionId,
            goal=goal,
            domains=list(domains or []),
            steps=steps,
        )

    def get_step(self, step_id: int) -> Optional[Step]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def progress_summary(self) -> dict[str, int]:
        """Counts per status — small enough to inject in every result."""
        counts: dict[str, int] = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "skipped": 0}
        for s in self.steps:
            counts[s.status] = counts.get(s.status, 0) + 1
        counts["total"] = len(self.steps)
        return counts

    def current_step(self) -> Optional[Step]:
        """First in_progress, else first pending. Used for `_planContext`."""
        for s in self.steps:
            if s.status == "in_progress":
                return s
        for s in self.steps:
            if s.status == "pending":
                return s
        return None

    def is_done(self) -> bool:
        """All steps either done or skipped, AND complete() was called."""
        if self.status != "complete":
            return False
        for s in self.steps:
            if s.status not in ("done", "skipped"):
                return False
        return True

    def has_blockers(self) -> bool:
        return any(s.status == "blocked" for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_view(self) -> dict[str, Any]:
        """Trimmed dict suitable for tool result — drops internal fields."""
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "domains": self.domains,
            "progress": self.progress_summary(),
            "steps": [
                {
                    "id": s.id,
                    "content": s.content,
                    "successCriteria": s.successCriteria,
                    "status": s.status,
                    "evidence": s.evidence,
                    "validatorReasoning": s.validatorReasoning,
                }
                for s in self.steps
            ],
            "finalEvidence": self.finalEvidence,
            "revisions": len(self.revisions),
        }
