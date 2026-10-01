"""Reading the activity journal for an owner.

The journal stores **turns**; the owner reads **tasks**. A task is one turn,
or several that are one piece of work: a goal's turns, or a follow-up the
model judged to carry on earlier work (``recap.py``). Turns are put together
here, at read time, by the ``taskId`` each one names.

Which turns are shown at all:

- a routine's or a goal's: always;
- otherwise, a turn that did work by rule (``"work": true``), unless the
  model, writing its summary, judged it was only conversation;
- a candidate without such work (a long reply, a channel message) only once
  the model judged it work.

Lines written before tasks and conversation were told apart carry no
``work``; the same rule is worked out from their steps, so the old journal
reads the new way without being rewritten.

A few statuses are only true *now* and are worked out at read time instead
of written:

- a task this process is still working on is ``running``, or ``waiting``
  while its conversation waits on the owner;
- a turn that started doing work in an earlier boot and never ended is
  ``interrupted``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flowly.activity import store
from flowly.activity.recorder import BACKGROUND_TRIGGERS
from flowly.activity.steps import TASK_KINDS, is_work, task_kind_of

# Ended in a way the owner should look at; these carry the unseen dot.
NOTEWORTHY = frozenset({"blocked", "failed", "waiting", "interrupted"})
# A follow-up this soon after earlier work in the same conversation may carry
# it on; the model decides whether it does.
CONTINUATION_WINDOW_MS = 30 * 60_000
# The owner's request as a list shows it, while the model's title is not in.
REQUEST_PREVIEW_CHARS = 140
_STEP_FIELDS = ("tool", "kind", "target")


# ── which turns are work ────────────────────────────────────────────────────


def _trigger_kind(turn: dict[str, Any]) -> str:
    return str((turn.get("trigger") or {}).get("kind") or "")


def _did_work(turn: dict[str, Any]) -> bool:
    """Work by rule. A line from before the rule existed is judged by its steps."""
    if isinstance(turn.get("work"), bool):
        return turn["work"]
    return any(is_work(step) for step in turn.get("steps") or [] if isinstance(step, dict))


def _shown(turn: dict[str, Any]) -> bool:
    if turn.get("discarded") or not isinstance(turn.get("startedAt"), int):
        return False
    if _trigger_kind(turn) in BACKGROUND_TRIGGERS:
        return True
    verdict = turn.get("verdict")
    if verdict is False:
        return False
    return _did_work(turn) or verdict is True


# ── putting turns together ──────────────────────────────────────────────────


@dataclass(frozen=True)
class _Task:
    id: str
    turns: tuple[dict[str, Any], ...]  # oldest first

    @property
    def first(self) -> dict[str, Any]:
        return self.turns[0]

    @property
    def last(self) -> dict[str, Any]:
        return self.turns[-1]

    @property
    def started_at(self) -> int:
        return self.first["startedAt"]

    @property
    def recap(self) -> dict[str, Any]:
        # The newest summary speaks for the whole task: a turn that carried
        # it on was summarized with the task so far in view.
        for turn in reversed(self.turns):
            if isinstance(turn.get("recap"), dict):
                return turn["recap"]
        return {}


# Holds the merged view it was built from, compared by identity: the store
# hands out the same object until a month file changes.
_grouped: tuple[dict[str, dict[str, Any]], dict[str, _Task], dict[str, str]] | None = None


def _tasks() -> tuple[dict[str, _Task], dict[str, str], int]:
    """``({task id: task}, {turn id: task id}, seen_before)``, reused until the journal changes."""
    global _grouped
    turns, seen_before = store.load()
    if _grouped is not None and _grouped[0] is turns:
        return _grouped[1], _grouped[2], seen_before
    members: dict[str, list[dict[str, Any]]] = {}
    for turn in turns.values():
        if _shown(turn):
            members.setdefault(str(turn.get("taskId") or turn["id"]), []).append(turn)
    tasks: dict[str, _Task] = {}
    owner_of: dict[str, str] = {}
    for task_id, group in members.items():
        group.sort(key=lambda turn: turn["startedAt"])
        tasks[task_id] = _Task(task_id, tuple(group))
        for turn in group:
            owner_of[turn["id"]] = task_id
    _grouped = (turns, tasks, owner_of)
    return tasks, owner_of, seen_before


# ── what a task says ────────────────────────────────────────────────────────


def _turn_status(turn: dict[str, Any], running_ids: set[str], waiting_keys: set[str]) -> str:
    if turn.get("ended"):
        return str(turn.get("status") or "done")
    if turn["id"] in running_ids:
        return "waiting" if turn.get("sessionKey") in waiting_keys else "running"
    return "interrupted"


def _status(task: _Task, running_ids: set[str], waiting_keys: set[str]) -> str:
    for turn in reversed(task.turns):
        if turn["id"] in running_ids:
            return _turn_status(turn, running_ids, waiting_keys)
    return _turn_status(task.last, running_ids, waiting_keys)


def _title(task: _Task) -> str:
    """The model's title, else the routine's name, else nothing.

    Never the owner's own message: with no title the apps show the
    conversation's title or their word for an untitled task."""
    if task.recap.get("title"):
        return task.recap["title"]
    routine = (task.first.get("trigger") or {}).get("name")
    return routine.strip() if isinstance(routine, str) else ""


