"""memory_feedback tool — record whether a recalled memory item was helpful, so
its confidence is adjusted over time (and wrong items get demoted to review).

Registered only when memory governance is enabled.
"""

from __future__ import annotations

from typing import Any

from flowly.agent.tools.base import Tool
from flowly.memory.governance import GovernanceError


class MemoryFeedbackTool(Tool):
    def __init__(self, *, facade):
        self._facade = facade

    @property
    def name(self) -> str:
        return "memory_feedback"

    @property
    def description(self) -> str:
        return (
            "Report whether a recalled memory item (by its id from memory_recall) "
            "was helpful or wrong. Helpful raises its trust; unhelpful lowers it "
            "and, if it drops too low, queues it for review. Use sparingly, only "
            "when a specific remembered fact clearly helped or was clearly wrong."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "The memory item id (m_…)."},
                "helpful": {"type": "boolean", "description": "True if it helped, False if wrong/outdated."},
                "note": {"type": "string", "description": "Optional short reason."},
            },
            "required": ["item_id", "helpful"],
        }

    async def execute(self, item_id: str, helpful: bool, note: str = "", **kwargs: Any) -> str:
        try:
            it = self._facade.ingest_feedback(item_id, bool(helpful), note or "")
        except GovernanceError as exc:
            return f"Error: {exc}"
        return (
            f"Recorded {'helpful' if helpful else 'unhelpful'} feedback on {it.id} "
            f"(confidence now {it.confidence:.2f}, status {it.status})."
        )
