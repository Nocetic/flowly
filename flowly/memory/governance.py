"""Memory governance store — lifecycle layer over the existing memory engines.

This is the *governance* layer for Flowly's memory system. It does **not** store
facts itself; it wraps the existing engines:

* Structured facts live in the temporal knowledge graph
  (``flowly/memory/knowledge_graph.py``). A governance item of ``kind='fact'``
  references a KG triple via ``ref_kind='kg_triple'`` + ``ref_id=<triple id>``.
* Free-form preferences/notes live in ``MEMORY.md`` / daily notes. A governance
  item references them via ``ref_kind='memory_md'`` (``ref_id`` = content anchor)
  or carries the text inline (``ref_kind='inline'``).

What the governance layer owns and the underlying engines do not:

* **Lifecycle** — a status machine (candidate → active → needs_review → stale →
  superseded → rejected) with *enforced* transitions and an append-only audit
  trail. Illegal transitions raise rather than silently corrupt state.
* **Calibrated confidence**, **message-level provenance** (which session /
  messages produced the item), **privacy level**, and **supersede links**.

Design mirrors ``flowly/board/store.py``: single in-process writer serialized by
an ``RLock``, synchronous SQLite, WAL + foreign keys, a persistent connection
usable from async handlers, the CLI, and tests alike. There is no claim/CAS/
heartbeat machinery because there are no competing writer *processes* — the
dreamer, self-review, and live ``memory_append`` all run in-process and funnel
through this single store.

Storage location: ``<state_dir>/memory_governance.sqlite3``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# --------------------------------------------------------------------------
# Status model + transition table
# --------------------------------------------------------------------------

STATUS_CANDIDATE = "candidate"
STATUS_ACTIVE = "active"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_STALE = "stale"
STATUS_SUPERSEDED = "superseded"
STATUS_REJECTED = "rejected"

VALID_STATUSES = frozenset({
    STATUS_CANDIDATE,
    STATUS_ACTIVE,
    STATUS_NEEDS_REVIEW,
    STATUS_STALE,
    STATUS_SUPERSEDED,
    STATUS_REJECTED,
})

# Allowed transitions only. Anything not listed raises InvalidTransition.
# - candidate can be auto-activated, queued for review, rejected, or lose a
#   contradiction outright (→ superseded).
# - active facts can be superseded (newer wins), age to stale, be re-queued for
#   review, or rejected by the user.
# - stale/superseded can be brought back via user "undo" (→ active).
# - rejected is terminal (a hard "no"); reopen by creating a fresh candidate.
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_CANDIDATE: frozenset({
        STATUS_ACTIVE, STATUS_NEEDS_REVIEW, STATUS_REJECTED, STATUS_SUPERSEDED,
    }),
    STATUS_NEEDS_REVIEW: frozenset({
        STATUS_ACTIVE, STATUS_REJECTED, STATUS_STALE,
    }),
    STATUS_ACTIVE: frozenset({
        STATUS_SUPERSEDED, STATUS_STALE, STATUS_NEEDS_REVIEW, STATUS_REJECTED,
    }),
    STATUS_STALE: frozenset({
        STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_REJECTED,
    }),
    STATUS_SUPERSEDED: frozenset({STATUS_ACTIVE}),  # undo
    STATUS_REJECTED: frozenset(),                   # terminal
}

# Item kinds. `fact` items reference the KG; the rest are free-form/inline.
VALID_KINDS = frozenset({
    "profile", "preference", "project", "environment",
    "correction", "temporal", "relationship", "procedure", "fact",
})

VALID_REF_KINDS = frozenset({"kg_triple", "memory_md", "inline", "obsidian_note"})
VALID_PRIVACY = frozenset({"normal", "sensitive", "secret"})

# Actors recorded in the audit trail (free-form, but these are the canonical set).
ACTOR_DREAMER = "dreamer"
ACTOR_USER = "user"
ACTOR_SYSTEM = "system"
ACTOR_MIGRATION = "migration"


class GovernanceError(Exception):
    """Base error for governance store operations."""


class InvalidTransition(GovernanceError):
    """Raised when a status transition is not permitted by the state machine."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_item_id() -> str:
    return "m_" + uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------
