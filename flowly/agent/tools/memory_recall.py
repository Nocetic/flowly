"""memory_recall tool — surface active governed memory (with item ids) to the
agent so it can cite them and give trust feedback via memory_feedback.

Registered only when memory governance is enabled.
"""

from __future__ import annotations

import json
from typing import Any

from flowly.agent.tools.base import Tool


class MemoryRecallTool(Tool):
    def __init__(self, *, facade):
        self._facade = facade

    @property
    def name(self) -> str:
        return "memory_recall"

    @property
    def description(self) -> str:
        return (
            "Recall active long-term memory about the user (preferences, facts, "
            "projects) as structured items WITH ids and confidence. Use when you "
            "need to check what is reliably known. If a recalled item turns out "
            "helpful or wrong, report it with memory_feedback(item_id, helpful)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "include_sensitive": {
                    "type": "boolean",
                    "description": "Include sensitive items (default false). Never returns secrets.",
                },
                "limit": {"type": "integer", "description": "Max items (highest-trust first)."},
            },
            "required": [],
        }

    async def execute(self, include_sensitive: bool = False, limit: int = 20, **kwargs: Any) -> str:
        out = self._facade.recall(include_sensitive=bool(include_sensitive), limit=int(limit))
        return json.dumps(out, ensure_ascii=False)
