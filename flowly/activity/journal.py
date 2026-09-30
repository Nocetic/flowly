"""Reading the activity journal for an owner.

The stored lines say what happened; a few statuses are only true *now* and are
worked out at read time instead of written:

- a task this process is still running is ``running``, or ``waiting`` while its
  conversation waits on the owner;
- a task that started in an earlier boot and never ended is ``interrupted``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flowly.activity import store

# Ended in a way the owner should look at; these carry the unseen dot.
NOTEWORTHY = frozenset({"blocked", "failed", "waiting", "interrupted"})
TITLE_FALLBACK_MAX = 60
_KIND_ORDER = (("search", "research"), ("web", "research"), ("write", "files"), ("exec", "code"),
               ("mcp", "connection"), ("bot", "team"), ("agent", "team"), ("media", "media"))


def _status(task: dict[str, Any], running_ids: set[str], waiting_keys: set[str]) -> str:
    if task.get("ended"):
        return str(task.get("status") or "done")
    if task["id"] in running_ids:
        return "waiting" if task.get("sessionKey") in waiting_keys else "running"
    return "interrupted"


def _title(task: dict[str, Any]) -> str:
    recap = task.get("recap") or {}
    if recap.get("title"):
        return recap["title"]
    routine = (task.get("trigger") or {}).get("name")
    if isinstance(routine, str) and routine.strip():
        return routine.strip()
    request = (task.get("request") or "").strip().splitlines()
    first = request[0].strip() if request else ""
    if first:
        return first if len(first) <= TITLE_FALLBACK_MAX else first[: TITLE_FALLBACK_MAX - 1].rstrip() + "…"
    return task.get("conversationTitle") or ""


def _kind(task: dict[str, Any]) -> str:
    if (task.get("trigger") or {}).get("kind") == "routine":
        return "routine"
    kinds = {step.get("kind") for step in task.get("steps") or [] if isinstance(step, dict)}
    for step_kind, icon in _KIND_ORDER:
        if step_kind in kinds:
            return icon
    return "general"


def _summary(task: dict[str, Any], status: str, seen_before: int) -> dict[str, Any]:
    stamp = task.get("endedAt") or task.get("startedAt") or 0
    return {
        "id": task["id"],
        "title": _title(task),
        "outcome": (task.get("recap") or {}).get("outcome", ""),
        "status": status,
        "trigger": task.get("trigger") or {},
        "kind": _kind(task),
        "startedAt": task.get("startedAt"),
        "endedAt": task.get("endedAt"),
        "sessionKey": task.get("sessionKey", ""),
        "conversationTitle": task.get("conversationTitle", ""),
        "summarized": bool(task.get("recap")),
        "unseen": status in NOTEWORTHY and isinstance(stamp, int) and stamp > seen_before,
    }


def list_tasks(
    *,
    limit: int,
    before: int | None,
    visible: Callable[[str], bool],
    running_ids: set[str],
    waiting_keys: set[str],
) -> dict[str, Any]:
    tasks, seen_before = store.load()
    ordered = sorted(
        (task for task in tasks.values()
         if isinstance(task.get("startedAt"), int) and not task.get("discarded")),
        key=lambda task: task["startedAt"],
        reverse=True,
    )
    items: list[dict[str, Any]] = []
    for task in ordered:
        if before is not None and task["startedAt"] >= before:
            continue
        if not visible(str(task.get("sessionKey") or "")):
            continue
        if len(items) == limit:
            return {"items": items, "nextBefore": items[-1]["startedAt"], "seenBefore": seen_before}
        items.append(_summary(task, _status(task, running_ids, waiting_keys), seen_before))
    return {"items": items, "nextBefore": None, "seenBefore": seen_before}


def get_task(
    task_id: str,
    *,
    visible: Callable[[str], bool],
    running_ids: set[str],
    waiting_keys: set[str],
) -> dict[str, Any] | None:
    tasks, seen_before = store.load()
    task = tasks.get(task_id)
    if (task is None or task.get("discarded") or not isinstance(task.get("startedAt"), int)
            or not visible(str(task.get("sessionKey") or ""))):
        return None
    status = _status(task, running_ids, waiting_keys)
    recap = task.get("recap") or {}
    notes = {item["i"]: item["note"] for item in recap.get("steps") or []
             if isinstance(item, dict) and isinstance(item.get("i"), int) and isinstance(item.get("note"), str)}
    steps = [
        {**{key: step.get(key) for key in ("tool", "kind", "target", "ok", "durationMs")},
         "blocked": bool(step.get("blocked")),
         **({"note": notes[index]} if index in notes else {})}
        for index, step in enumerate(task.get("steps") or []) if isinstance(step, dict)
    ]
    return {
        **_summary(task, status, seen_before),
        "summary": recap.get("summary", ""),
        "request": task.get("request", ""),
        "activeMs": task.get("activeMs"),
        "steps": steps,
        "prompts": task.get("prompts") or [],
        "model": task.get("model", ""),
        "tokens": task.get("tokens") or {},
        "recapTokens": task.get("recapTokens") or {},
        **({"error": task["error"]} if task.get("error") else {}),
    }


def mark_seen(before: int) -> int:
    """Record that the owner has seen everything up to ``before``; returns the cursor."""
    _tasks, seen_before = store.load()
    if before > seen_before:
        store.append({"type": "seen", "before": before}, at_ms=before)
        return before
    return seen_before
