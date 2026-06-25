"""Skill operation log + rollback ledger.

Skill ops are auto-applied (like memory consolidation), so this is a HISTORY +
rollback record, not an approval queue. Single-writer SQLite (RLock, WAL) with an
append-only audit trail and a meta kv for miner/curator watermarks + advisory
locks + dirty flags + first-run-deferral seed — same shape as
flowly/memory/governance.py.

Storage: ``get_data_dir()/skill_governance.sqlite3`` (alongside the memory DB).
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

# op kinds
KIND_CREATE = "create"
KIND_MERGE = "merge"
KIND_ARCHIVE = "archive"
KIND_DEMOTE = "demote"
VALID_KINDS = frozenset({KIND_CREATE, KIND_MERGE, KIND_ARCHIVE, KIND_DEMOTE})

# statuses
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_UNDONE = "undone"
VALID_STATUSES = frozenset({STATUS_APPLIED, STATUS_FAILED, STATUS_UNDONE})
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_APPLIED: frozenset({STATUS_UNDONE}),
    STATUS_FAILED: frozenset(),
    STATUS_UNDONE: frozenset(),
}

ACTOR_MINER = "miner"
ACTOR_CURATOR = "curator"
ACTOR_USER = "user"
ACTOR_SYSTEM = "system"


class SkillOpError(Exception):
    pass


class InvalidTransition(SkillOpError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_op_id() -> str:
    return "so_" + uuid.uuid4().hex[:12]


@dataclass
class SkillOp:
    id: str
    kind: str
    status: str
    targets: list[str] = field(default_factory=list)
    draft_name: str = ""
    applied_content: str = ""
    applied_files: dict[str, str] = field(default_factory=dict)
    rationale: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    snapshot_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_ops (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    targets         TEXT NOT NULL DEFAULT '[]',
    draft_name      TEXT NOT NULL DEFAULT '',
    applied_content TEXT NOT NULL DEFAULT '',
    applied_files   TEXT NOT NULL DEFAULT '{}',
    rationale       TEXT NOT NULL DEFAULT '',
    evidence        TEXT NOT NULL DEFAULT '{}',
    snapshot_id     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_op_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id       TEXT NOT NULL REFERENCES skill_ops(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_skill_ops_status ON skill_ops(status);
CREATE INDEX IF NOT EXISTS idx_skill_ops_kind ON skill_ops(kind);
"""

_COLS = ("id, kind, status, targets, draft_name, applied_content, applied_files, "
         "rationale, evidence, snapshot_id, created_at, updated_at")


class SkillOpLog:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.debug(f"[skill-oplog] ready at {self.db_path}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> SkillOp:
        return SkillOp(
            id=r["id"], kind=r["kind"], status=r["status"],
            targets=json.loads(r["targets"] or "[]"),
            draft_name=r["draft_name"] or "",
            applied_content=r["applied_content"] or "",
            applied_files=json.loads(r["applied_files"] or "{}"),
            rationale=r["rationale"] or "",
            evidence=json.loads(r["evidence"] or "{}"),
            snapshot_id=r["snapshot_id"] or "",
            created_at=r["created_at"], updated_at=r["updated_at"],
        )

    def _audit(self, op_id: str, frm: Optional[str], to: str, actor: str, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO skill_op_audit (op_id, from_status, to_status, actor, reason, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (op_id, frm, to, actor, reason, _now_iso()),
        )

    def add_op(
        self, *, kind: str, status: str = STATUS_APPLIED, targets: Optional[list[str]] = None,
        draft_name: str = "", applied_content: str = "", applied_files: Optional[dict] = None,
        rationale: str = "", evidence: Optional[dict] = None, snapshot_id: str = "",
        actor: str = ACTOR_SYSTEM, reason: str = "applied",
    ) -> SkillOp:
        if kind not in VALID_KINDS:
            raise SkillOpError(f"invalid kind: {kind!r}")
        if status not in VALID_STATUSES:
            raise SkillOpError(f"invalid status: {status!r}")
        now = _now_iso()
        op = SkillOp(
            id=_new_op_id(), kind=kind, status=status, targets=list(targets or []),
            draft_name=draft_name, applied_content=applied_content,
            applied_files=dict(applied_files or {}), rationale=rationale,
            evidence=dict(evidence or {}), snapshot_id=snapshot_id,
            created_at=now, updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                f"INSERT INTO skill_ops ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (op.id, op.kind, op.status, json.dumps(op.targets), op.draft_name,
                 op.applied_content, json.dumps(op.applied_files), op.rationale,
                 json.dumps(op.evidence), op.snapshot_id, op.created_at, op.updated_at),
            )
            self._audit(op.id, None, op.status, actor, reason)
            self._conn.commit()
        return op

    def transition(self, op_id: str, to: str, *, actor: str = ACTOR_SYSTEM, reason: str = "") -> SkillOp:
        if to not in VALID_STATUSES:
            raise SkillOpError(f"invalid status: {to!r}")
        with self._lock:
            cur = self._conn.execute(f"SELECT {_COLS} FROM skill_ops WHERE id=?", (op_id,)).fetchone()
            if cur is None:
                raise SkillOpError(f"op not found: {op_id}")
            op = self._row(cur)
            if to == op.status:
                return op
            if to not in _TRANSITIONS.get(op.status, frozenset()):
                raise InvalidTransition(f"{op.status} → {to} not allowed ({op_id})")
            self._conn.execute("UPDATE skill_ops SET status=?, updated_at=? WHERE id=?",
                               (to, _now_iso(), op_id))
            self._audit(op_id, op.status, to, actor, reason)
            self._conn.commit()
            return self.get(op_id)  # type: ignore[return-value]

    def get(self, op_id: str) -> Optional[SkillOp]:
        with self._lock:
            r = self._conn.execute(f"SELECT {_COLS} FROM skill_ops WHERE id=?", (op_id,)).fetchone()
        return self._row(r) if r else None

    def list_ops(self, *, status: Optional[str] = None, limit: Optional[int] = None) -> list[SkillOp]:
        sql = f"SELECT {_COLS} FROM skill_ops"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT value FROM skill_meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO skill_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._conn.commit()
