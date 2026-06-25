"""Single-writer SQLite store for the Flowly Board.

Design notes
------------
* **One writer.** Every write goes through this store, serialized by an
  internal ``RLock``. There is no claim-lock / CAS / heartbeat machinery
  because there are no competing writer *processes* — Flowly's workers are
  in-process async subagents that never write the board directly.
* **Sync API.** Methods are plain (synchronous) SQLite calls. They are
  cheap and safe to call from async handlers (aiohttp gateway) and from
  async tool ``execute`` coroutines alike; the lock keeps concurrent
  callers consistent. Keeping the API sync also makes it trivially usable
  from tests and the CLI.
* **WAL + FK.** WAL for concurrent readers (dashboard polling while the
  agent writes); foreign keys on so note cascade-delete works.

Storage location: ``get_flowly_home() / "board.db"`` (profile-aware).
"""

from __future__ import annotations

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
STATUS_IN_PROGRESS = "in_progress"
STATUS_WAITING = "waiting"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

VALID_STATUSES = {
    STATUS_TODO,
    STATUS_IN_PROGRESS,
    STATUS_WAITING,
    STATUS_DONE,
    STATUS_CANCELLED,
}

# Terminal states never get auto-reset by crash recovery.
TERMINAL_STATUSES = {STATUS_DONE, STATUS_CANCELLED}

# Column order the UI renders left → right.
COLUMN_ORDER = [STATUS_TODO, STATUS_IN_PROGRESS, STATUS_WAITING, STATUS_DONE]


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
    run_id: Optional[str] = None
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
        d["runId"] = self.run_id
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
    run_id         TEXT,
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

CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS idx_cards_parent ON cards(parent_id);
CREATE INDEX IF NOT EXISTS idx_notes_card ON card_notes(card_id);
"""

_CARD_COLUMNS = (
    "id, title, body, status, origin_channel, origin_chat_id, created_by, "
    "run_id, parent_id, result, error, created_at, updated_at"
)


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
            self._conn.commit()
        logger.debug(f"[board] store ready at {self.db_path}")

    # -- internal -----------------------------------------------------------

    def _row_to_card(self, row: sqlite3.Row, *, with_notes: bool = False) -> Card:
        card = Card(
            id=row["id"],
            title=row["title"],
            body=row["body"] or "",
            status=row["status"],
            origin_channel=row["origin_channel"] or "",
            origin_chat_id=row["origin_chat_id"] or "",
            created_by=row["created_by"] or "user",
            run_id=row["run_id"],
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
        parent_id: Optional[str] = None,
    ) -> Card:
        title = (title or "").strip()
        if not title:
            raise BoardError("card title cannot be empty")
        if status not in VALID_STATUSES:
            raise BoardError(f"invalid status: {status!r}")
        now = time.time()
        card_id = _new_card_id()
        with self._lock:
            if parent_id is not None and self._get_card_locked(parent_id) is None:
                raise BoardError(f"parent card not found: {parent_id!r}")
            self._conn.execute(
                f"INSERT INTO cards ({_CARD_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    card_id, title, body, status, origin_channel, origin_chat_id,
                    created_by, None, parent_id, None, None, now, now,
                ),
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
    ) -> Card:
        if status not in VALID_STATUSES:
            raise BoardError(f"invalid status: {status!r}")
        now = time.time()
        with self._lock:
            existing = self._get_card_locked(card_id)
            if existing is None:
                raise BoardError(f"card not found: {card_id!r}")
            sets = ["status = ?", "updated_at = ?"]
            args: list[Any] = [status, now]
            if result is not None:
                sets.append("result = ?")
                args.append(result)
            if error is not None:
                sets.append("error = ?")
                args.append(error)
            if clear_run_id or status in TERMINAL_STATUSES:
                sets.append("run_id = NULL")
            args.append(card_id)
            self._conn.execute(
                f"UPDATE cards SET {', '.join(sets)} WHERE id = ?", args
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
                "UPDATE cards SET run_id = ?, updated_at = ? WHERE id = ?",
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
    ) -> Card:
        now = time.time()
        with self._lock:
            if self._get_card_locked(card_id) is None:
                raise BoardError(f"card not found: {card_id!r}")
            sets = ["updated_at = ?"]
            args: list[Any] = [now]
            if title is not None:
                t = title.strip()
                if not t:
                    raise BoardError("card title cannot be empty")
                sets.append("title = ?")
                args.append(t)
            if body is not None:
                sets.append("body = ?")
                args.append(body)
            args.append(card_id)
            self._conn.execute(
                f"UPDATE cards SET {', '.join(sets)} WHERE id = ?", args
            )
            self._conn.commit()
            card = self._get_card_locked(card_id, with_notes=True)
        assert card is not None
        return card

    def add_note(self, card_id: str, author: str, text: str) -> CardNote:
        text = (text or "").strip()
        if not text:
            raise BoardError("note text cannot be empty")
        now = time.time()
        with self._lock:
            if self._get_card_locked(card_id) is None:
                raise BoardError(f"card not found: {card_id!r}")
            cur = self._conn.execute(
                "INSERT INTO card_notes (card_id, author, text, created_at) "
                "VALUES (?, ?, ?, ?)",
                (card_id, author, text, now),
            )
            self._conn.execute(
                "UPDATE cards SET updated_at = ? WHERE id = ?", (now, card_id)
            )
            self._conn.commit()
            note_id = cur.lastrowid
        return CardNote(
            id=int(note_id), card_id=card_id, author=author, text=text, created_at=now
        )

    def delete_card(self, card_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def delete_by_status(self, status: str) -> int:
        """Delete every card in ``status``. Returns the number removed."""
        if status not in VALID_STATUSES:
            raise BoardError(f"invalid status: {status!r}")
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
                "SELECT id, run_id FROM cards WHERE status = ?", (STATUS_IN_PROGRESS,)
            )
            rows = cur.fetchall()
            for r in rows:
                rid = r["run_id"]
                if rid and rid in live_run_ids:
                    continue
                self._conn.execute(
                    "UPDATE cards SET status = ?, run_id = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (STATUS_TODO, now, r["id"]),
                )
                self._conn.execute(
                    "INSERT INTO card_notes (card_id, author, text, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (r["id"], "system", "reset to todo after restart (worker gone)", now),
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

    def list_cards(
        self,
        *,
        status: Optional[str] = None,
        parent_id: Optional[str] = None,
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
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(int(limit))
        with self._lock:
            cur = self._conn.execute(
                f"SELECT {_CARD_COLUMNS} FROM cards{where} "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                args,
            )
            return [self._row_to_card(r, with_notes=with_notes) for r in cur.fetchall()]

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
            "timestampMs": int(time.time() * 1000),
        }

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
