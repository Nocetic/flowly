"""General, session-level plan system.

Distinct from ``flowly.agent.planner`` (browser-coupled): this is the
surface that powers "plan mode" across every client — the agent proposes
a plan, the user approves/rejects/revises it, then the steps stream to
the composer of Desktop/iOS/TUI with live ticks, synced over feature_rpc
(gateway + relay) and resumable after leaving/returning to a chat or even
after a bot restart.

Public surface:

- :mod:`flowly.plans.models` — ``GeneralPlan`` / ``PlanStep`` / statuses
  / ``PlanApproval`` and the frozen wire schema (``public_view``).
- :mod:`flowly.plans.store` — atomic disk persistence + startup hydration.
- :mod:`flowly.plans.approval` — durable, Future-based approval gate.
- :mod:`flowly.plans.manager` — process singleton tying it together, with
  a broadcast hook and restart recovery (``executing`` → ``paused``).
"""

from flowly.plans.approval import (
    PlanApprovalManager,
    PlanDecision,
    get_plan_approval_manager,
)
from flowly.plans.manager import PlanManager, get_plan_manager
from flowly.plans.models import (
    GeneralPlan,
    PlanApproval,
    PlanStatus,
    PlanStep,
    Revision,
    StepStatus,
    new_decision_id,
    new_plan_id,
)
from flowly.plans.store import PlanStore

__all__ = [
    "GeneralPlan",
    "PlanApproval",
    "PlanStatus",
    "PlanStep",
    "Revision",
    "StepStatus",
    "new_decision_id",
    "new_plan_id",
    "PlanStore",
    "PlanApprovalManager",
    "PlanDecision",
    "get_plan_approval_manager",
    "PlanManager",
    "get_plan_manager",
]
