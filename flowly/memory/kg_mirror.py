"""KG mirror — reflect governance supersede/undo into the temporal knowledge graph.

When a governance item that references a KG triple is superseded, the underlying
triple must be temporally closed (``valid_to`` set) so the KG stops reporting it
as current — without deleting history. Undo re-opens it.

This writes the ``triples`` table directly (a legitimate temporal close, exactly
what ``KnowledgeGraph.invalidate`` does, but addressable by triple id) so we don't
need to reconstruct subject/predicate/object from the governance item. The KG
module itself is left untouched.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from loguru import logger


class SqliteKGMirror:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def supersede(self, triple_id: str, ended: str | None = None) -> int:
        """Close a currently-valid triple. Returns rows affected (0 if already
        closed or missing). Idempotent — won't reopen-then-reclose."""
        ended = ended or date.today().isoformat()
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=5)
            try:
                cur = conn.execute(
                    "UPDATE triples SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
                    (ended, triple_id),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning(f"[kg-mirror] supersede({triple_id}) failed: {exc}")
            return 0

    def restore(self, triple_id: str) -> int:
        """Re-open a triple (undo). Returns rows affected."""
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=5)
            try:
                cur = conn.execute(
                    "UPDATE triples SET valid_to = NULL WHERE id = ?",
                    (triple_id,),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning(f"[kg-mirror] restore({triple_id}) failed: {exc}")
            return 0
