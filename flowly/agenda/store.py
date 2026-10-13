"""The agent's agenda: when it will look at something again, and why.

One JSON file per agent home (``<home>/agenda/agenda.json``) holds:

- ``items``: pending wake-ups. Each has a time, the note the agent left
  itself, the conversation it is about, and, for a goal's check-in, the goal.
  A goal has at most one pending check-in; scheduling another replaces it.
- ``history``: the last settled wake-ups and what came of them (delivered,
  silent, dropped and why), for the owner to see and the agent to learn from.
- ``conversations``: per conversation, when the agent last wrote first, how
  many of those messages are still unanswered, and when the owner last wrote.
- ``settings``: the owner's proactivity level, quiet hours, timezone and the
  time of the daily review.

Written under a file lock with an atomic replace, like the goals file.
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

from flowly.agenda import policy
from flowly.objectives.store import ObjectiveError, clean_session_key, clean_text
from flowly.profile import _atomic_write_json, get_flowly_home

SCHEMA_VERSION = 1
KINDS = ("objective", "follow_up")
MAX_PENDING = 40
#: Follow-ups one conversation may hold at once (goal check-ins not counted).
MAX_FOLLOW_UPS_PER_CONVERSATION = 6
MAX_NOTE = 1_000
MAX_HISTORY = 100
MAX_CONVERSATIONS = 200
#: Attempts at one wake-up whose turn fails before it is given up.
MAX_ATTEMPTS = 3
RETRY_DELAY_MS = 30 * 60 * 1000
DEFAULT_REVIEW_TIME = "08:30"

#: Conversations an agent may write first in: the owner's chats and channels.
#: Internal sessions (scheduled runs, reviews, flowlet sources) never qualify.
OWNER_CONVERSATION_PREFIXES = (
    "desktop:", "web:", "ios:", "android:", "cli:", "tui:",
    "telegram:", "whatsapp:", "discord:", "slack:", "teams:", "imessage:", "email:",
)

T = TypeVar("T")


def _now_ms() -> int:
    return int(time.time() * 1000)


def is_owner_conversation(session_key: str) -> bool:
    return bool(session_key) and session_key.startswith(OWNER_CONVERSATION_PREFIXES) and len(session_key.split(":", 1)[1]) > 0


def _default_settings() -> dict[str, Any]:
    return {
        "level": policy.DEFAULT_LEVEL,
        "quietStart": policy.DEFAULT_QUIET_START,
        "quietEnd": policy.DEFAULT_QUIET_END,
        "timezone": "",
        "reviewTime": DEFAULT_REVIEW_TIME,
    }


def _clean_clock(value: Any, field: str) -> str:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59 and len(parts[1]) == 2:
            return f"{hour:02d}:{minute:02d}"
    raise ObjectiveError("INVALID", f"{field} must be HH:MM")


class AgendaStore:
    def __init__(self, home: Path, *, lock_timeout: float = 10.0):
        self.path = Path(home) / "agenda" / "agenda.json"
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
            raise ObjectiveError("BUSY", "The agenda is busy. Retry.") from exc

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            raise ObjectiveError("CORRUPT", f"Agenda file is unreadable: {exc}") from exc
        if not isinstance(raw, dict):
            raise ObjectiveError("CORRUPT", "Agenda file has an unknown shape")
        raw.setdefault("revision", 0)
        raw["settings"] = {**_default_settings(), **(raw.get("settings") or {})}
        for key in ("items", "history"):
            if not isinstance(raw.get(key), list):
                raw[key] = []
        if not isinstance(raw.get("conversations"), dict):
            raw["conversations"] = {}
        return raw

    def _transaction(self, mutate: Callable[[dict[str, Any]], T]) -> T:
        with self._locked():
            data = self._read()
            result = mutate(data)
            data["version"] = SCHEMA_VERSION
            data["revision"] = int(data.get("revision") or 0) + 1
            data["history"] = data["history"][-MAX_HISTORY:]
            if len(data["conversations"]) > MAX_CONVERSATIONS:
                ordered = sorted(
                    data["conversations"].items(),
                    key=lambda kv: max(int(kv[1].get("lastOwnerAt") or 0), int(kv[1].get("lastProactiveAt") or 0)),
                )
                data["conversations"] = dict(ordered[-MAX_CONVERSATIONS:])
            _atomic_write_json(self.path, data)
            return result

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            return copy.deepcopy(self._read())

    # ── settings ─────────────────────────────────────────────────────

    def settings(self) -> dict[str, Any]:
        return self.snapshot()["settings"]

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        if "level" in changes:
            level = str(changes["level"] or "").strip().lower()
            if level not in policy.LEVELS:
                raise ObjectiveError("INVALID", f"level must be one of {', '.join(policy.LEVELS)}")
            cleaned["level"] = level
        if "quietStart" in changes:
            cleaned["quietStart"] = _clean_clock(changes["quietStart"], "quietStart")
        if "quietEnd" in changes:
            cleaned["quietEnd"] = _clean_clock(changes["quietEnd"], "quietEnd")
        if "reviewTime" in changes:
            cleaned["reviewTime"] = _clean_clock(changes["reviewTime"], "reviewTime")
        if "timezone" in changes:
            zone = str(changes["timezone"] or "").strip()
            if zone:
                from flowly.cron.timezone import schedule_timezone

                try:
                    schedule_timezone(zone)
                except ValueError as exc:
                    raise ObjectiveError("INVALID", f"Unknown timezone: {zone}") from exc
            cleaned["timezone"] = zone
        if not cleaned:
            raise ObjectiveError("INVALID", "Nothing to change")

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            data["settings"].update(cleaned)
            return dict(data["settings"])

        return self._transaction(mutate)

    # ── wake-ups ─────────────────────────────────────────────────────

    def schedule(
        self,
        *,
        kind: str,
        due_at: int,
        note: Any,
        session_key: Any,
        objective_id: str | None = None,
        cancel_on_reply: bool = True,
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise ObjectiveError("INVALID", f"kind must be one of {', '.join(KINDS)}")
        key = clean_session_key(session_key)
        if not is_owner_conversation(key):
            raise ObjectiveError("INVALID", "A follow-up must belong to one of the owner's conversations")
        if kind == "objective" and not objective_id:
            raise ObjectiveError("INVALID", "A goal check-in needs its goal")
        if not isinstance(due_at, int) or isinstance(due_at, bool):
            raise ObjectiveError("INVALID", "A time is required")
        item = {
            "id": f"wk_{secrets.token_hex(6)}",
            "kind": kind,
            "dueAt": due_at,
            "note": clean_text(note, limit=MAX_NOTE, field="note", required=True, multiline=True),
            "sessionKey": key,
            "objectiveId": objective_id if kind == "objective" else None,
            # A goal's check-in is about the goal, not the conversation's last
            # exchange: the owner writing in between does not make it moot.
            "cancelOnReply": bool(cancel_on_reply) and kind == "follow_up",
            "createdAt": _now_ms(),
            "attempts": 0,
            "state": "pending",
        }

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            items = data["items"]
            if kind == "objective":
                replaced = [i for i in items if i.get("objectiveId") == objective_id and i.get("state") == "pending"]
                for old in replaced:
                    items.remove(old)
                    data["history"].append({**old, "outcome": "replaced", "settledAt": item["createdAt"]})
            else:
                same = sum(1 for i in items if i.get("kind") == "follow_up" and i.get("sessionKey") == key)
                if same >= MAX_FOLLOW_UPS_PER_CONVERSATION:
                    raise ObjectiveError(
                        "LIMIT", "This conversation already has enough follow-ups planned. Cancel one first."
                    )
            if len(items) >= MAX_PENDING:
                raise ObjectiveError("LIMIT", f"The agenda already holds {MAX_PENDING} follow-ups.")
            items.append(item)
            return copy.deepcopy(item)

        return self._transaction(mutate)

    def pending(self) -> list[dict[str, Any]]:
        items = self.snapshot()["items"]
        return sorted(items, key=lambda i: int(i.get("dueAt") or 0))

    def next_for_objective(self, objective_id: str) -> dict[str, Any] | None:
        return next((i for i in self.pending() if i.get("objectiveId") == objective_id), None)

    def due(self, now_ms: int) -> list[dict[str, Any]]:
        return [i for i in self.pending() if i.get("state") == "pending" and int(i.get("dueAt") or 0) <= now_ms]

    def cancel(self, item_id: str, *, reason: str = "cancelled") -> bool:
        def mutate(data: dict[str, Any]) -> bool:
            found = next((i for i in data["items"] if i.get("id") == item_id and i.get("state") == "pending"), None)
            if found is None:
                return False
            data["items"].remove(found)
            data["history"].append({**found, "outcome": "cancelled", "reason": reason, "settledAt": _now_ms()})
            return True

        return self._transaction(mutate)

    def cancel_for_objective(self, objective_id: str, *, reason: str) -> int:
        def mutate(data: dict[str, Any]) -> int:
            gone = [i for i in data["items"] if i.get("objectiveId") == objective_id and i.get("state") == "pending"]
            now = _now_ms()
            for item in gone:
                data["items"].remove(item)
                data["history"].append({**item, "outcome": "cancelled", "reason": reason, "settledAt": now})
            return len(gone)

        return self._transaction(mutate)

    def defer(self, item_id: str, until_ms: int, reason: str) -> None:
        def mutate(data: dict[str, Any]) -> None:
            for item in data["items"]:
                if item.get("id") == item_id:
                    item["dueAt"] = int(until_ms)
                    item["state"] = "pending"
                    item["deferredReason"] = reason
                    item.pop("startedAt", None)

        self._transaction(mutate)

    def start(self, item_id: str) -> dict[str, Any] | None:
        """Mark a due wake-up as running; None when it is no longer pending."""
        def mutate(data: dict[str, Any]) -> dict[str, Any] | None:
            for item in data["items"]:
                if item.get("id") == item_id and item.get("state") == "pending":
                    item["state"] = "running"
                    item["startedAt"] = _now_ms()
                    return copy.deepcopy(item)
            return None

        return self._transaction(mutate)

    def settle(self, item_id: str, outcome: str, *, reason: str = "", preview: str = "") -> dict[str, Any] | None:
        def mutate(data: dict[str, Any]) -> dict[str, Any] | None:
            found = next((i for i in data["items"] if i.get("id") == item_id), None)
            if found is None:
                return None
            data["items"].remove(found)
            record = {**found, "outcome": outcome, "reason": reason, "settledAt": _now_ms()}
            record.pop("state", None)
            if preview:
                record["preview"] = preview[:200]
            data["history"].append(record)
            return copy.deepcopy(record)

        return self._transaction(mutate)

    def retry_or_give_up(self, item_id: str, reason: str) -> str:
        """After a failed turn: try again later, or give up after MAX_ATTEMPTS."""
        def mutate(data: dict[str, Any]) -> str:
            found = next((i for i in data["items"] if i.get("id") == item_id), None)
            if found is None:
                return "gone"
            found["attempts"] = int(found.get("attempts") or 0) + 1
            if found["attempts"] >= MAX_ATTEMPTS:
                data["items"].remove(found)
                data["history"].append({**found, "outcome": "failed", "reason": reason, "settledAt": _now_ms()})
                return "failed"
            found["state"] = "pending"
            found["dueAt"] = _now_ms() + RETRY_DELAY_MS
            found["deferredReason"] = reason
            found.pop("startedAt", None)
            return "retry"

        return self._transaction(mutate)

    def recover_interrupted(self) -> int:
        """At start: a wake-up left running belongs to a process that died."""
        def mutate(data: dict[str, Any]) -> int:
            count = 0
            for item in list(data["items"]):
                if item.get("state") != "running":
                    continue
                count += 1
                item["attempts"] = int(item.get("attempts") or 0) + 1
                item.pop("startedAt", None)
                if item["attempts"] >= MAX_ATTEMPTS:
                    data["items"].remove(item)
                    data["history"].append({**item, "outcome": "failed", "reason": "interrupted", "settledAt": _now_ms()})
                else:
                    item["state"] = "pending"
                    item["dueAt"] = _now_ms() + 5 * 60 * 1000
                    item["deferredReason"] = "interrupted"
            return count

        return self._transaction(mutate)

    # ── conversations ────────────────────────────────────────────────

    def conversation(self, session_key: str) -> dict[str, Any]:
        return dict(self.snapshot()["conversations"].get(session_key) or {})

    def last_owner_at(self) -> int | None:
        """When the owner last wrote to this agent, in any conversation."""
        value = self.snapshot().get("lastOwnerAt")
        return int(value) if isinstance(value, (int, float)) and value > 0 else None

    def owner_wrote(self, session_key: str, now_ms: int | None = None) -> int:
        """The owner wrote in ``session_key``: their silence is over there, and
        follow-ups waiting on that conversation's last exchange are moot.
        Returns how many follow-ups were cancelled."""
        now = now_ms if now_ms is not None else _now_ms()

        def mutate(data: dict[str, Any]) -> int:
            stats = data["conversations"].setdefault(session_key, {})
            stats["unanswered"] = 0
            stats["lastOwnerAt"] = now
            data["lastOwnerAt"] = now
            moot = [
                i for i in data["items"]
                if i.get("sessionKey") == session_key and i.get("cancelOnReply") and i.get("state") == "pending"
            ]
            for item in moot:
                data["items"].remove(item)
                data["history"].append({**item, "outcome": "cancelled", "reason": "owner_wrote", "settledAt": now})
            return len(moot)

        return self._transaction(mutate)

    def delivered(self, session_key: str, now_ms: int | None = None) -> None:
        now = now_ms if now_ms is not None else _now_ms()

        def mutate(data: dict[str, Any]) -> None:
            stats = data["conversations"].setdefault(session_key, {})
            stats["unanswered"] = int(stats.get("unanswered") or 0) + 1
            stats["lastProactiveAt"] = now

        self._transaction(mutate)

    def mark_review(self, day: str, now_ms: int | None = None) -> None:
        now = now_ms if now_ms is not None else _now_ms()

        def mutate(data: dict[str, Any]) -> None:
            data["lastReviewDay"] = day
            data["lastReviewAt"] = now

        self._transaction(mutate)


_stores: dict[Path, AgendaStore] = {}


def get_agenda_store(home: Path | None = None) -> AgendaStore:
    root = Path(home or get_flowly_home())
    store = _stores.get(root)
    if store is None:
        store = _stores[root] = AgendaStore(root)
    return store