# Row type
# --------------------------------------------------------------------------


@dataclass
class MemoryItem:
    id: str
    kind: str
    text: str
    status: str = STATUS_CANDIDATE
    ref_kind: str = "inline"
    ref_id: Optional[str] = None
    normalized_key: str = ""
    confidence: float = 0.0
    privacy_level: str = "normal"
    source_session: str = ""
    source_message_ids: list[str] = field(default_factory=list)
    supersedes: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    last_seen_at: Optional[str] = None
    last_used_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    # Free-form JSON metadata. Used by source adapters (e.g. Obsidian) to carry
    # provenance (vault path, line range) and a pending KG payload for facts.
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # camelCase mirror for desktop/web clients (Python keeps snake_case).
        d["refKind"] = self.ref_kind
        d["refId"] = self.ref_id
        d["normalizedKey"] = self.normalized_key
        d["privacyLevel"] = self.privacy_level
        d["sourceSession"] = self.source_session
        d["sourceMessageIds"] = self.source_message_ids
        d["validFrom"] = self.valid_from
        d["validTo"] = self.valid_to
        d["lastSeenAt"] = self.last_seen_at
        d["lastUsedAt"] = self.last_used_at
        d["createdAt"] = self.created_at
        d["updatedAt"] = self.updated_at
        return d


@dataclass
class AuditEntry:
    id: int
    item_id: str
    from_status: Optional[str]
    to_status: str
    actor: str
    reason: str
    at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_items (
    id                 TEXT PRIMARY KEY,
    kind               TEXT NOT NULL,
    text               TEXT NOT NULL,
    status             TEXT NOT NULL,
    ref_kind           TEXT NOT NULL DEFAULT 'inline',
    ref_id             TEXT,
    normalized_key     TEXT NOT NULL DEFAULT '',
    confidence         REAL NOT NULL DEFAULT 0.0,
    privacy_level      TEXT NOT NULL DEFAULT 'normal',
    source_session     TEXT NOT NULL DEFAULT '',
    source_message_ids TEXT NOT NULL DEFAULT '[]',
    supersedes         TEXT REFERENCES memory_items(id),
    valid_from         TEXT,
    valid_to           TEXT,
    last_seen_at       TEXT,
    last_used_at       TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    metadata           TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);

