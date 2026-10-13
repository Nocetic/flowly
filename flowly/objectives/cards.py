"""The goal card under a reply: what a turn started tracking, or finished.

The ``objective`` tool's calls never reach a product surface
(:data:`flowly.tool_activity.INTERNAL_TOOLS`), so a client cannot read from
them that a goal was created. Instead, when the turn ends, the agent loop asks
this module which goals the turn created or closed and stores their cards on
the closing reply (``goal_cards`` in the session, ``goalCards`` on the wire),
next to its usage and attachments. A live reply and a reopened conversation
show the same card.

A card is a snapshot of the goal when the turn ended; clients compute the
time progress from ``createdAt``/``endsAt`` when they draw it, and open the
goal itself for anything current.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

from flowly.objectives.store import ObjectiveStore, get_objective_store

#: Cards one reply carries at most.
MAX_CARDS = 3
_GOAL_ID = re.compile(r"\bobj_[0-9a-f]{12}\b")


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text") or "") for part in content if isinstance(part, Mapping))
    return ""


def goal_cards_from_turn(
    messages: Iterable[Mapping[str, Any]], store: ObjectiveStore | None = None,
) -> list[dict[str, Any]]:
    """Cards for the goals these turn messages created or closed, in order."""
    from flowly.tool_activity import project_tool_call_for_ui

    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    events: dict[str, str] = {}  # goal id -> created | done | dropped, last one wins
    for message in messages:
        for call in message.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            projected = project_tool_call_for_ui(call)
            function = projected.get("function") or {}
            if function.get("name") == "objective" and isinstance(projected.get("id"), str):
                calls[projected["id"]] = (str(_arguments(function.get("arguments")).get("action") or ""),
                                          _arguments(function.get("arguments")))
        if message.get("role") != "tool":
            continue
        found = calls.get(str(message.get("tool_call_id") or ""))
        if found is None:
            continue
        action, args = found
        result = _text(message.get("content"))
        if result.startswith("Error"):
            continue
        if action == "create":
            match = _GOAL_ID.search(result)
            if match:
                events.pop(match.group(0), None)
                events[match.group(0)] = "created"
        elif action == "close":
            goal_id = str(args.get("id") or "")
            if _GOAL_ID.fullmatch(goal_id):
                events.pop(goal_id, None)
                events[goal_id] = "done" if (args.get("outcome") or "done") == "done" else "dropped"

    if not events:
        return []
    goals = store or get_objective_store()
    cards = []
    for goal_id, event in list(events.items())[-MAX_CARDS:]:
        goal = goals.get(goal_id)
        if goal is None:
            continue
        cards.append({
            "id": goal["id"],
            "event": event,
            "title": goal["title"],
            "category": goal["category"],
            "status": goal["status"],
            "createdAt": goal["createdAt"],
            "endsAt": goal.get("endsAt"),
            "progress": goal.get("progress") or {},
            "steps": len(goal.get("plan") or []),
            "stepsDone": sum(1 for step in goal.get("plan") or [] if step.get("done")),
        })
    return cards
