"""Durable record of the goals an agent tracks for its owner.

One JSON file per agent home (``<home>/objectives/objectives.json``), written
under a file lock with an atomic replace, so the gateway, its feature RPCs and
a CLI never interleave a half-written record. The file holds only what the goal
IS (title, plan, stage, progress, a short log). When the agent next looks at
it lives in the agenda (``flowly/agenda``), the one place that decides when
the agent wakes; ``objectives.list`` joins the two for clients.

Everything written here is read back into the agent's prompt on a later
wake-up, so every text field is bounded and passes the same injection scan as
a scheduled task's prompt (:mod:`flowly.cron.guard`).
"""

from __future__ import annotations

import copy
import json
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from filelock import FileLock, Timeout

from flowly.profile import _atomic_write_json, get_flowly_home

SCHEMA_VERSION = 1

CATEGORIES = ("health", "relationships", "finance", "career", "interests", "productivity", "other")
STATUSES = ("active", "paused", "done", "dropped")
OPEN_STATUSES = frozenset({"active", "paused"})
LOG_KINDS = ("note", "checkin", "plan", "status")

#: Open goals one agent may track at once; more is a list, not a focus.
MAX_ACTIVE_OBJECTIVES = 20
#: Closed goals kept for history; the oldest closed ones go first.
MAX_STORED_OBJECTIVES = 200
MAX_TITLE = 120
MAX_WHY = 1_000
MAX_STEP = 300
MAX_STEPS = 20
MAX_STAGE = 200
MAX_CADENCE = 120
MAX_UNIT = 24
MAX_LOG_TEXT = 500
MAX_LOG_ENTRIES = 50
MAX_SESSION_KEY = 512

T = TypeVar("T")


class ObjectiveError(ValueError):
    """A rejected request; ``code`` is the stable RPC error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now_ms() -> int:
    return int(time.time() * 1000)


def clean_text(value: Any, *, limit: int, field: str, required: bool = False, multiline: bool = False) -> str:
    """A bounded, printable, injection-free string."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ObjectiveError("INVALID", f"{field} must be text")
    text = value.strip()
    if not multiline:
        text = " ".join(text.split())
    if required and not text:
        raise ObjectiveError("INVALID", f"{field} is required")
    if len(text) > limit:
        raise ObjectiveError("INVALID", f"{field} exceeds {limit} characters")
    if any((ord(ch) < 32 and ch not in "\n\t") or ord(ch) == 127 for ch in text):
        raise ObjectiveError("INVALID", f"{field} contains control characters")
    if text:
        from flowly.cron.guard import scan_cron_prompt

        threat = scan_cron_prompt(text)
        if threat:
            raise ObjectiveError("INVALID", f"{field}: {threat}")
    return text