-- Generic key/value for watermarks + the dreamer advisory lock (used in P2).
CREATE TABLE IF NOT EXISTS memory_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Trust feedback: helpful/unhelpful signals on recalled items (F2).
CREATE TABLE IF NOT EXISTS memory_feedback (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id  TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    helpful  INTEGER NOT NULL,
    note     TEXT NOT NULL DEFAULT '',
    given_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_status ON memory_items(status);
CREATE INDEX IF NOT EXISTS idx_items_kind ON memory_items(kind);
CREATE INDEX IF NOT EXISTS idx_items_key ON memory_items(normalized_key);
CREATE INDEX IF NOT EXISTS idx_items_ref ON memory_items(ref_kind, ref_id);
CREATE INDEX IF NOT EXISTS idx_items_privacy ON memory_items(privacy_level);
CREATE INDEX IF NOT EXISTS idx_audit_item ON memory_audit(item_id);
CREATE INDEX IF NOT EXISTS idx_feedback_item ON memory_feedback(item_id);
"""

_ITEM_COLUMNS = (
    "id, kind, text, status, ref_kind, ref_id, normalized_key, confidence, "
    "privacy_level, source_session, source_message_ids, supersedes, "
    "valid_from, valid_to, last_seen_at, last_used_at, created_at, updated_at, "
    "metadata"
)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """One-time, idempotent column additions for stores created before a
    column existed. SQLite has no migration framework here — the base schema
    is ``CREATE TABLE IF NOT EXISTS`` — so evolving columns are added via
    guarded ``ALTER TABLE``.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
    if "metadata" not in cols:
        conn.execute(
            "ALTER TABLE memory_items ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'"
        )


class GovernanceStore:
    """Single-writer memory governance store. Thread-safe via an internal lock."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False: aiohttp handlers / tool coroutines may touch
        # the store from the event-loop thread while tests/CLI use the main
        # thread. The RLock serializes all access regardless.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            _migrate_schema(self._conn)
            self._conn.commit()
        logger.debug(f"[memory-gov] store ready at {self.db_path}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> MemoryItem:
        try:
            msg_ids = json.loads(row["source_message_ids"] or "[]")
            if not isinstance(msg_ids, list):
                msg_ids = []
        except (ValueError, TypeError):
            msg_ids = []
        try:
            keys = row.keys()
        except Exception:
            keys = []
        meta_raw = row["metadata"] if "metadata" in keys else None
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
            if not isinstance(meta, dict):
                meta = {}
        except (ValueError, TypeError):
            meta = {}
        return MemoryItem(
            id=row["id"],
            kind=row["kind"],
            text=row["text"],
            status=row["status"],
            ref_kind=row["ref_kind"] or "inline",
            ref_id=row["ref_id"],
            normalized_key=row["normalized_key"] or "",
            confidence=row["confidence"],
            privacy_level=row["privacy_level"] or "normal",
            source_session=row["source_session"] or "",
            source_message_ids=msg_ids,
            supersedes=row["supersedes"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            last_seen_at=row["last_seen_at"],
            last_used_at=row["last_used_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=meta,
        )

    def _get_item_locked(self, item_id: str) -> Optional[MemoryItem]:
        cur = self._conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM memory_items WHERE id = ?", (item_id,)
        )
        row = cur.fetchone()
        return self._row_to_item(row) if row else None

    def _audit_locked(
        self,
        item_id: str,
        from_status: Optional[str],
        to_status: str,
        actor: str,
        reason: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO memory_audit (item_id, from_status, to_status, actor, reason, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, from_status, to_status, actor, reason, _now_iso()),
        )

    # -- writes -------------------------------------------------------------

    def add_item(
        self,
        *,
        kind: str,
        text: str,
        status: str = STATUS_CANDIDATE,
        ref_kind: str = "inline",
        ref_id: Optional[str] = None,
        normalized_key: str = "",
        confidence: float = 0.0,
        privacy_level: str = "normal",
        source_session: str = "",
        source_message_ids: Optional[list[str]] = None,
        supersedes: Optional[str] = None,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        actor: str = ACTOR_SYSTEM,
        reason: str = "created",
    ) -> MemoryItem:
        if kind not in VALID_KINDS:
            raise GovernanceError(f"invalid kind: {kind!r}")
        if status not in VALID_STATUSES:
            raise GovernanceError(f"invalid status: {status!r}")
        if ref_kind not in VALID_REF_KINDS:
            raise GovernanceError(f"invalid ref_kind: {ref_kind!r}")
        if privacy_level not in VALID_PRIVACY:
            raise GovernanceError(f"invalid privacy_level: {privacy_level!r}")

        now = _now_iso()
        item = MemoryItem(
            id=_new_item_id(),
            kind=kind,
            text=text,
            status=status,
            ref_kind=ref_kind,
            ref_id=ref_id,
            normalized_key=normalized_key,
            confidence=confidence,
            privacy_level=privacy_level,
            source_session=source_session,
            source_message_ids=list(source_message_ids or []),
            supersedes=supersedes,
            valid_from=valid_from,
            valid_to=valid_to,
            last_seen_at=now,
            last_used_at=None,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._conn.execute(
                f"INSERT INTO memory_items ({_ITEM_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id, item.kind, item.text, item.status, item.ref_kind,
                    item.ref_id, item.normalized_key, item.confidence,
                    item.privacy_level, item.source_session,
                    json.dumps(item.source_message_ids), item.supersedes,
                    item.valid_from, item.valid_to, item.last_seen_at,
                    item.last_used_at, item.created_at, item.updated_at,
                    json.dumps(item.metadata),
                ),
            )
            self._audit_locked(item.id, None, item.status, actor, reason)
            self._conn.commit()
        return item

    def transition(
        self,
        item_id: str,
        to_status: str,
        *,
        actor: str = ACTOR_SYSTEM,
        reason: str = "",
        supersedes: Optional[str] = None,
    ) -> MemoryItem:
        """Move an item to ``to_status``, enforcing the transition table.

        Raises ``InvalidTransition`` if the move is not allowed, ``GovernanceError``
        if the item is missing or the target status is unknown. When activating an
        item that replaces another, pass ``supersedes`` to record the link (and the
        caller is responsible for transitioning the loser to ``superseded``).
        """
        if to_status not in VALID_STATUSES:
            raise GovernanceError(f"invalid status: {to_status!r}")
        with self._lock:
            item = self._get_item_locked(item_id)
            if item is None:
                raise GovernanceError(f"item not found: {item_id}")
            if to_status == item.status:
                return item  # idempotent no-op, no audit noise
            allowed = _TRANSITIONS.get(item.status, frozenset())
            if to_status not in allowed:
                raise InvalidTransition(
                    f"{item.status} → {to_status} is not allowed "
                    f"(item {item_id}); allowed: {sorted(allowed) or 'none'}"
                )
            now = _now_iso()
            if supersedes is not None:
                self._conn.execute(
                    "UPDATE memory_items SET status=?, supersedes=?, updated_at=? WHERE id=?",
                    (to_status, supersedes, now, item_id),
                )
            else:
                self._conn.execute(
                    "UPDATE memory_items SET status=?, updated_at=? WHERE id=?",
                    (to_status, now, item_id),
                )
            self._audit_locked(item_id, item.status, to_status, actor, reason)
            self._conn.commit()
            return self._get_item_locked(item_id)  # type: ignore[return-value]

    def update_fields(self, item_id: str, **fields: Any) -> MemoryItem:
        """Update non-status fields (text, confidence, normalized_key, privacy_level,
        valid_from/to, ref_*). Status changes must go through ``transition``."""
        allowed_cols = {
            "text", "confidence", "normalized_key", "privacy_level",
            "ref_kind", "ref_id", "valid_from", "valid_to",
            "last_seen_at", "last_used_at", "source_session",
        }
        sets, params = [], []
        for col, val in fields.items():
            if col == "source_message_ids":
                sets.append("source_message_ids=?")
                params.append(json.dumps(list(val or [])))
                continue
            if col not in allowed_cols:
                raise GovernanceError(f"field not updatable here: {col!r}")
            if col == "privacy_level" and val not in VALID_PRIVACY:
                raise GovernanceError(f"invalid privacy_level: {val!r}")
            if col == "ref_kind" and val not in VALID_REF_KINDS:
                raise GovernanceError(f"invalid ref_kind: {val!r}")
            sets.append(f"{col}=?")
            params.append(val)
        if not sets:
            got = self.get_item(item_id)
            if got is None:
                raise GovernanceError(f"item not found: {item_id}")
            return got
        sets.append("updated_at=?")
        params.append(_now_iso())
        params.append(item_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE memory_items SET {', '.join(sets)} WHERE id=?", params
            )
            if cur.rowcount == 0:
                raise GovernanceError(f"item not found: {item_id}")
            self._conn.commit()
            return self._get_item_locked(item_id)  # type: ignore[return-value]

    def touch_seen(self, item_id: str) -> None:
        """Bump last_seen_at — the dreamer's repetition signal."""
        with self._lock:
            self._conn.execute(
                "UPDATE memory_items SET last_seen_at=? WHERE id=?",
                (_now_iso(), item_id),
            )
            self._conn.commit()

    def touch_used(self, item_id: str) -> None:
        """Bump last_used_at — recorded when an item is actually recalled."""
        with self._lock:
            self._conn.execute(
                "UPDATE memory_items SET last_used_at=? WHERE id=?",
                (_now_iso(), item_id),
            )
            self._conn.commit()

    def add_feedback(self, item_id: str, helpful: bool, note: str = "") -> None:
        """Record a helpful/unhelpful trust signal on a recalled item (F2)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory_feedback (item_id, helpful, note, given_at) "
                "VALUES (?, ?, ?, ?)",
                (item_id, 1 if helpful else 0, note, _now_iso()),
            )
            self._conn.commit()

    def feedback_counts(self, item_id: str) -> tuple[int, int]:
        """Return (helpful_count, unhelpful_count) for an item."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(helpful),0), COALESCE(SUM(1-helpful),0) "
                "FROM memory_feedback WHERE item_id=?",
                (item_id,),
            ).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    # -- reads --------------------------------------------------------------

    def get_item(self, item_id: str) -> Optional[MemoryItem]:
        with self._lock:
            return self._get_item_locked(item_id)

    def list_items(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        ref_kind: Optional[str] = None,
        privacy_level: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[MemoryItem]:
        where, params = [], []
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if ref_kind is not None:
            where.append("ref_kind = ?")
            params.append(ref_kind)
        if privacy_level is not None:
            where.append("privacy_level = ?")
            params.append(privacy_level)
        sql = f"SELECT {_ITEM_COLUMNS} FROM memory_items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_item(r) for r in rows]

    def find_by_key(
        self, normalized_key: str, *, statuses: Optional[set[str]] = None
    ) -> list[MemoryItem]:
        """Items sharing a normalized key — the reconcile/contradiction lookup."""
        if not normalized_key:
            return []
        sql = f"SELECT {_ITEM_COLUMNS} FROM memory_items WHERE normalized_key = ?"
        params: list[Any] = [normalized_key]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(sorted(statuses))
        sql += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_item(r) for r in rows]

    def find_by_ref(self, ref_kind: str, ref_id: str) -> list[MemoryItem]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM memory_items WHERE ref_kind=? AND ref_id=?",
                (ref_kind, ref_id),
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def audit_log(self, item_id: str) -> list[AuditEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, item_id, from_status, to_status, actor, reason, at "
                "FROM memory_audit WHERE item_id=? ORDER BY id ASC",
                (item_id,),
            ).fetchall()
        return [
            AuditEntry(
                id=r["id"], item_id=r["item_id"], from_status=r["from_status"],
                to_status=r["to_status"], actor=r["actor"], reason=r["reason"],
                at=r["at"],
            )
            for r in rows
        ]

    def stats(self) -> dict[str, Any]:
        """Counts by status and kind — feeds the ``memory stats`` surface (P4)."""
        with self._lock:
            by_status = {
                r["status"]: r["n"]
                for r in self._conn.execute(
                    "SELECT status, COUNT(*) AS n FROM memory_items GROUP BY status"
                ).fetchall()
            }
            by_kind = {
                r["kind"]: r["n"]
                for r in self._conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM memory_items GROUP BY kind"
                ).fetchall()
            }
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM memory_items"
            ).fetchone()["n"]
        return {
            "total": total,
            "by_status": by_status,
            "by_kind": by_kind,
            "review_queue": by_status.get(STATUS_NEEDS_REVIEW, 0),
            "active": by_status.get(STATUS_ACTIVE, 0),
        }

    # -- meta kv (watermark + dreamer lock land here in P2) -----------------

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM memory_meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()
