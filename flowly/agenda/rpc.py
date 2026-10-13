"""Owner-facing feature RPCs for tracked goals and the agent's agenda.

Served by every agent runtime (the main gateway and each named agent, through
the profile host), over the same ``feature_rpc`` surface Desktop and the
phone apps share:

- ``objectives.list`` — the goals, each with its next planned check-in.
- ``objectives.update`` — the owner pauses, resumes, finishes, drops or
  deletes a goal, ticks a plan step (``step``, ``done``) or renames it
  (``title``). Anything that ends or pauses a goal cancels its check-in;
  a tick or a rename is logged so the agent reads it on its next check-in.
- ``agenda.list`` — what the agent plans to look at again, and what came of
  its recent wake-ups.
- ``agenda.settings.get`` / ``agenda.settings.set`` — how often the agent may
  write first, quiet hours, timezone, daily review time.

Errors are :class:`~flowly.objectives.store.ObjectiveError`; the dispatcher
maps their ``code`` to the RPC error code.
"""

from __future__ import annotations

from typing import Any

from flowly.agenda import policy
from flowly.agenda.store import get_agenda_store
from flowly.objectives.store import ObjectiveError, get_objective_store

_OWNER_ACTIONS = {"pause": "paused", "resume": "active", "done": "done", "drop": "dropped"}
_PUBLIC_ITEM_FIELDS = (
    "id", "kind", "dueAt", "note", "sessionKey", "objectiveId", "createdAt",
    "deferredReason", "outcome", "reason", "settledAt", "preview",
)


def _goal_id(params: dict[str, Any]) -> str:
    value = params.get("id")
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ObjectiveError("INVALID", "A goal id is required")
    return value.strip()


def objectives_list(params: dict[str, Any]) -> dict[str, Any]:
    include_closed = params.get("includeClosed", True) is not False
    agenda = get_agenda_store()
    checks = {i["objectiveId"]: int(i["dueAt"]) for i in agenda.pending() if i.get("objectiveId")}
    goals = get_objective_store().list(include_closed=include_closed)
    for goal in goals:
        goal["nextCheckAt"] = checks.get(goal["id"])
    return {"objectives": goals, "settings": agenda.settings()}


def objectives_update(params: dict[str, Any]) -> dict[str, Any]:
    goal_id = _goal_id(params)
    action = str(params.get("action") or "").strip().lower()
    goals = get_objective_store()
    agenda = get_agenda_store()
    if action == "delete":
        removed = goals.delete(goal_id)
        if not removed:
            raise ObjectiveError("NOT_FOUND", "No such goal")
        agenda.cancel_for_objective(goal_id, reason="owner_deleted")
        return {"ok": True, "deleted": goal_id}
    if action == "step":
        goal = goals.set_step(goal_id, params.get("step"), params.get("done"))
        goal["nextCheckAt"] = (agenda.next_for_objective(goal_id) or {}).get("dueAt")
        return {"ok": True, "objective": goal}
    if action == "rename":
        goal = goals.update(goal_id, {"title": params.get("title")})
        goals.log(goal_id, goal["title"], kind="owner", event="renamed")
        goal = goals.get(goal_id) or goal
        goal["nextCheckAt"] = (agenda.next_for_objective(goal_id) or {}).get("dueAt")
        return {"ok": True, "objective": goal}
    status = _OWNER_ACTIONS.get(action)
    if status is None:
        raise ObjectiveError("INVALID", "action must be pause, resume, done, drop, delete, step or rename")
    goal = goals.set_status(goal_id, status, by_owner=True)
    if status != "active":
        agenda.cancel_for_objective(goal_id, reason=f"owner_{action}")
    goal["nextCheckAt"] = None if status != "active" else (
        (agenda.next_for_objective(goal_id) or {}).get("dueAt")
    )
    return {"ok": True, "objective": goal}


def agenda_list(params: dict[str, Any]) -> dict[str, Any]:
    snapshot = get_agenda_store().snapshot()
    limit = params.get("historyLimit", 20)
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and 0 <= limit <= 100 else 20

    def public(item: dict[str, Any]) -> dict[str, Any]:
        return {key: item[key] for key in _PUBLIC_ITEM_FIELDS if key in item}

    return {
        "pending": [public(i) for i in sorted(snapshot["items"], key=lambda i: int(i.get("dueAt") or 0))],
        "history": [public(i) for i in reversed(snapshot["history"][-limit:])] if limit else [],
        "settings": snapshot["settings"],
    }


def agenda_settings_get(_params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "settings": get_agenda_store().settings(),
        "levels": list(policy.LEVELS),
        "dailyCaps": dict(policy.DAILY_CAP_BY_LEVEL),
        "globalDailyCap": policy.GLOBAL_DAILY_CAP,
    }


def agenda_settings_set(params: dict[str, Any]) -> dict[str, Any]:
    allowed = {"level", "quietStart", "quietEnd", "timezone", "reviewTime"}
    unknown = set(params) - allowed - {"expectedBotId"}
    if unknown:
        raise ObjectiveError("INVALID", f"Unknown setting: {', '.join(sorted(unknown))}")
    changes = {key: value for key, value in params.items() if key in allowed}
    return {"settings": get_agenda_store().update_settings(changes)}
