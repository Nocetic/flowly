"""Restart-safe conversation events derived from the durable message archive.

Each scan atomically reconciles archive identities into a bounded SQLite event
journal. A persisted baseline prevents history replay at startup; stable message
ids prevent replay after compaction. Cursors identify the journal generation and
query scope, and retention gaps are explicit rather than silent message loss.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from flowly.mcp.server.readplane import SessionReader, _valid_session_key


class EventJournal:
    def __init__(
        self, reader: SessionReader, path: Path | None = None, *, max_events: int = 10_000,
    ) -> None:
        from flowly.profile import get_flowly_home

        self.reader = reader
        self.path = path or get_flowly_home() / "mcp" / "conversation-events.sqlite"
        self.max_events = max(1, max_events)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (key TEXT PRIMARY KEY, signature TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS seen (
                    session_key TEXT NOT NULL, message_id TEXT NOT NULL,
                    PRIMARY KEY(session_key, message_id));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL, message_id TEXT, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_session ON events(session_key, id);
            """)
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('generation', ?)", (uuid.uuid4().hex,))
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('floor', '0')")
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _head(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        return int(row[0]) if row else 0

    def _sync(self, conn: sqlite3.Connection) -> None:
        self.reader._ensure()
        manager = self.reader._manager
        baseline = conn.execute("SELECT value FROM meta WHERE key='baseline'").fetchone()
        old = {row["key"]: row["signature"] for row in conn.execute("SELECT * FROM sources")}
        current = {row["key"] for row in manager.list_sessions()}
        for key in old.keys() - current:
            # Deleted conversations must not remain readable in retained events.
            conn.execute("DELETE FROM events WHERE session_key=?", (key,))
            conn.execute("DELETE FROM seen WHERE session_key=?", (key,))
            conn.execute("DELETE FROM sources WHERE key=?", (key,))
            conn.execute(
                "INSERT INTO events(session_key,payload) VALUES (?,?)",
                (key, json.dumps({"type": "session_deleted", "session_key": key})),
            )
        for key in sorted(current):
            signature = json.dumps(self.reader.source_signature(key))
            if old.get(key) == signature:
                continue
            messages = self.reader.read_messages(key)
            if messages is None or signature != json.dumps(self.reader.source_signature(key)):
                continue  # Concurrent writer: retry a coherent snapshot on the next scan.
            visible_ids = {message["message_id"] for message in messages}
            for row in conn.execute(
                "SELECT id,message_id FROM events WHERE session_key=? AND message_id IS NOT NULL", (key,),
            ).fetchall():
                if row["message_id"] not in visible_ids:
                    conn.execute("DELETE FROM events WHERE id=?", (row["id"],))
            for message in messages:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO seen VALUES (?,?)", (key, message["message_id"]),
                ).rowcount
                if inserted and baseline:
                    payload = {"type": "message", "session_key": key, **message}
                    conn.execute(
                        "INSERT INTO events(session_key,message_id,payload) VALUES (?,?,?)",
                        (key, message["message_id"], json.dumps(payload, ensure_ascii=False)),
                    )
            conn.execute(
                "INSERT INTO sources VALUES (?,?) ON CONFLICT(key) DO UPDATE SET signature=excluded.signature",
                (key, signature),
            )
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('baseline','1')")
        floor = max(0, self._head(conn) - self.max_events)
        conn.execute("DELETE FROM events WHERE id<=?", (floor,))
        conn.execute("UPDATE meta SET value=? WHERE key='floor'", (str(floor),))

    @staticmethod
    def _cursor(position: int, generation: str, scope: str) -> str:
        raw = json.dumps({"v": 1, "position": position, "generation": generation, "scope": scope})
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    def poll(self, after_cursor: str | None = None, session_key: str | None = None, limit: int = 20) -> dict:
        if session_key is not None and _valid_session_key(session_key) != session_key:
            return {"error": "Invalid session_key"}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            return {"error": "limit must be an integer between 1 and 200"}
        scope = session_key or ""
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self._sync(conn)
            meta = dict(conn.execute("SELECT key,value FROM meta"))
            generation = meta["generation"]
            head = self._head(conn)
            if after_cursor is None:
                return {"events": [], "next_cursor": self._cursor(head, generation, scope), "has_more": False}
            try:
                if not isinstance(after_cursor, str) or len(after_cursor) > 2048:
                    raise ValueError
                decoded = json.loads(base64.b64decode(
                    after_cursor + "=" * (-len(after_cursor) % 4), altchars=b"-_", validate=True,
                ))
                position = decoded["position"]
                if (decoded.get("v") != 1 or decoded.get("scope") != scope
                        or type(position) is not int or position < 0):
                    raise ValueError
            except (ValueError, TypeError, KeyError, UnicodeError):
                return {"error": "Invalid cursor or cursor belongs to a different session filter"}
            if decoded.get("generation") != generation or position < int(meta["floor"]) or position > head:
                return {"error": "Event cursor expired; refresh conversation history and resume with next_cursor",
                        "code": "CURSOR_EXPIRED", "gap": True,
                        "next_cursor": self._cursor(head, generation, scope)}
            sql = "SELECT id,payload FROM events WHERE id>?"
            args: list[Any] = [position]
            if session_key is not None:
                sql += " AND session_key=?"
                args.append(session_key)
            rows = conn.execute(sql + " ORDER BY id LIMIT ?", (*args, limit + 1)).fetchall()
            has_more = len(rows) > limit
            selected = rows[:limit]
            next_position = selected[-1]["id"] if has_more else head
            return {
                "events": [dict(json.loads(row["payload"]), cursor=self._cursor(row["id"], generation, scope))
                           for row in selected],
                "next_cursor": self._cursor(next_position, generation, scope),
                "has_more": has_more,
            }

    async def wait(
        self, after_cursor: str | None = None, session_key: str | None = None,
        timeout_ms: int = 30_000,
    ) -> dict:
        if type(timeout_ms) is not int or not 0 <= timeout_ms <= 300_000:
            return {"error": "timeout_ms must be an integer between 0 and 300000"}
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        cursor = after_cursor
        while True:
            result = await asyncio.to_thread(self.poll, cursor, session_key)
            if result.get("error") or result.get("events"):
                return result
            cursor = result["next_cursor"]
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {**result, "reason": "timeout"}
            await asyncio.sleep(min(0.2, remaining))
