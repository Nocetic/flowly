"""Durable single-owner SQLite store for the shared Flowly Board.

Design notes
------------
* **One owner.** Only the primary runtime opens the database. Named profile
  workers use an authenticated broker and never receive a store handle.
* **Claimed execution.** Revision, idempotency, lease, heartbeat, and run rows
  make retries and crash recovery deterministic even though writes stay in one
  process.
* **Sync API.** Methods are plain (synchronous) SQLite calls. They are
  cheap and safe to call from async handlers (aiohttp gateway) and from
  async tool ``execute`` coroutines alike; the lock keeps concurrent
  callers consistent. Keeping the API sync also makes it trivially usable
  from tests and the CLI.
* **WAL + FK.** WAL for concurrent readers (dashboard polling while the
  agent writes); foreign keys on so note cascade-delete works.

Storage location: the primary runtime's ``get_flowly_home() / "board.db"``.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# --------------------------------------------------------------------------
# Status model
# --------------------------------------------------------------------------

STATUS_TODO = "todo"
STATUS_READY = "ready"
STATUS_IN_PROGRESS = "in_progress"
STATUS_WAITING = "waiting"
STATUS_REVIEW = "review"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_ARCHIVED = "archived"

VALID_STATUSES = {
    STATUS_TODO,
    STATUS_READY,
    STATUS_IN_PROGRESS,
    STATUS_WAITING,
    STATUS_REVIEW,
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_CANCELLED,
    STATUS_ARCHIVED,
}

# Terminal states never get auto-reset by crash recovery.
TERMINAL_STATUSES = {STATUS_DONE, STATUS_CANCELLED, STATUS_ARCHIVED}

# Column order the UI renders left → right.
COLUMN_ORDER = [
    STATUS_TODO,
    STATUS_READY,
    STATUS_IN_PROGRESS,
    STATUS_WAITING,
    STATUS_REVIEW,
    STATUS_BLOCKED,
    STATUS_DONE,
]

_MAX_TITLE_CHARS = 500
_MAX_BODY_CHARS = 30_000
_MAX_NOTE_CHARS = 20_000
_MAX_RESULT_CHARS = 200_000
_MAX_ERROR_CHARS = 8_000
_UNSET = object()


class BoardError(Exception):
    """Raised for invalid board operations (bad status, missing card)."""


# --------------------------------------------------------------------------
# Row types
# --------------------------------------------------------------------------


@dataclass
class CardNote:
    id: int
    card_id: str
    author: str
    text: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Card:
    id: str
    title: str
    status: str
    body: str = ""
    origin_channel: str = ""
    origin_chat_id: str = ""
    created_by: str = "user"
    assignee_profile: str = ""
    assignee_bot_id: str = ""
    priority: int = 0
    scheduled_at: Optional[float] = None
    idempotency_key: str = ""
    revision: int = 0
    run_id: Optional[str] = None
    claim_token: Optional[str] = None
    lease_expires_at: Optional[float] = None
    heartbeat_at: Optional[float] = None
    attempt_count: int = 0
    max_attempts: int = 2
    parent_id: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    notes: list[CardNote] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Stable camelCase mirror for the desktop/web clients, while keeping
        # snake_case for Python callers. The UI reads camelCase.
        d["originChannel"] = self.origin_channel
        d["originChatId"] = self.origin_chat_id
        d["createdBy"] = self.created_by
        d["assigneeProfile"] = self.assignee_profile
        d["assigneeBotId"] = self.assignee_bot_id
        d["scheduledAt"] = self.scheduled_at
        d["idempotencyKey"] = self.idempotency_key
        d["runId"] = self.run_id
        d["claimToken"] = self.claim_token
        d["leaseExpiresAt"] = self.lease_expires_at
        d["heartbeatAt"] = self.heartbeat_at
        d["attemptCount"] = self.attempt_count
        d["maxAttempts"] = self.max_attempts
        d["parentId"] = self.parent_id
        d["createdAt"] = self.created_at
        d["updatedAt"] = self.updated_at
        d["notes"] = [n.to_dict() for n in self.notes]
        return d


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL,
    origin_channel TEXT NOT NULL DEFAULT '',
    origin_chat_id TEXT NOT NULL DEFAULT '',
    created_by     TEXT NOT NULL DEFAULT 'user',
    assignee_profile TEXT NOT NULL DEFAULT '',
    assignee_bot_id TEXT NOT NULL DEFAULT '',
    priority       INTEGER NOT NULL DEFAULT 0,
    scheduled_at   REAL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    revision       INTEGER NOT NULL DEFAULT 0,
    run_id         TEXT,
    claim_token    TEXT,
    lease_expires_at REAL,
    heartbeat_at   REAL,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 2,
    parent_id      TEXT,
    result         TEXT,
    error          TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS card_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    author     TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS card_runs (
    id               TEXT PRIMARY KEY,
    card_id          TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    attempt          INTEGER NOT NULL,
    profile          TEXT NOT NULL DEFAULT '',
    claim_token      TEXT NOT NULL,
    worker_run_id    TEXT,
    status           TEXT NOT NULL,
    started_at       REAL NOT NULL,
    completed_at     REAL,
    error            TEXT
);

CREATE TABLE IF NOT EXISTS card_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    actor      TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS card_links (
    parent_id  TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    child_id   TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    PRIMARY KEY (parent_id, child_id),
    CHECK (parent_id <> child_id)
);

CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS idx_cards_parent ON cards(parent_id);
CREATE INDEX IF NOT EXISTS idx_notes_card ON card_notes(card_id);
CREATE INDEX IF NOT EXISTS idx_card_runs_card ON card_runs(card_id, attempt);
CREATE INDEX IF NOT EXISTS idx_card_events_card ON card_events(card_id, id);
CREATE INDEX IF NOT EXISTS idx_card_links_child ON card_links(child_id);
"""

