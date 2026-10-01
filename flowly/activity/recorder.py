"""Record the work the agent does, from the turn itself.

Activity lists **work**, not conversation. A turn is watched from the moment
the conversation's turn lock is taken (time queued behind an earlier turn is
not the turn's) to the turn's ``finally``, whatever the outcome. Time is the
process's monotonic clock; nothing depends on streams, clients or the network.

A turn becomes work, and only then reaches the disk, when:

- a routine or a goal started it (known before it begins); or
- it takes its first step that is work (``steps.is_work``): looking things
  up, acting in an app, writing a file, running a program, asking another
  agent. Recalling its own memory, asking the owner or drafting a plan is
  part of talking and does not count.

That moment writes the ``start`` line, so work cut short by a crash is still
known and reads as interrupted. A turn that never did work writes nothing at
all: a conversation leaves no trace in Activity.

A turn without such a step that wrote a long reply (a cover letter, a plan)
is a *candidate*: its record is written at the end, for the bot's model to
judge when it writes the summary (``recap.py``), and it shows only if the
model calls it work. A message from a channel follows the same rules as one
from the app: for many owners Telegram is simply where they talk to their
agent, and a greeting there is no more a task than in the app.

*Active time* excludes time the turn spent parked on an approval or a
question: a turn that waited ten minutes for a yes and then said one sentence
did not work for ten minutes.

A task can span several turns. A goal's turns are one task from the start
(``task_id_for``); a follow-up the model judges to continue earlier work is
joined to it later, by the summary. See ``journal.py`` for how turns are put
together.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from flowly.activity import store
from flowly.activity.steps import describe_step, is_work

# Identifies this process. A ``start`` from another boot that never ended was
# cut short by a crash, a kill or an update.
BOOT_ID = uuid.uuid4().hex

# Host-only orchestration and background housekeeping are not the owner's
# tasks: the Board, groups and helpers keep their own history.
INTERNAL_SESSION_PREFIXES = (
    "desktop:profile-inbox:",
    "desktop:profile-room:",
    "desktop:profile-task:",
    "heartbeat:",
    "subagent:",
)

# The cron runner names the routine a turn belongs to under this metadata key
# (``{"name": …}``), so the task reads "Morning brief", not a job id.
ROUTINE_METADATA_KEY = "_activity_routine"
ROUTINE_NAME_MAX_CHARS = 80

# A reply at least this long is a candidate even without a tool: a piece of
# writing is work. Length, not time, because time says more about how fast
# the model is than about what it made.
LONG_REPLY_CHARS = 1_500
# Started without the owner being in the conversation: always a task.
BACKGROUND_TRIGGERS = frozenset({"routine", "goal"})
REQUEST_MAX_CHARS = 280
STEP_EXCERPT_CHARS = 1_500
MAX_STEPS = 200
SUBJECT_MAX_CHARS = 80

_APPROVAL_DECISIONS = {"allow-once": "allowed", "allow-always": "allowed", "deny": "denied",
                       "timeout": "timeout", "cancelled": "cancelled"}
_CLARIFY_DECISIONS = {"answered": "answered", "timeout": "timeout", "cancelled": "cancelled"}


def _one_line(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tokens(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    def pick(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return 0
    tokens = {"input": pick("prompt_tokens", "input_tokens"), "output": pick("completion_tokens", "output_tokens")}
    return tokens if any(tokens.values()) else {}


def task_id_for(turn_id: str, trigger: dict[str, Any]) -> str:
    """The task a turn belongs to when it starts: a goal's turns share one."""
    goal = trigger.get("goalId") if trigger.get("kind") == "goal" else None
    if isinstance(goal, str) and goal.strip():
        return f"goal:{goal.strip()[:100]}"
    return turn_id


@dataclass
class ActiveTask:
    id: str
    session_key: str
    trigger: dict[str, Any]
    request: str
    started_at: int
    started_mono: float
    task_id: str = ""
    # Did work, so its ``start`` line is on disk.
    working: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)
    prompts: dict[str, dict[str, Any]] = field(default_factory=dict)
    waited: float = 0.0
    # A prompt closed with no (deny/timeout) since the last step: the next
    # failed step was blocked, not broken.
    refused_since_step: bool = False


