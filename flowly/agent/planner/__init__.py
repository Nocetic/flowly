"""Browser-task planning subsystem.

Implements the planner-actor-validator pattern for multi-step browser
agent workflows. The Planner (this module) decomposes a goal into
schema-rich steps with success criteria; the Actor (the existing
agent loop) executes them; the Validator (later phase, separate
Haiku call) confirms each step's evidence matches the criteria
before letting the agent mark it done.

Design rationale captured in commit messages — see git log for the
research-driven decisions behind this architecture.

Phase 1 (this commit): models + in-memory state + browser_plan tool.
No loop.py changes, no validator, no end-turn guard. Pure additive
— if the tool is never called, behavior is unchanged.
"""

from flowly.agent.planner.models import Plan, Step, StepStatus, PlanStatus, Revision
from flowly.agent.planner.state import PlanStateManager, get_plan_state

__all__ = [
    "Plan",
    "Step",
    "StepStatus",
    "PlanStatus",
    "Revision",
    "PlanStateManager",
    "get_plan_state",
]