_POST_MIGRATION_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_cards_assignee ON cards(assignee_profile);
CREATE INDEX IF NOT EXISTS idx_cards_dispatch ON cards(status, scheduled_at, priority);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_idempotency
    ON cards(idempotency_key) WHERE idempotency_key <> '';
"""

_CARD_COLUMNS = (
    "id, title, body, status, origin_channel, origin_chat_id, created_by, "
    "assignee_profile, assignee_bot_id, priority, scheduled_at, idempotency_key, "
    "revision, run_id, claim_token, lease_expires_at, heartbeat_at, attempt_count, "
    "max_attempts, parent_id, result, error, created_at, updated_at"
)

_ADDITIVE_CARD_COLUMNS = {
    "assignee_profile": "TEXT NOT NULL DEFAULT ''",
    "assignee_bot_id": "TEXT NOT NULL DEFAULT ''",
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "scheduled_at": "REAL",
    "idempotency_key": "TEXT NOT NULL DEFAULT ''",
    "revision": "INTEGER NOT NULL DEFAULT 0",
    "claim_token": "TEXT",
    "lease_expires_at": "REAL",
    "heartbeat_at": "REAL",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL DEFAULT 2",
}


def _new_card_id() -> str:
    return "c_" + uuid.uuid4().hex[:8]


class BoardStore:
    """Single-writer board store. Thread-safe via an internal lock."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False: aiohttp handlers and tool coroutines may
        # touch the store from the event-loop thread while tests/CLI use the
        # main thread. The RLock serializes all access regardless.
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._migrate_schema_locked()
            self._conn.executescript(_POST_MIGRATION_SCHEMA)
            self._conn.commit()
        logger.debug(f"[board] store ready at {self.db_path}")

    # -- internal -----------------------------------------------------------

    def _migrate_schema_locked(self) -> None:
        """Add durable execution columns without rewriting existing boards."""
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(cards)").fetchall()
        }
        for name, declaration in _ADDITIVE_CARD_COLUMNS.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE cards ADD COLUMN {name} {declaration}"
                )
        run_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(card_runs)").fetchall()
        }
        if "worker_run_id" not in run_columns:
            self._conn.execute("ALTER TABLE card_runs ADD COLUMN worker_run_id TEXT")

    def _row_to_card(self, row: sqlite3.Row, *, with_notes: bool = False) -> Card:
        card = Card(
            id=row["id"],
            title=row["title"],
            body=row["body"] or "",
            status=row["status"],
            origin_channel=row["origin_channel"] or "",
            origin_chat_id=row["origin_chat_id"] or "",
            created_by=row["created_by"] or "user",
            assignee_profile=row["assignee_profile"] or "",
            assignee_bot_id=row["assignee_bot_id"] or "",
            priority=int(row["priority"] or 0),
            scheduled_at=row["scheduled_at"],
            idempotency_key=row["idempotency_key"] or "",
            revision=int(row["revision"] or 0),
            run_id=row["run_id"],
            claim_token=row["claim_token"],
            lease_expires_at=row["lease_expires_at"],
            heartbeat_at=row["heartbeat_at"],
            attempt_count=int(row["attempt_count"] or 0),
            max_attempts=max(1, int(row["max_attempts"] or 2)),
            parent_id=row["parent_id"],
            result=row["result"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        if with_notes:
            card.notes = self._get_notes_locked(card.id)
        return card

    def _get_notes_locked(self, card_id: str) -> list[CardNote]:
        cur = self._conn.execute(
            "SELECT id, card_id, author, text, created_at FROM card_notes "
            "WHERE card_id = ? ORDER BY created_at ASC, id ASC",
            (card_id,),
        )
        return [
            CardNote(
                id=r["id"],
                card_id=r["card_id"],
                author=r["author"],
                text=r["text"],
                created_at=r["created_at"],
            )
            for r in cur.fetchall()
        ]

    def _get_card_locked(self, card_id: str, *, with_notes: bool = False) -> Optional[Card]:
        cur = self._conn.execute(
            f"SELECT {_CARD_COLUMNS} FROM cards WHERE id = ?", (card_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_card(row, with_notes=with_notes)

    def _record_event_locked(
        self,
        card_id: str,
        kind: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        *,
        now: float | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO card_events (card_id, kind, actor, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                card_id,
                kind,
                (actor or "system")[:128],
                json.dumps(payload or {}, separators=(",", ":"), sort_keys=True),
                time.time() if now is None else now,
            ),
        )

    # -- writes -------------------------------------------------------------

    def add_card(
        self,
        title: str,
        *,
        body: str = "",
        status: str = STATUS_TODO,
        origin_channel: str = "",
        origin_chat_id: str = "",
        created_by: str = "user",
        assignee_profile: str = "",
        assignee_bot_id: str = "",
        priority: int = 0,
        scheduled_at: Optional[float] = None,
        idempotency_key: str = "",
        max_attempts: int = 2,
        parent_id: Optional[str] = None,
    ) -> Card:
        if not isinstance(title, str):
            raise BoardError("card title must be a string")
        title = title.strip()
        if not title:
            raise BoardError("card title cannot be empty")
        if len(title) > _MAX_TITLE_CHARS:
            raise BoardError("card title is too long")
        if not isinstance(body, str) or len(body) > _MAX_BODY_CHARS:
            raise BoardError("card body is too long")
        if status not in VALID_STATUSES:
            raise BoardError(f"invalid status: {status!r}")
        if not all(
            isinstance(value, str)
            for value in (
                origin_channel,
                origin_chat_id,
                created_by,
                assignee_profile,
                assignee_bot_id,
                idempotency_key,
            )
        ):
            raise BoardError("card identity fields must be strings")
        assignee_profile = assignee_profile.strip()
        assignee_bot_id = assignee_bot_id.strip()
        idempotency_key = idempotency_key.strip()
        if len(assignee_profile) > 64 or len(assignee_bot_id) > 128:
            raise BoardError("assignee identity is invalid")
        if len(idempotency_key) > 128:
            raise BoardError("idempotency key is too long")
        if any(ord(char) < 0x20 for char in idempotency_key):
            raise BoardError("idempotency key contains invalid characters")
        if len(origin_channel or "") > 64 or len(origin_chat_id or "") > 512:
            raise BoardError("origin identity is too long")
        if len(created_by or "") > 128:
            raise BoardError("creator identity is too long")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise BoardError("priority must be an integer")
        if priority < -100 or priority > 100:
            raise BoardError("priority must be between -100 and 100")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise BoardError("max attempts must be an integer")
        if max_attempts < 1 or max_attempts > 10:
            raise BoardError("max attempts must be between 1 and 10")
        if scheduled_at is not None:
            try:
                scheduled_at = float(scheduled_at)
            except (TypeError, ValueError) as exc:
                raise BoardError("scheduled time is invalid") from exc
            if not math.isfinite(scheduled_at):
                raise BoardError("scheduled time is invalid")
        now = time.time()
        card_id = _new_card_id()
        with self._lock:
            if idempotency_key:
                existing = self._conn.execute(
                    f"SELECT {_CARD_COLUMNS} FROM cards WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._row_to_card(existing, with_notes=True)
            if parent_id is not None and self._get_card_locked(parent_id) is None:
                raise BoardError(f"parent card not found: {parent_id!r}")
            self._conn.execute(
                f"INSERT INTO cards ({_CARD_COLUMNS}) VALUES "
                f"({', '.join('?' for _ in _CARD_COLUMNS.split(','))})",
                (
                    card_id, title, body, status, origin_channel, origin_chat_id,
                    created_by, assignee_profile, assignee_bot_id, priority,
                    scheduled_at, idempotency_key, 0, None, None, None, None,
                    0, max_attempts, parent_id, None, None, now, now,
                ),
            )
            self._record_event_locked(
                card_id,
                "created",
                created_by,
                {
                    "status": status,
                    "assigneeProfile": assignee_profile,
                },
                now=now,
            )
            self._conn.commit()
            card = self._get_card_locked(card_id, with_notes=True)
        assert card is not None
        logger.debug(f"[board] add_card {card_id} status={status} title={title!r}")
        return card

    def set_status(
        self,
        card_id: str,
        status: str,
        *,
        result: Optional[str] = None,
        error: Optional[str] = None,
        clear_run_id: bool = False,
        clear_schedule: bool = False,
        expected_revision: int | None = None,
        actor: str = "system",
    ) -> Card:
        if status not in VALID_STATUSES:
            raise BoardError(f"invalid status: {status!r}")
        if result is not None:
            if not isinstance(result, str):
                raise BoardError("card result must be a string")
            if len(result) > _MAX_RESULT_CHARS:
                raise BoardError("card result is too long")
        if error is not None:
            if not isinstance(error, str):
                raise BoardError("card error must be a string")
            if len(error) > _MAX_ERROR_CHARS:
                raise BoardError("card error is too long")
        now = time.time()
        with self._lock:
            existing = self._get_card_locked(card_id)
            if existing is None:
                raise BoardError(f"card not found: {card_id!r}")
            if existing.claim_token:
                raise BoardError("a claimed card must be controlled by its worker run")
            if expected_revision is not None and existing.revision != expected_revision:
                raise BoardError("card changed; refresh and try again")
            sets = ["status = ?", "updated_at = ?", "revision = revision + 1"]
            args: list[Any] = [status, now]
            if result is not None:
                sets.append("result = ?")
                args.append(result)
            if error is not None:
                sets.append("error = ?")
                args.append(error)
            if clear_run_id or status in TERMINAL_STATUSES:
                sets.append("run_id = NULL")
            if clear_schedule or status in TERMINAL_STATUSES:
                sets.append("scheduled_at = NULL")
            if status in TERMINAL_STATUSES:
                sets.extend([
                    "claim_token = NULL",
                    "lease_expires_at = NULL",
                    "heartbeat_at = NULL",
                ])
            args.append(card_id)
            args.append(existing.revision)
            cur = self._conn.execute(
                f"UPDATE cards SET {', '.join(sets)} WHERE id = ? AND revision = ?",
                args,
            )
            if cur.rowcount != 1:
                raise BoardError("card changed; refresh and try again")
            self._record_event_locked(
                card_id,
                "status_changed",
                actor,
                {"from": existing.status, "to": status},
                now=now,
            )
            self._conn.commit()
            card = self._get_card_locked(card_id, with_notes=True)
        assert card is not None
        return card

    def set_run_id(self, card_id: str, run_id: Optional[str]) -> Card:
        now = time.time()
        with self._lock:
            if self._get_card_locked(card_id) is None:
                raise BoardError(f"card not found: {card_id!r}")
            self._conn.execute(
                "UPDATE cards SET run_id = ?, updated_at = ?, "
                "revision = revision + 1 WHERE id = ?",
                (run_id, now, card_id),
            )
            self._conn.commit()
            card = self._get_card_locked(card_id, with_notes=True)
        assert card is not None
        return card

    def update_card(
        self,
        card_id: str,
        *,
        title: Optional[str] = None,
        body: Optional[str] = None,
        priority: int | None = None,
        scheduled_at: Any = _UNSET,
        max_attempts: int | None = None,
        expected_revision: int | None = None,
        actor: str = "user",
    ) -> Card:
        now = time.time()
        with self._lock:
            existing = self._get_card_locked(card_id)
            if existing is None:
                raise BoardError(f"card not found: {card_id!r}")
            if existing.claim_token:
                raise BoardError("a running card cannot be edited")
            if expected_revision is not None and existing.revision != expected_revision:
                raise BoardError("card changed; refresh and try again")
            if (
                title is None
                and body is None
                and priority is None
                and scheduled_at is _UNSET
                and max_attempts is None
            ):
                raise BoardError("no card updates were provided")
            sets = ["updated_at = ?", "revision = revision + 1"]
            args: list[Any] = [now]
            if title is not None:
                if not isinstance(title, str):
                    raise BoardError("card title must be a string")
                t = title.strip()
                if not t:
                    raise BoardError("card title cannot be empty")
                if len(t) > _MAX_TITLE_CHARS:
                    raise BoardError("card title is too long")
                sets.append("title = ?")
                args.append(t)
            if body is not None:
                if not isinstance(body, str) or len(body) > _MAX_BODY_CHARS:
                    raise BoardError("card body is too long")
                sets.append("body = ?")
                args.append(body)
            if priority is not None:
                if isinstance(priority, bool) or not isinstance(priority, int):
                    raise BoardError("priority must be an integer")
                if priority < -100 or priority > 100:
                    raise BoardError("priority must be between -100 and 100")
                sets.append("priority = ?")
                args.append(priority)
            if scheduled_at is not _UNSET:
                if scheduled_at is not None:
                    try:
                        scheduled_at = float(scheduled_at)
                    except (TypeError, ValueError) as exc:
                        raise BoardError("scheduled time is invalid") from exc
                    if not math.isfinite(scheduled_at):
                        raise BoardError("scheduled time is invalid")
                sets.append("scheduled_at = ?")
                args.append(scheduled_at)
            if max_attempts is not None:
                if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
                    raise BoardError("max attempts must be an integer")
                if max_attempts < 1 or max_attempts > 10:
                    raise BoardError("max attempts must be between 1 and 10")
                if max_attempts < existing.attempt_count:
                    raise BoardError("max attempts cannot be below completed attempts")
                sets.append("max_attempts = ?")
                args.append(max_attempts)
            args.append(card_id)
            args.append(existing.revision)
            cur = self._conn.execute(
                f"UPDATE cards SET {', '.join(sets)} WHERE id = ? AND revision = ?",
                args,
            )
            if cur.rowcount != 1:
                raise BoardError("card changed; refresh and try again")
            self._record_event_locked(
                card_id,
                "updated",
                actor,
                {
                    "title": title is not None,
                    "body": body is not None,
                    "priority": priority is not None,
                    "scheduledAt": scheduled_at is not _UNSET,
                    "maxAttempts": max_attempts is not None,
                },
                now=now,
            )
            self._conn.commit()
            card = self._get_card_locked(card_id, with_notes=True)
        assert card is not None
        return card

    def add_note(
        self,
        card_id: str,
        author: str,
        text: str,
        *,
        expected_revision: int | None = None,
    ) -> CardNote:
        if not isinstance(text, str) or not isinstance(author, str):
            raise BoardError("note fields must be strings")
        text = text.strip()
        if not text:
            raise BoardError("note text cannot be empty")
        if len(text) > _MAX_NOTE_CHARS:
            raise BoardError("note text is too long")
        author = (author or "user").strip() or "user"
        if len(author) > 128:
            raise BoardError("note author is too long")
        now = time.time()
        with self._lock:
            existing = self._get_card_locked(card_id)
            if existing is None:
                raise BoardError(f"card not found: {card_id!r}")
            if expected_revision is not None and existing.revision != expected_revision:
                raise BoardError("card changed; refresh and try again")
            cur = self._conn.execute(
                "INSERT INTO card_notes (card_id, author, text, created_at) "
                "VALUES (?, ?, ?, ?)",
                (card_id, author, text, now),
            )
            updated = self._conn.execute(
                "UPDATE cards SET updated_at = ?, revision = revision + 1 "
                "WHERE id = ? AND revision = ?",
                (now, card_id, existing.revision),
            )
            if updated.rowcount != 1:
                self._conn.rollback()
                raise BoardError("card changed; refresh and try again")
            self._record_event_locked(
                card_id,
                "comment_added",
                author,
                {"noteId": int(cur.lastrowid)},
                now=now,
            )
            self._conn.commit()
            note_id = cur.lastrowid
        return CardNote(
            id=int(note_id), card_id=card_id, author=author, text=text, created_at=now
        )

    def assign_card(
        self,
        card_id: str,
        *,
        profile: str,
        bot_id: str,
        actor: str = "user",
        ready: bool = True,
        expected_revision: int | None = None,
    ) -> Card:
        """Assign a card using both route identity and rename-safe bot id."""
        profile = (profile or "").strip()
        bot_id = (bot_id or "").strip()
        if not profile or len(profile) > 64 or not bot_id or len(bot_id) > 128:
            raise BoardError("assignee identity is invalid")
        now = time.time()
        with self._lock:
            existing = self._get_card_locked(card_id)
            if existing is None:
                raise BoardError(f"card not found: {card_id!r}")
            if existing.status == STATUS_IN_PROGRESS:
                raise BoardError("a running card cannot be reassigned")
            if expected_revision is not None and existing.revision != expected_revision:
                raise BoardError("card changed; refresh and try again")
            next_status = existing.status
            if ready and next_status in {
                STATUS_TODO,
                STATUS_WAITING,
                STATUS_BLOCKED,
            }:
                next_status = STATUS_READY
            cur = self._conn.execute(
                "UPDATE cards SET assignee_profile = ?, assignee_bot_id = ?, "
                "status = ?, updated_at = ?, revision = revision + 1 "
                "WHERE id = ? AND revision = ?",
                (profile, bot_id, next_status, now, card_id, existing.revision),
            )
            if cur.rowcount != 1:
                raise BoardError("card changed; refresh and try again")
            self._record_event_locked(
                card_id,
                "assigned",
                actor,
                {"profile": profile, "botId": bot_id, "status": next_status},
                now=now,
            )
            self._conn.commit()
            card = self._get_card_locked(card_id, with_notes=True)
        assert card is not None
        return card

    def unassign_card(
        self,
        card_id: str,
        *,
        actor: str = "user",
        expected_revision: int | None = None,
    ) -> Card:
        now = time.time()
        with self._lock:
            existing = self._get_card_locked(card_id)
            if existing is None:
                raise BoardError(f"card not found: {card_id!r}")
            if existing.status == STATUS_IN_PROGRESS:
                raise BoardError("a running card cannot be unassigned")
            if expected_revision is not None and existing.revision != expected_revision:
                raise BoardError("card changed; refresh and try again")
            next_status = STATUS_TODO if existing.status == STATUS_READY else existing.status
            cur = self._conn.execute(
                "UPDATE cards SET assignee_profile = '', assignee_bot_id = '', "
                "status = ?, updated_at = ?, revision = revision + 1 "
                "WHERE id = ? AND revision = ?",
                (next_status, now, card_id, existing.revision),
            )
            if cur.rowcount != 1:
                raise BoardError("card changed; refresh and try again")
            self._record_event_locked(
                card_id,
                "unassigned",
                actor,
                {"previousProfile": existing.assignee_profile},
                now=now,
            )
            self._conn.commit()
            card = self._get_card_locked(card_id, with_notes=True)
        assert card is not None
        return card

    def link_cards(self, parent_id: str, child_id: str, *, actor: str = "user") -> None:
        if parent_id == child_id:
            raise BoardError("a card cannot depend on itself")
        now = time.time()
        with self._lock:
            if self._get_card_locked(parent_id) is None:
                raise BoardError(f"parent card not found: {parent_id!r}")
            if self._get_card_locked(child_id) is None:
                raise BoardError(f"child card not found: {child_id!r}")
            would_cycle = self._conn.execute(
                "WITH RECURSIVE descendants(id) AS ("
                " SELECT child_id FROM card_links WHERE parent_id = ?"
                " UNION"
                " SELECT links.child_id FROM card_links links"
                " JOIN descendants ON links.parent_id = descendants.id"
                ") SELECT 1 FROM descendants WHERE id = ? LIMIT 1",
                (child_id, parent_id),
            ).fetchone()
            if would_cycle is not None:
                raise BoardError("dependency would create a cycle")
            inserted = self._conn.execute(
                "INSERT OR IGNORE INTO card_links (parent_id, child_id, created_at) "
                "VALUES (?, ?, ?)",
                (parent_id, child_id, now),
            )
            if inserted.rowcount == 1:
                self._record_event_locked(
                    child_id,
                    "dependency_added",
                    actor,
                    {"parentId": parent_id},
                    now=now,
                )
            self._conn.commit()

    def claim_card(
        self,
        card_id: str,
        *,
        worker: str,
        lease_seconds: float = 60.0,
    ) -> Card | None:
        """Atomically claim an eligible card and open an auditable run."""
        worker = (worker or "").strip()
        if not worker or len(worker) > 128:
            raise BoardError("worker identity is invalid")
        lease_seconds = float(lease_seconds)
        if lease_seconds < 15 or lease_seconds > 600:
            raise BoardError("lease must be between 15 and 600 seconds")
        now = time.time()
        token = "claim_" + uuid.uuid4().hex
        run_id = "board_run_" + uuid.uuid4().hex
        with self._lock:
            existing = self._get_card_locked(card_id)
            if existing is None:
                raise BoardError(f"card not found: {card_id!r}")
            if existing.status not in {STATUS_TODO, STATUS_READY, STATUS_WAITING}:
                return None
            if existing.scheduled_at is not None and existing.scheduled_at > now:
                return None
            blocked_parent = self._conn.execute(
                "SELECT 1 FROM card_links l JOIN cards p ON p.id = l.parent_id "
                "WHERE l.child_id = ? AND p.status <> ? LIMIT 1",
                (card_id, STATUS_DONE),
            ).fetchone()
            if blocked_parent is not None:
                return None
            attempt = existing.attempt_count + 1
            cur = self._conn.execute(
                "UPDATE cards SET status = ?, run_id = ?, claim_token = ?, "
                "lease_expires_at = ?, heartbeat_at = ?, attempt_count = ?, "
                "updated_at = ?, revision = revision + 1 "
                "WHERE id = ? AND revision = ? AND status = ?",
                (
                    STATUS_IN_PROGRESS,
                    run_id,
                    token,
                    now + lease_seconds,
                    now,
                    attempt,
                    now,
                    card_id,
                    existing.revision,
                    existing.status,
                ),
            )
            if cur.rowcount != 1:
                return None
            self._conn.execute(
                "INSERT INTO card_runs "
                "(id, card_id, attempt, profile, claim_token, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                (run_id, card_id, attempt, worker, token, now),
            )
            self._record_event_locked(
                card_id,
                "claimed",
                worker,
                {"runId": run_id, "attempt": attempt},
                now=now,
            )
            self._conn.commit()
            return self._get_card_locked(card_id, with_notes=True)

    def heartbeat_claim(
        self,
        card_id: str,
        claim_token: str,
        *,
        lease_seconds: float = 60.0,
    ) -> bool:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET heartbeat_at = ?, lease_expires_at = ?, "
                "updated_at = ? WHERE id = ? AND claim_token = ? "
                "AND status = ?",
                (
                    now,
                    now + float(lease_seconds),
                    now,
                    card_id,
                    claim_token,
                    STATUS_IN_PROGRESS,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def set_worker_run_id(
        self,
        card_id: str,
        claim_token: str,
        worker_run_id: str,
    ) -> bool:
        """Attach the profile runtime's opaque run id to the active attempt."""
        worker_run_id = (worker_run_id or "").strip()
        if not worker_run_id or len(worker_run_id) > 256:
            raise BoardError("worker run identity is invalid")
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET run_id = ?, updated_at = ? WHERE id = ? "
                "AND claim_token = ? AND status = ?",
                (worker_run_id, now, card_id, claim_token, STATUS_IN_PROGRESS),
            )
            if cur.rowcount != 1:
                return False
            self._conn.execute(
                "UPDATE card_runs SET worker_run_id = ? WHERE card_id = ? "
                "AND claim_token = ?",
                (worker_run_id, card_id, claim_token),
            )
            self._conn.commit()
            return True

    def finish_claim(
        self,
        card_id: str,
        claim_token: str,
        *,
        outcome: str,
        result: str | None = None,
        error: str | None = None,
        retry_delay: float = 0.0,
        actor: str = "system",
    ) -> Card:
        """Finish exactly the currently claimed run; stale workers fail closed."""
        if outcome not in {"done", "failed", "cancelled", "review", "blocked"}:
            raise BoardError("claim outcome is invalid")
        result = result[:_MAX_RESULT_CHARS] if result is not None else None
        error = error[:_MAX_ERROR_CHARS] if error is not None else None
        now = time.time()
        with self._lock:
            existing = self._get_card_locked(card_id)
            if existing is None:
                raise BoardError(f"card not found: {card_id!r}")
            if existing.status != STATUS_IN_PROGRESS or existing.claim_token != claim_token:
                raise BoardError("task claim is stale")

            if outcome == "done":
                next_status = STATUS_DONE
            elif outcome == "cancelled":
                next_status = STATUS_CANCELLED
            elif outcome == "review":
                next_status = STATUS_REVIEW
            elif outcome == "blocked":
                next_status = STATUS_BLOCKED
            elif existing.attempt_count >= existing.max_attempts:
                next_status = STATUS_BLOCKED
            else:
                next_status = STATUS_READY if existing.assignee_profile else STATUS_TODO

            scheduled_at = (
                now + max(0.0, float(retry_delay))
                if outcome == "failed" and next_status in {STATUS_READY, STATUS_TODO}
                else None
            )
            self._conn.execute(
                "UPDATE cards SET status = ?, result = ?, error = ?, "
                "scheduled_at = ?, run_id = NULL, claim_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, updated_at = ?, "
                "revision = revision + 1 WHERE id = ? AND claim_token = ?",
                (
                    next_status,
                    result,
                    error,
                    scheduled_at,
                    now,
                    card_id,
                    claim_token,
                ),
            )
            self._conn.execute(
                "UPDATE card_runs SET status = ?, completed_at = ?, error = ? "
                "WHERE card_id = ? AND claim_token = ?",
                (outcome, now, error, card_id, claim_token),
            )
            self._record_event_locked(
                card_id,
                "run_finished",
                actor,
                {
                    "outcome": outcome,
                    "status": next_status,
                    "attempt": existing.attempt_count,
                },
                now=now,
            )
            if outcome == "failed":
                self._conn.execute(
                    "INSERT INTO card_notes (card_id, author, text, created_at) "
                    "VALUES (?, 'system', ?, ?)",
                    (card_id, f"run failed: {error or 'unknown error'}", now),
                )
            self._conn.commit()
            card = self._get_card_locked(card_id, with_notes=True)
        assert card is not None
        return card

    def recover_expired_claims(self, *, now: float | None = None) -> int:
        """Requeue expired worker leases without accepting stale completions."""
        current = time.time() if now is None else float(now)
        recovered = 0
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_CARD_COLUMNS} FROM cards WHERE status = ? "
                "AND claim_token IS NOT NULL AND lease_expires_at <= ?",
                (STATUS_IN_PROGRESS, current),
            ).fetchall()
            for row in rows:
                card = self._row_to_card(row)
                next_status = (
                    STATUS_BLOCKED
                    if card.attempt_count >= card.max_attempts
                    else (STATUS_READY if card.assignee_profile else STATUS_TODO)
                )
                self._conn.execute(
                    "UPDATE cards SET status = ?, run_id = NULL, claim_token = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = NULL, error = ?, "
                    "updated_at = ?, revision = revision + 1 WHERE id = ? "
                    "AND claim_token = ?",
                    (
                        next_status,
                        "worker lease expired",
                        current,
                        card.id,
                        card.claim_token,
                    ),
                )
                self._conn.execute(
                    "UPDATE card_runs SET status = 'expired', completed_at = ?, "
                    "error = 'worker lease expired' WHERE card_id = ? AND claim_token = ?",
                    (current, card.id, card.claim_token),
                )
                self._record_event_locked(
                    card.id,
                    "lease_expired",
                    "system",
                    {"status": next_status, "attempt": card.attempt_count},
                    now=current,
                )
                recovered += 1
            if recovered:
                self._conn.commit()
        return recovered

    def delete_card(self, card_id: str) -> bool:
        with self._lock:
            existing = self._get_card_locked(card_id)
            if existing is not None and existing.status == STATUS_IN_PROGRESS:
                raise BoardError("cancel a running card before deleting it")
            cur = self._conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def delete_by_status(self, status: str) -> int:
        """Delete every card in ``status``. Returns the number removed."""
        if status not in VALID_STATUSES:
            raise BoardError(f"invalid status: {status!r}")
        if status == STATUS_IN_PROGRESS:
            raise BoardError("running cards cannot be cleared")
        with self._lock:
            cur = self._conn.execute("DELETE FROM cards WHERE status = ?", (status,))
            self._conn.commit()
            return cur.rowcount

    def reset_orphaned(self, live_run_ids: set[str]) -> int:
        """Crash recovery: reset ``in_progress`` cards whose worker is gone.

        Any card in ``in_progress`` whose ``run_id`` is not in
        ``live_run_ids`` (including cards with a null run_id) is moved back
        to ``todo`` with an explanatory note. Returns the number reset.
        """
        now = time.time()
        reset = 0
        with self._lock:
            cur = self._conn.execute(
                f"SELECT {_CARD_COLUMNS} FROM cards WHERE status = ?",
                (STATUS_IN_PROGRESS,),
            )
            rows = cur.fetchall()
            for r in rows:
                card = self._row_to_card(r)
                rid = card.run_id
                if rid and rid in live_run_ids:
                    continue
                next_status = (
                    STATUS_BLOCKED
                    if card.claim_token and card.attempt_count >= card.max_attempts
                    else (STATUS_READY if card.assignee_profile else STATUS_TODO)
                )
                self._conn.execute(
                    "UPDATE cards SET status = ?, run_id = NULL, claim_token = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = NULL, updated_at = ?, "
                    "revision = revision + 1 "
                    "WHERE id = ?",
                    (next_status, now, card.id),
                )
                self._conn.execute(
                    "INSERT INTO card_notes (card_id, author, text, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        card.id,
                        "system",
                        f"reset to {next_status} after restart (worker gone)",
                        now,
                    ),
                )
                if card.claim_token:
                    self._conn.execute(
                        "UPDATE card_runs SET status = 'interrupted', completed_at = ?, "
                        "error = 'primary runtime restarted' "
                        "WHERE card_id = ? AND claim_token = ?",
                        (now, card.id, card.claim_token),
                    )
                self._record_event_locked(
                    card.id,
                    "recovered_after_restart",
                    "system",
                    {"status": next_status},
                    now=now,
                )
                reset += 1
            if reset:
                self._conn.commit()
        if reset:
            logger.info(f"[board] crash recovery reset {reset} orphaned card(s)")
        return reset

    # -- reads --------------------------------------------------------------

    def get_card(self, card_id: str, *, with_notes: bool = True) -> Optional[Card]:
        with self._lock:
            return self._get_card_locked(card_id, with_notes=with_notes)

    def get_card_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        with_notes: bool = True,
    ) -> Optional[Card]:
        key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_CARD_COLUMNS} FROM cards WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            return (
                self._row_to_card(row, with_notes=with_notes)
                if row is not None
                else None
            )

    def list_cards(
        self,
        *,
        status: Optional[str] = None,
        parent_id: Optional[str] = None,
        assignee_profile: Optional[str] = None,
        with_notes: bool = False,
        limit: int = 500,
    ) -> list[Card]:
        if status is not None and status not in VALID_STATUSES:
            raise BoardError(f"invalid status: {status!r}")
        clauses = []
        args: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        if parent_id is not None:
            clauses.append("parent_id = ?")
            args.append(parent_id)
        if assignee_profile is not None:
            clauses.append("assignee_profile = ?")
            args.append(assignee_profile)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(int(limit))
        with self._lock:
            cur = self._conn.execute(
                f"SELECT {_CARD_COLUMNS} FROM cards{where} "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                args,
            )
            return [self._row_to_card(r, with_notes=with_notes) for r in cur.fetchall()]

    def list_dispatchable(
        self,
        *,
        limit: int = 20,
        now: float | None = None,
        exclude_profiles: tuple[str, ...] = (),
    ) -> list[Card]:
        """Return the next eligible card for each available named profile.

        Selecting at most one card per profile keeps a busy bot from filling
        the global dispatcher with tasks that are merely queued behind its
        per-profile semaphore. That preserves capacity and priority fairness
        for every other bot.
        """
        current = time.time() if now is None else float(now)
        excluded = tuple(
            profile.strip()
            for profile in exclude_profiles
            if isinstance(profile, str) and profile.strip()
        )
        exclusion_sql = ""
        args: list[Any] = [STATUS_READY, current, STATUS_DONE]
        if excluded:
            exclusion_sql = (
                "AND c.assignee_profile NOT IN ("
                + ", ".join("?" for _ in excluded)
                + ") "
            )
            args.extend(excluded)
        args.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "WITH ranked AS ("
                f" SELECT {_CARD_COLUMNS}, "
                " ROW_NUMBER() OVER ("
                "   PARTITION BY c.assignee_profile "
                "   ORDER BY c.priority DESC, c.created_at ASC, c.id ASC"
                " ) AS profile_rank "
                " FROM cards c "
                " WHERE c.status = ? AND c.assignee_profile <> '' "
                " AND (c.scheduled_at IS NULL OR c.scheduled_at <= ?) "
                " AND NOT EXISTS ("
                "   SELECT 1 FROM card_links l JOIN cards p ON p.id = l.parent_id "
                "   WHERE l.child_id = c.id AND p.status <> ?"
                f" ) {exclusion_sql}"
                ") "
                f"SELECT {_CARD_COLUMNS} FROM ranked WHERE profile_rank = 1 "
                "ORDER BY priority DESC, created_at ASC, id ASC LIMIT ?",
                args,
            ).fetchall()
            return [self._row_to_card(row) for row in rows]

    def get_runs(self, card_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, card_id, attempt, profile, worker_run_id, status, "
                "started_at, completed_at, error FROM card_runs WHERE card_id = ? "
                "ORDER BY attempt ASC, started_at ASC",
                (card_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "cardId": row["card_id"],
                "attempt": row["attempt"],
                "profile": row["profile"],
                "workerRunId": row["worker_run_id"],
                "status": row["status"],
                "startedAt": row["started_at"],
                "completedAt": row["completed_at"],
                "error": row["error"],
            }
            for row in rows
        ]

    def get_events(self, card_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, actor, payload, created_at FROM card_events "
                "WHERE card_id = ? ORDER BY id DESC LIMIT ?",
                (card_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            events.append({
                "id": row["id"],
                "kind": row["kind"],
                "actor": row["actor"],
                "payload": payload,
                "createdAt": row["created_at"],
            })
        return events

    def get_dependencies(self, card_id: str) -> dict[str, list[str]]:
        with self._lock:
            parents = [
                row["parent_id"]
                for row in self._conn.execute(
                    "SELECT parent_id FROM card_links WHERE child_id = ? "
                    "ORDER BY created_at, parent_id",
                    (card_id,),
                ).fetchall()
            ]
            children = [
                row["child_id"]
                for row in self._conn.execute(
                    "SELECT child_id FROM card_links WHERE parent_id = ? "
                    "ORDER BY created_at, child_id",
                    (card_id,),
                ).fetchall()
            ]
        return {"parents": parents, "children": children}

    def snapshot(self, *, with_notes: bool = False) -> dict[str, Any]:
        """Board snapshot for clients (TUI/desktop).

        Shape (camelCase for JS consumers)::

            {
              "columns": [
                {"status": "todo", "cards": [<card>, ...]},
                ...
              ],
              "counts": {"todo": N, "in_progress": N, ...},
              "total": N,
              "timestampMs": 1234567890123
            }
        """
        with self._lock:
            cur = self._conn.execute(
                f"SELECT {_CARD_COLUMNS} FROM cards ORDER BY created_at ASC, id ASC"
            )
            all_cards = [self._row_to_card(r, with_notes=with_notes) for r in cur.fetchall()]

        counts = {s: 0 for s in VALID_STATUSES}
        buckets: dict[str, list[dict[str, Any]]] = {s: [] for s in VALID_STATUSES}
        for c in all_cards:
            counts[c.status] = counts.get(c.status, 0) + 1
            buckets.setdefault(c.status, []).append(c.to_dict())

        columns = [
            {"status": s, "cards": buckets.get(s, [])} for s in COLUMN_ORDER
        ]
        return {
            "columns": columns,
            "counts": counts,
            "total": len(all_cards),
            "revision": max((card.revision for card in all_cards), default=0),
            "timestampMs": int(time.time() * 1000),
        }

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
