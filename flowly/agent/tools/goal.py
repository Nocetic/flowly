"""Standing-goal control for the agent itself.

The user can always drive goals with ``/goal``; this tool exists so they can
also drive them in plain language — "keep working on this until the tests
pass", "goal yap bunu", "pause the goal". The AGENT never invents a goal: a
goal starts only when the user explicitly asked for one, and the tool's
description carries that contract.

Setting a goal mid-turn is deliberately light: state is written and every
chip updates immediately, and the CURRENT turn becomes the goal's first turn
— its delivery already flows through the judge, so the autonomous loop takes
over with no extra kickoff machinery.
"""

from __future__ import annotations

import json
from typing import Any

from flowly.agent.tools.base import Tool


class GoalTool(Tool):
    """Set, inspect, pause, resume or end this conversation's standing goal."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    @property
    def name(self) -> str:
        return "goal"

    @property
    def description(self) -> str:
        return (
            "Manage this conversation's standing goal — a durable objective "
            "the agent keeps working toward across turns, with a judge "
            "deciding after each turn whether to continue, and pause/resume/"
            "stop controls on every client. Actions: set (ONLY when the user "
            "explicitly asks for ongoing/autonomous work — never invent one), "
            "status, pause, resume, stop. Setting a goal makes the current "
            "turn its first turn; finish this reply by starting the work."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "status", "pause", "resume", "stop"],
                    "description": "What to do with the standing goal.",
                },
                "goal": {
                    "type": "string",
                    "description": (
                        "For set: the objective, in the user's own terms. "
                        "Keep their constraints; do not embellish."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str = "status",
        goal: str = "",
        **kwargs: Any,
    ) -> str:
        session_key = str(kwargs.get("session_key") or "")
        manager = getattr(self._agent, "goal_manager", None)
        if not session_key or manager is None:
            return "Error: standing goals are unavailable in this configuration."
        action = str(action or "").strip().lower()

        if action == "status":
            state = manager.get(session_key)
            if state is None:
                return json.dumps({"goal": None, "note": "no standing goal"})
            return json.dumps({"goal": state.to_public_dict()}, ensure_ascii=False)

        if action in ("pause", "resume", "stop"):
            result = await self._agent.goal_control(session_key, action)
            snapshot = result.get("goal")
            if snapshot is None:
                return "No standing goal to modify."
            return json.dumps({"goal": snapshot}, ensure_ascii=False)

        if action == "set":
            text = str(goal or "").strip()
            if not text:
                return "Error: a goal text is required to set a standing goal."
            handler = getattr(self._agent, "goal_commands", None)
            if handler is None:
                return "Error: standing goals are unavailable in this configuration."
            result = await handler.goal(
                session_key,
                text,
                conversation_epoch=self._agent.context_epoch(session_key),
            )
            state = result.state
            if state is None:
                return f"Error: {result.content}"
            # Every chip updates now, before this turn even finishes.
            if ":" in session_key:
                channel, chat_id = session_key.split(":", 1)
            else:
                channel, chat_id = "cli", session_key
            try:
                await self._agent.publish_goal_snapshot(
                    session_key, channel, chat_id, result.content, state,
                )
            except Exception:  # noqa: BLE001 — the state write already succeeded
                pass
            return (
                f"Standing goal set ({state.max_turns}-turn budget): {state.goal}. "
                "This turn is its first turn — continue directly with the work; "
                "after each turn a judge decides whether to keep going."
            )

        return f"Error: unknown goal action {action!r}."
