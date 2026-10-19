"""Goals the agent tracks for its owner over days or weeks.

A tracked goal is the owner's aim ("work out every day this week", "find a
laptop under 40k"), with a plan, a stage and a short log. The agent opens one
when a plan comes up in conversation (and says so), checks in on it on the
rhythm it chose, adjusts the plan as things go, and closes it when it is done
or over. Each goal has at most one planned check-in, held by the agenda
(:mod:`flowly.agenda`); ``next_check`` plans or moves it.

Not the conversation's standing goal (the ``goal`` tool), which keeps the
agent working on a task until a judge says it is finished.
"""

from __future__ import annotations

import json
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.agent.tools.follow_up import owner_now, resolve_conversation, timing_note
from flowly.objectives.store import CATEGORIES, ObjectiveError

_FIELD_MAP = {
    "title": "title",
    "category": "category",
    "why": "why",
    "plan": "plan",
    "stage": "stage",
    "cadence": "cadence",
    "current": "current",
    "target": "target",
    "unit": "unit",
    "done_steps": "doneSteps",
}


class ObjectiveTool(Tool):
    @property
    def name(self) -> str:
        return "objective"

    @property
    def description(self) -> str:
        return (
            "Track the owner's own goals over days or weeks (health, relationships, finance, career, "
            "learning, interests, productivity, other): a plan, a stage, progress, and check-ins on a "
            "rhythm. When the owner gives a timeframe (a week, a month, by December), set ends so the "
            "goal shows how far along it is. "
            "Create one when the owner sets out to do something over time and a plan comes together "
            "(\"a week of daily workouts\", \"save 500 by December\", \"find a laptop\") — even if they "
            "did not say 'goal' — and tell them in your reply that you will keep track of it. Then plan "
            "its first check-in with next_check. On each check-in: log what happened, adjust the plan "
            "or stage when it should change, and plan the next check-in; close it when it is done or "
            "the owner lets it go. The owner sees these goals in the app and can pause or stop them. "
            "Not the conversation's standing goal (the goal tool) and not a one-off reminder (cron). "
            "Actions: create, update, log, close, list, get."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "update", "log", "close", "list", "get"]},
                "id": {"type": "string", "description": "The goal's id (all actions except create and list)."},
                "title": {"type": "string", "description": "Short, in the owner's words and language."},
                "category": {"type": "string", "enum": list(CATEGORIES)},
                "why": {"type": "string", "description": "Why it matters to the owner, if they said."},
                "plan": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The steps, in order (replaces the plan on update).",
                },
                "done_steps": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "On update: plan step numbers (1-based) now done.",
                },
                "stage": {"type": "string", "description": "Where it stands, e.g. 'Day 3 of 7'."},
                "cadence": {"type": "string", "description": "The check-in rhythm in words, e.g. 'every evening at 20:00'."},
                "current": {"type": "number", "description": "Progress so far, when it is measurable."},
                "target": {"type": "number", "description": "The measurable target, if any."},
                "unit": {"type": "string", "description": "Unit of current/target, e.g. 'days', 'TRY'."},
                "ends": {
                    "type": "string",
                    "description": "When the goal ends: '+7d' for a week, '+30d' for a month, or an ISO date. Set it whenever a timeframe was said.",
                },
                "next_check": {
                    "type": "string",
                    "description": "When to check in next: '20:00', 'tomorrow 20:00', '+1d', ISO. Replaces the planned one.",
                },
                "check_note": {"type": "string", "description": "What to look at on that check-in."},
                "note": {"type": "string", "description": "For log and close: what happened."},
                "outcome": {"type": "string", "enum": ["done", "dropped"], "description": "For close."},
                "conversation": {
                    "type": "string",
                    "description": "Only in the daily review: the owner's conversation (session key) the goal belongs to.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str = "list", **kwargs: Any) -> str:
        from flowly.agenda.timeparse import WhenError

        try:
            return self._run(action, kwargs)
        except (ObjectiveError, WhenError) as exc:
            return f"Error: {exc}"

    def _run(self, action: str, args: dict[str, Any]) -> str:
        from flowly.agenda.store import get_agenda_store
        from flowly.agenda.timeparse import describe, parse_when
        from flowly.objectives.store import get_objective_store

        goals = get_objective_store()
        agenda = get_agenda_store()

        if action == "list":
            _now, zone = owner_now()
            checks = {i["objectiveId"]: int(i["dueAt"]) for i in agenda.pending() if i.get("objectiveId")}
            rows = []
            for goal in goals.list(include_closed=False):
                rows.append({
                    "id": goal["id"],
                    "title": goal["title"],
                    "category": goal["category"],
                    "status": goal["status"],
                    "stage": goal.get("stage") or "",
                    "nextCheck": describe(checks[goal["id"]], zone) if goal["id"] in checks else None,
                    "conversation": goal.get("sessionKey") or "",
                })
            return json.dumps(rows, ensure_ascii=False)

        if action == "create":
            key = resolve_conversation(args.get("conversation"))
            now, zone = owner_now()
            ends_at = parse_when(args["ends"], now=now) if args.get("ends") else None
            due = parse_when(args["next_check"], now=now) if args.get("next_check") else None
            goal = goals.create(
                title=args.get("title"),
                category=args.get("category") or "other",
                why=args.get("why") or "",
                plan=args.get("plan"),
                stage=args.get("stage") or "",
                cadence=args.get("cadence") or "",
                target=args.get("target"),
                current=args.get("current"),
                unit=args.get("unit") or "",
                ends_at=ends_at,
                session_key=key,
                created_by="agent",
            )
            message = f"Tracking goal {goal['id']}: {goal['title']}."
            if due is not None:
                agenda.schedule(
                    kind="objective", due_at=due, session_key=key, objective_id=goal["id"],
                    note=args.get("check_note") or f"Check in on: {goal['title']}",
                )
                message += f" First check-in {describe(due, zone)}." + timing_note(due, zone)
            else:
                message += " No check-in planned yet: set one with next_check."
            return message + " Tell the owner you will keep track of it."

        goal_id = str(args.get("id") or "").strip()
        if not goal_id:
            return "Error: id is required."
        goal = goals.get(goal_id)
        if goal is None:
            return f"Error: no goal {goal_id}."

        if action == "get":
            next_item = agenda.next_for_objective(goal_id)
            _now, zone = owner_now()
            return json.dumps({
                **goal,
                "nextCheck": describe(int(next_item["dueAt"]), zone) if next_item else None,
            }, ensure_ascii=False)

        if action == "update":
            changes = {_FIELD_MAP[k]: v for k, v in args.items() if k in _FIELD_MAP and v is not None}
            now, zone = owner_now()
            if args.get("ends"):
                changes["endsAt"] = parse_when(args["ends"], now=now)
            message_parts = []
            if changes:
                goal = goals.update(goal_id, changes)
                message_parts.append(f"Updated {goal_id}.")
            if args.get("next_check"):
                if goal.get("status") != "active":
                    return f"Error: {goal_id} is {goal.get('status')}; resume it before planning a check-in."
                due = parse_when(args["next_check"], now=now)
                key = goal.get("sessionKey") or resolve_conversation(args.get("conversation"))
                agenda.schedule(
                    kind="objective", due_at=due, session_key=key, objective_id=goal_id,
                    note=args.get("check_note") or f"Check in on: {goal['title']}",
                )
                message_parts.append(f"Next check-in {describe(due, zone)}." + timing_note(due, zone))
            if not message_parts:
                return "Error: nothing to update (give fields to change or next_check)."
            return " ".join(message_parts)

        if action == "log":
            goals.log(goal_id, args.get("note"), kind="checkin")
            return f"Logged on {goal_id}."

        if action == "close":
            outcome = args.get("outcome") or "done"
            if outcome not in ("done", "dropped"):
                return "Error: outcome must be done or dropped."
            goals.set_status(goal_id, outcome, note=args.get("note") or "")
            agenda.cancel_for_objective(goal_id, reason=f"goal_{outcome}")
            return f"Closed {goal_id} as {outcome}. Its check-ins are cancelled."

        return f"Error: unknown action {action!r}."