def _steps(task: _Task) -> list[dict[str, Any]]:
    return [step for turn in task.turns for step in turn.get("steps") or [] if isinstance(step, dict)]


def _kind(task: _Task) -> str:
    """The task's icon: a routine or a goal says so; otherwise the most
    consequential kind of work it did (``steps.TASK_KINDS``)."""
    trigger = _trigger_kind(task.first)
    if trigger in BACKGROUND_TRIGGERS:
        return trigger
    found = {task_kind_of(step) for step in _steps(task)}
    return next((kind for kind in TASK_KINDS if kind in found), "general")


def _latest_step(task: _Task, live_steps: dict[str, dict[str, Any]]) -> dict[str, str] | None:
    """What it is doing now, if it runs; else the last thing it did."""
    for turn in reversed(task.turns):
        step = live_steps.get(turn["id"])
        if step is None:
            recorded = [step for step in turn.get("steps") or [] if isinstance(step, dict)]
            step = recorded[-1] if recorded else None
        if step is not None:
            return {key: str(step.get(key) or "") for key in _STEP_FIELDS}
    return None


def _preview(request: Any) -> str:
    text = " ".join(request.split()) if isinstance(request, str) else ""
    return text if len(text) <= REQUEST_PREVIEW_CHARS else text[: REQUEST_PREVIEW_CHARS - 1].rstrip() + "…"


def _ended_at(task: _Task) -> int | None:
    return task.last.get("endedAt") if task.last.get("ended") else None