def clean_session_key(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > MAX_SESSION_KEY:
        raise ObjectiveError("INVALID", "conversation must be a session key")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ObjectiveError("INVALID", "conversation must be a session key")
    return value.strip()


def _clean_category(value: Any) -> str:
    category = str(value or "other").strip().lower()
    if category not in CATEGORIES:
        raise ObjectiveError("INVALID", f"category must be one of {', '.join(CATEGORIES)}")
    return category


def _clean_plan(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ObjectiveError("INVALID", "plan must be a list of steps")
    if len(value) > MAX_STEPS:
        raise ObjectiveError("INVALID", f"plan exceeds {MAX_STEPS} steps")
    steps = []
    for item in value:
        if isinstance(item, str):
            text, done = item, False
        elif isinstance(item, dict):
            text, done = item.get("text"), item.get("done", False)
        else:
            raise ObjectiveError("INVALID", "each plan step must be text")
        if not isinstance(done, bool):
            raise ObjectiveError("INVALID", "a step's done flag must be true or false")
        steps.append({"text": clean_text(text, limit=MAX_STEP, field="plan step", required=True), "done": done})
    return steps


def _clean_number(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObjectiveError("INVALID", f"{field} must be a number")
    number = float(value)
    if number != number or abs(number) > 1e12:
        raise ObjectiveError("INVALID", f"{field} is out of range")
    return int(number) if number.is_integer() else number


def _clean_time(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ObjectiveError("INVALID", f"{field} must be a time in milliseconds")
    return value


class ObjectiveStore:
    """The tracked goals of one agent home."""

    def __init__(self, home: Path, *, lock_timeout: float = 10.0):
        self.path = Path(home) / "objectives" / "objectives.json"
        self._lock = FileLock(str(self.path.with_suffix(".lock")))
        self._lock_timeout = lock_timeout

    # ── persistence ──────────────────────────────────────────────────

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock.acquire(timeout=self._lock_timeout):
                yield
        except Timeout as exc:
            raise ObjectiveError("BUSY", "Goals are busy. Retry.") from exc

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": SCHEMA_VERSION, "revision": 0, "objectives": []}
        except (OSError, ValueError) as exc:
            raise ObjectiveError("CORRUPT", f"Goals file is unreadable: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("objectives"), list):
            raise ObjectiveError("CORRUPT", "Goals file has an unknown shape")
        raw.setdefault("revision", 0)
        return raw

    def _transaction(self, mutate: Callable[[dict[str, Any]], T]) -> T:
        with self._locked():
            data = self._read()
            result = mutate(data)
            data["version"] = SCHEMA_VERSION
            data["revision"] = int(data.get("revision") or 0) + 1
            _prune(data["objectives"])
            _atomic_write_json(self.path, data)
            return result

    # ── reads ────────────────────────────────────────────────────────

    def revision(self) -> int:
        with self._locked():
            return int(self._read().get("revision") or 0)

    def list(self, *, include_closed: bool = True) -> list[dict[str, Any]]:
        with self._locked():
            items = self._read()["objectives"]
        rows = [copy.deepcopy(item) for item in items if include_closed or item.get("status") in OPEN_STATUSES]
        rows.sort(key=lambda item: (item.get("status") not in OPEN_STATUSES, -int(item.get("updatedAt") or 0)))
        return rows

    def get(self, objective_id: str) -> dict[str, Any] | None:
        with self._locked():
            found = _find(self._read()["objectives"], objective_id)
        return copy.deepcopy(found) if found else None

    # ── writes ───────────────────────────────────────────────────────

    def create(
        self,
        *,
        title: Any,
        category: Any = "other",
        why: Any = "",
        plan: Any = None,
        stage: Any = "",
        cadence: Any = "",
        target: Any = None,
        unit: Any = "",
        ends_at: Any = None,
        session_key: Any = "",
        created_by: str = "agent",
    ) -> dict[str, Any]:
        now = _now_ms()
        record = {
            "id": f"obj_{secrets.token_hex(6)}",
            "title": clean_text(title, limit=MAX_TITLE, field="title", required=True),
            "category": _clean_category(category),
            "why": clean_text(why, limit=MAX_WHY, field="why", multiline=True),
            "plan": _clean_plan(plan),
            "stage": clean_text(stage, limit=MAX_STAGE, field="stage"),
            "cadence": clean_text(cadence, limit=MAX_CADENCE, field="cadence"),
            "progress": {
                "current": None,
                "target": _clean_number(target, "target"),
                "unit": clean_text(unit, limit=MAX_UNIT, field="unit"),
            },
            "status": "active",
            "endsAt": _clean_time(ends_at, "endsAt"),
            "sessionKey": clean_session_key(session_key),
            "createdBy": "owner" if created_by == "owner" else "agent",
            "createdAt": now,
            "updatedAt": now,
            "closedAt": None,
            "log": [],
        }

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            open_count = sum(1 for item in data["objectives"] if item.get("status") in OPEN_STATUSES)
            if open_count >= MAX_ACTIVE_OBJECTIVES:
                raise ObjectiveError(
                    "LIMIT", f"Already tracking {MAX_ACTIVE_OBJECTIVES} goals. Close one first."
                )
            data["objectives"].append(record)
            return copy.deepcopy(record)

        return self._transaction(mutate)

    def update(self, objective_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply the given fields; a field left out is left as it is."""
        cleaned: dict[str, Any] = {}
        if "title" in changes:
            cleaned["title"] = clean_text(changes["title"], limit=MAX_TITLE, field="title", required=True)
        if "category" in changes:
            cleaned["category"] = _clean_category(changes["category"])
        if "why" in changes:
            cleaned["why"] = clean_text(changes["why"], limit=MAX_WHY, field="why", multiline=True)
        if "plan" in changes:
            cleaned["plan"] = _clean_plan(changes["plan"])
        if "stage" in changes:
            cleaned["stage"] = clean_text(changes["stage"], limit=MAX_STAGE, field="stage")
        if "cadence" in changes:
            cleaned["cadence"] = clean_text(changes["cadence"], limit=MAX_CADENCE, field="cadence")
        if "endsAt" in changes:
            cleaned["endsAt"] = _clean_time(changes["endsAt"], "endsAt")
        if "sessionKey" in changes:
            cleaned["sessionKey"] = clean_session_key(changes["sessionKey"])
        progress: dict[str, Any] = {}
        if "current" in changes:
            progress["current"] = _clean_number(changes["current"], "current")
        if "target" in changes:
            progress["target"] = _clean_number(changes["target"], "target")
        if "unit" in changes:
            progress["unit"] = clean_text(changes["unit"], limit=MAX_UNIT, field="unit")
        done_steps = changes.get("doneSteps")
        if done_steps is not None and (
            not isinstance(done_steps, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in done_steps)
        ):
            raise ObjectiveError("INVALID", "doneSteps must be a list of step numbers")
        if not cleaned and not progress and not done_steps:
            raise ObjectiveError("INVALID", "Nothing to change")

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            record = _require(data["objectives"], objective_id)
            record.update(cleaned)
            record["progress"] = {**(record.get("progress") or {}), **progress}
            for number in done_steps or []:
                if not 1 <= number <= len(record["plan"]):
                    raise ObjectiveError("INVALID", f"There is no step {number}")
                record["plan"][number - 1]["done"] = True
            record["updatedAt"] = _now_ms()
            return copy.deepcopy(record)

        return self._transaction(mutate)

    def log(self, objective_id: str, text: Any, *, kind: str = "note") -> dict[str, Any]:
        entry_text = clean_text(text, limit=MAX_LOG_TEXT, field="note", required=True, multiline=True)
        entry_kind = kind if kind in LOG_KINDS else "note"

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            record = _require(data["objectives"], objective_id)
            _append_log(record, entry_kind, entry_text)
            return copy.deepcopy(record)

        return self._transaction(mutate)

    def set_status(self, objective_id: str, status: str, *, note: Any = "") -> dict[str, Any]:
        if status not in STATUSES:
            raise ObjectiveError("INVALID", f"status must be one of {', '.join(STATUSES)}")
        note_text = clean_text(note, limit=MAX_LOG_TEXT, field="note", multiline=True)

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            record = _require(data["objectives"], objective_id)
            was_open = record.get("status") in OPEN_STATUSES
            if status in OPEN_STATUSES and not was_open:
                open_count = sum(1 for item in data["objectives"] if item.get("status") in OPEN_STATUSES)
                if open_count >= MAX_ACTIVE_OBJECTIVES:
                    raise ObjectiveError("LIMIT", f"Already tracking {MAX_ACTIVE_OBJECTIVES} goals.")
            record["status"] = status
            record["closedAt"] = None if status in OPEN_STATUSES else _now_ms()
            _append_log(record, "status", note_text or status)
            return copy.deepcopy(record)

        return self._transaction(mutate)

    def delete(self, objective_id: str) -> bool:
        def mutate(data: dict[str, Any]) -> bool:
            before = len(data["objectives"])
            data["objectives"] = [item for item in data["objectives"] if item.get("id") != objective_id]
            return len(data["objectives"]) != before

        return self._transaction(mutate)


def _find(items: list[dict[str, Any]], objective_id: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get("id") == objective_id), None)


def _require(items: list[dict[str, Any]], objective_id: str) -> dict[str, Any]:
    record = _find(items, str(objective_id or ""))
    if record is None:
        raise ObjectiveError("NOT_FOUND", "No such goal")
    return record


def _append_log(record: dict[str, Any], kind: str, text: str) -> None:
    now = _now_ms()
    entries = list(record.get("log") or [])
    entries.append({"at": now, "kind": kind, "text": text})
    record["log"] = entries[-MAX_LOG_ENTRIES:]
    record["updatedAt"] = now


def _prune(items: list[dict[str, Any]]) -> None:
    """Keep every open goal; drop the oldest closed ones past the cap."""
    if len(items) <= MAX_STORED_OBJECTIVES:
        return
    closed = sorted(
        (item for item in items if item.get("status") not in OPEN_STATUSES),
        key=lambda item: int(item.get("closedAt") or item.get("updatedAt") or 0),
    )
    excess = {id(item) for item in closed[: len(items) - MAX_STORED_OBJECTIVES]}
    items[:] = [item for item in items if id(item) not in excess]


_stores: dict[Path, ObjectiveStore] = {}


def get_objective_store(home: Path | None = None) -> ObjectiveStore:
    """The store of ``home`` (this process' agent home by default)."""
    root = Path(home or get_flowly_home())
    store = _stores.get(root)
    if store is None:
        store = _stores[root] = ObjectiveStore(root)
    return store
