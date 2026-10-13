"""Goals an agent tracks for its owner over days or weeks.

Not the conversation's standing goal (``flowly/goals``, ``/goal``): that one
keeps the agent working inside one conversation until a judge says done. A
tracked goal is the owner's own aim ("work out every day this week", "buy a
laptop under 40k") that the agent follows up on over time, through its
agenda (``flowly/agenda``). See ``docs/engineering/agent-goals.md``.
"""

from flowly.objectives.store import (
    CATEGORIES,
    MAX_ACTIVE_OBJECTIVES,
    STATUSES,
    ObjectiveError,
    ObjectiveStore,
    get_objective_store,
)

__all__ = [
    "CATEGORIES",
    "MAX_ACTIVE_OBJECTIVES",
    "STATUSES",
    "ObjectiveError",
    "ObjectiveStore",
    "get_objective_store",
]