@dataclass
class EndedTask:
    record: dict[str, Any]
    excerpts: list[str]


class ActivityRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, ActiveTask] = {}

    # ── lifecycle ────────────────────────────────────────────────────────

    @staticmethod
    def records(session_key: str) -> bool:
        return bool(session_key) and not session_key.startswith(INTERNAL_SESSION_PREFIXES)

    def begin(self, *, session_key: str, task_id: str, trigger: dict[str, Any], request: str) -> ActiveTask | None:
        if not self.records(session_key):
            return None
        turn_id = task_id or uuid.uuid4().hex
        task = ActiveTask(
            id=turn_id,
            session_key=session_key,
            trigger=dict(trigger),
            request=_one_line(request, REQUEST_MAX_CHARS),
            started_at=int(time.time() * 1000),
            started_mono=time.monotonic(),
            task_id=task_id_for(turn_id, trigger),
        )
        with self._lock:
            self._active[session_key] = task
            starts = self._mark_working(task) if task.trigger.get("kind") in BACKGROUND_TRIGGERS else None
        if starts is not None:
            store.append(starts, at_ms=task.started_at)
        return task

    @staticmethod
    def _mark_working(task: ActiveTask) -> dict[str, Any] | None:
        """The ``start`` line to write, the first time the turn does work. Under the lock."""
        if task.working:
            return None
        task.working = True
        return {
            "type": "start", "id": task.id, "taskId": task.task_id, "sessionKey": task.session_key,
            "trigger": task.trigger, "request": task.request, "startedAt": task.started_at,
            "work": True, "boot": BOOT_ID,
        }

    def active(self, session_key: str) -> ActiveTask | None:
        with self._lock:
            return self._active.get(session_key)

    def active_ids(self) -> set[str]:
        """Turns running now that are work; a conversation is never "running" in Activity."""
        with self._lock:
            return {task.id for task in self._active.values() if task.working}

    def live_steps(self) -> dict[str, list[dict[str, Any]]]:
        """``{turn id: its steps so far}`` for work running now.

        A turn's steps reach the disk only when it ends; until then this is how
        a reader sees what a running task has done and is doing."""
        with self._lock:
            return {task.id: [dict(step) for step in task.steps] for task in self._active.values()
                    if task.working and task.steps}

    def end(
        self,
        task: ActiveTask | None,
        *,
        outcome: str,
        usage: Any = None,
        model: str = "",
        error: str = "",
        conversation_title: str = "",
        reply_chars: int = 0,
    ) -> EndedTask | None:
        if task is None:
            return None
        with self._lock:
            if self._active.get(task.session_key) is task:
                del self._active[task.session_key]
            waited = task.waited + sum(time.monotonic() - prompt["opened"]
                                       for prompt in task.prompts.values() if "decision" not in prompt)
            working = task.working
        active_ms = max(0, int((time.monotonic() - task.started_mono - waited) * 1000))
        if outcome == "silent" and not working:
            # The agent chose not to answer (a passive group message, a
            # dropped dispatch) and did no work: nothing to record.
            return None
        if not working and max(0, int(reply_chars or 0)) < LONG_REPLY_CHARS:
            # A conversation. Nothing was written for it and nothing is now.
            return None
        last = task.steps[-1] if task.steps else None
        if outcome == "aborted":
            status = "stopped"
        elif outcome == "error":
            status = "failed"
        elif last is not None and last.get("blocked"):
            status = "blocked"
        else:
            status = "done"
        record = {
            "id": task.id,
            "taskId": task.task_id,
            "sessionKey": task.session_key,
            "conversationTitle": _one_line(conversation_title, 120),
            "trigger": task.trigger,
            "request": task.request,
            "startedAt": task.started_at,
            "endedAt": int(time.time() * 1000),
            "activeMs": active_ms,
            "status": status,
            # Work by rule; a candidate without it is the model's to judge.
            "work": working,
            "steps": task.steps,
            "prompts": [
                {"kind": prompt["kind"], "subject": prompt["subject"], "decision": prompt.get("decision", "open")}
                for prompt in task.prompts.values()
            ],
            "model": _one_line(model, 120),
            "tokens": _tokens(usage),
            **({"error": _one_line(error, 240)} if status == "failed" and error else {}),
        }
        store.append({"type": "task", **record}, at_ms=task.started_at)
        return EndedTask(record=record, excerpts=task.excerpts)

    # ── steps ────────────────────────────────────────────────────────────

    def note_tool(self, session_key: str, tool_name: str, args: Any, *, ok: bool,
                  duration_ms: int, result: Any = None) -> None:
        with self._lock:
            task = self._active.get(session_key)
            if task is None or len(task.steps) >= MAX_STEPS:
                return
            step = describe_step(tool_name, args)
            step.update({"ok": bool(ok), "durationMs": max(0, int(duration_ms or 0))})
            if not ok and task.refused_since_step:
                step["blocked"] = True
            task.refused_since_step = False
            task.steps.append(step)
            # Kept in memory for the summary only; never written to disk.
            task.excerpts.append(result[:STEP_EXCERPT_CHARS] if isinstance(result, str) else "")
            starts = self._mark_working(task) if is_work(step) else None
        if starts is not None:
            store.append(starts, at_ms=task.started_at)

    # ── prompts (wired to the approval and clarify managers) ─────────────

    def _open(self, session_key: str | None, prompt_id: str, kind: str, subject: str) -> None:
        with self._lock:
            task = self._active.get(session_key or "")
            if task is not None and prompt_id and prompt_id not in task.prompts:
                task.prompts[prompt_id] = {"kind": kind, "subject": _one_line(subject, SUBJECT_MAX_CHARS),
                                           "opened": time.monotonic()}

    def _close(self, session_key: str | None, prompt_id: str, decision: str) -> None:
        with self._lock:
            task = self._active.get(session_key or "")
            prompt = task.prompts.get(prompt_id) if task is not None else None
            if prompt is None or "decision" in prompt:
                return
            prompt["decision"] = decision
            task.waited += time.monotonic() - prompt["opened"]
            if decision in {"denied", "timeout"}:
                task.refused_since_step = True

    async def on_approval_requested(self, pending: Any) -> None:
        kind = getattr(pending, "kind", "action")
        command = getattr(getattr(pending, "request", None), "command", "")
        # A tool action is a sentence for people; a shell or Codex command is
        # reduced to its program, like a step's target.
        subject = command if kind == "action" else describe_step("exec", {"command": command})["target"]
        self._open(getattr(pending, "session_key", None), getattr(pending, "id", ""), "approval", subject)

    async def on_approval_closed(self, approval_id: str, reason: str, session_key: str) -> None:
        self._close(session_key, approval_id, _APPROVAL_DECISIONS.get(reason, "closed"))

    async def on_clarify_requested(self, pending: Any) -> None:
        self._open(getattr(pending, "session_key", None), getattr(pending, "id", ""), "question",
                   getattr(pending, "question", ""))

    async def on_clarify_closed(self, clarify_id: str, reason: str, session_key: str) -> None:
        self._close(session_key, clarify_id, _CLARIFY_DECISIONS.get(reason, "closed"))


_recorder: ActivityRecorder | None = None
_attached = False


def get_activity_recorder() -> ActivityRecorder:
    """The process's recorder, wired to the approval and clarify managers once."""
    global _recorder, _attached
    if _recorder is None:
        _recorder = ActivityRecorder()
    if not _attached:
        try:
            from flowly.clarify.manager import get_clarify_manager
            from flowly.exec.approval_manager import get_approval_manager

            approvals, questions = get_approval_manager(), get_clarify_manager()
            approvals.add_notify_callback(_recorder.on_approval_requested)
            approvals.add_close_callback(_recorder.on_approval_closed)
            questions.add_notify_callback(_recorder.on_clarify_requested)
            questions.add_close_callback(_recorder.on_clarify_closed)
            _attached = True
        except Exception:  # noqa: BLE001 — prompts are extra detail, never required
            _attached = True
    return _recorder