def _summary(task: _Task, status: str, seen_before: int,
             live_steps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    stamp = task.last.get("endedAt") or task.last.get("startedAt") or 0
    titled = next((turn.get("conversationTitle") for turn in reversed(task.turns)
                   if turn.get("conversationTitle")), "")
    return {
        "id": task.id,
        "title": _title(task),
        "outcome": task.recap.get("outcome", ""),
        "status": status,
        "trigger": task.first.get("trigger") or {},
        "kind": _kind(task),
        "startedAt": task.started_at,
        "endedAt": _ended_at(task),
        "sessionKey": task.first.get("sessionKey", ""),
        "conversationTitle": titled,
        "summarized": bool(task.recap),
        "unseen": status in NOTEWORTHY and isinstance(stamp, int) and stamp > seen_before,
        # Until the model's title is in, the apps show what was asked
        # (marked as a placeholder) and what it is doing.
        "request": _preview(task.first.get("request")),
        "latestStep": _latest_step(task, live_steps or {}),
    }


def _sum_tokens(values: list[Any]) -> dict[str, int]:
    total: dict[str, int] = {}
    for value in values:
        if isinstance(value, dict):
            for key in ("input", "output"):
                if isinstance(value.get(key), int):
                    total[key] = total.get(key, 0) + value[key]
    return total


# ── the reads ───────────────────────────────────────────────────────────────


def list_tasks(
    *,
    limit: int,
    before: int | None,
    visible: Callable[[str], bool],
    running_ids: set[str],
    waiting_keys: set[str],
    live_steps: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Newest first, by when each task started; ``before`` pages back from that."""
    tasks, _owner_of, seen_before = _tasks()
    items: list[dict[str, Any]] = []
    for task in sorted(tasks.values(), key=lambda task: task.started_at, reverse=True):
        if before is not None and task.started_at >= before:
            continue
        if not visible(str(task.first.get("sessionKey") or "")):
            continue
        if len(items) == limit:
            return {"items": items, "nextBefore": items[-1]["startedAt"], "seenBefore": seen_before}
        items.append(_summary(task, _status(task, running_ids, waiting_keys), seen_before, live_steps))
    return {"items": items, "nextBefore": None, "seenBefore": seen_before}


def get_task(
    task_id: str,
    *,
    visible: Callable[[str], bool],
    running_ids: set[str],
    waiting_keys: set[str],
    live_steps: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """One task in full. A turn's id finds the task it became part of."""
    tasks, owner_of, seen_before = _tasks()
    task = tasks.get(task_id) or tasks.get(owner_of.get(task_id, ""))
    if task is None or not visible(str(task.first.get("sessionKey") or "")):
        return None
    status = _status(task, running_ids, waiting_keys)
    steps: list[dict[str, Any]] = []
    for turn in task.turns:
        recap = turn.get("recap") if isinstance(turn.get("recap"), dict) else {}
        notes = {item["i"]: item["note"] for item in recap.get("steps") or []
                 if isinstance(item, dict) and isinstance(item.get("i"), int) and isinstance(item.get("note"), str)}
        steps.extend(
            {**{key: step.get(key) for key in ("tool", "kind", "target", "ok", "durationMs")},
             "blocked": bool(step.get("blocked")),
             **({"note": notes[index]} if index in notes else {})}
            for index, step in enumerate(turn.get("steps") or []) if isinstance(step, dict)
        )
    failed = task.last if task.last.get("error") else {}
    active = [turn.get("activeMs") for turn in task.turns if isinstance(turn.get("activeMs"), int)]
    return {
        **_summary(task, status, seen_before, live_steps),
        "summary": task.recap.get("summary", ""),
        "request": task.first.get("request", ""),
        "activeMs": sum(active) if active else None,
        "steps": steps,
        "prompts": [prompt for turn in task.turns for prompt in turn.get("prompts") or []],
        "model": next((turn["model"] for turn in reversed(task.turns) if turn.get("model")), ""),
        "tokens": _sum_tokens([turn.get("tokens") for turn in task.turns]),
        "recapTokens": _sum_tokens([turn.get("recapTokens") for turn in task.turns]),
        **({"error": failed["error"]} if failed else {}),
    }


def earlier_work(session_key: str, *, turn_id: str, started_at: int) -> dict[str, Any] | None:
    """The task a finished turn might carry on, for the model to judge.

    The latest shown task of the same conversation that ended within
    ``CONTINUATION_WINDOW_MS`` before the turn started. A routine's or a
    goal's task is never carried on by a conversation: each routine run is
    its own task, and a goal groups its own turns."""
    tasks, owner_of, _seen = _tasks()
    own = owner_of.get(turn_id)
    best: _Task | None = None
    for task in tasks.values():
        if task.id == own or task.first.get("sessionKey") != session_key:
            continue
        if _trigger_kind(task.first) in BACKGROUND_TRIGGERS:
            continue
        ended = _ended_at(task)
        if not isinstance(ended, int) or not 0 <= started_at - ended <= CONTINUATION_WINDOW_MS:
            continue
        if best is None or (ended, task.started_at) > (_ended_at(best) or 0, best.started_at):
            best = task
    if best is None:
        return None
    return {
        "id": best.id,
        "request": best.first.get("request", ""),
        "title": best.recap.get("title", ""),
        "outcome": best.recap.get("outcome", ""),
        "summary": best.recap.get("summary", ""),
    }


def task_so_far(task_id: str, *, turn_id: str) -> dict[str, Any] | None:
    """A task's own words so far, without the given turn: what a goal's next
    turn is summarized against."""
    tasks, _owner_of, _seen = _tasks()
    task = tasks.get(task_id)
    if task is None:
        return None
    earlier = tuple(turn for turn in task.turns if turn["id"] != turn_id)
    if not earlier:
        return None
    so_far = _Task(task.id, earlier)
    return {"id": task.id, "request": so_far.first.get("request", ""), "title": so_far.recap.get("title", ""),
            "outcome": so_far.recap.get("outcome", ""), "summary": so_far.recap.get("summary", "")}


def mark_seen(before: int) -> int:
    """Record that the owner has seen everything up to ``before``; returns the cursor."""
    _tasks_by_id, _owner_of, seen_before = _tasks()
    if before > seen_before:
        store.append({"type": "seen", "before": before}, at_ms=before)
        return before
    return seen_before
