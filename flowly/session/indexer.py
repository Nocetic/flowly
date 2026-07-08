"""Session search index — SQLite FTS5 over conversation history.

Maintains a full-text index of all session messages so the agent can
recall past conversations.  The index is a *derived copy* — the
canonical data remains in the JSONL session files.  If the index DB
is deleted it will be rebuilt on next startup.

DB location: ``~/.flowly/session_index.sqlite``
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from loguru import logger

def _default_db_path() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "session_index.sqlite"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    key        TEXT PRIMARY KEY,
    created_at REAL,
    updated_at REAL,
    msg_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   REAL NOT NULL,
    FOREIGN KEY (session_key) REFERENCES sessions(key)
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_key, timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.id, old.content);
END;
"""


def _sanitize_fts5_query(query: str) -> str:
    """Sanitize user input for safe FTS5 MATCH queries.

    Strips special FTS5 characters, wraps hyphenated terms, and removes
    dangling boolean operators so the query never causes OperationalError.
    """
    if not query:
        return ""
    # Preserve balanced double-quoted phrases
    quoted: list[str] = []

    def _keep(m: re.Match) -> str:
        quoted.append(m.group(0))
        return f"\x00Q{len(quoted) - 1}\x00"

    s = re.sub(r'"[^"]*"', _keep, query)
    # Strip FTS5 specials
    s = re.sub(r'[+{}()\"^]', " ", s)
    s = re.sub(r"\*+", "*", s)
    s = re.sub(r"(^|\s)\*", r"\1", s)
    # Remove dangling boolean operators
    s = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", s.strip())
    s = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", s.strip())
    # Wrap hyphenated terms
    s = re.sub(r"\b(\w+(?:-\w+)+)\b", r'"\1"', s)
    # Restore quoted phrases
    for i, q in enumerate(quoted):
        s = s.replace(f"\x00Q{i}\x00", q)
    # Auto-prefix: append * to bare words for agglutinative languages
    # (Turkish: anne → annemin, Docker → Dockerfile, etc.)
    # Skips quoted phrases, words already ending in *, and boolean operators
    words = s.strip().split()
    prefixed = []
    in_quote = False
    for w in words:
        if w.startswith('"'):
            in_quote = True
        if in_quote:
            prefixed.append(w)
            if w.endswith('"'):
                in_quote = False
        elif w.endswith('*') or w.upper() in ("AND", "OR", "NOT"):
            prefixed.append(w)
        else:
            prefixed.append(w + "*")
    return " ".join(prefixed)


class SessionIndexer:
    """SQLite FTS5 index over session messages."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or _default_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    # ── Indexing ───────────────────────────────────────────────────

    def index_session(self, key: str, messages: list[dict[str, Any]]) -> None:
        """Index (or re-index) a session's messages, preserving row ids.

        INCREMENTAL by design. Sessions are saved by fully rewriting the
        canonical jsonl on every turn, so a naive delete-all + reinsert
        reassigned every message a NEW autoincrement id on every save AND on
        every startup rebuild. That churned the ids the cross-session memory
        dreamer watermarks against (``messages.id``), so old messages kept
        reappearing as "new" and the dreamer reprocessed the whole history
        forever (the "proposes memories with no conversation" bug).

        Fix: only APPEND genuinely-new tail messages, leaving existing rows —
        and their ids — untouched. This makes the common append-only save (and
        the whole startup rebuild) idempotent: unchanged sessions touch no rows.
        A DELETE+reinsert happens only when the stored prefix DIVERGES from the
        incoming one (history compacted or a message edited in place), and only
        for that one session — so at most its recent tail is re-id'd, not the
        entire store.
        """
        now = time.time()
        content_messages = [
            m for m in messages
            if m.get("content") and m.get("role") in ("user", "assistant")
        ]
        try:
            with self._conn:
                existing = self._conn.execute(
                    "SELECT role, content FROM messages WHERE session_key = ? "
                    "ORDER BY id",
                    (key,),
                ).fetchall()

                # Longest common prefix by (role, content). Timestamps are
                # excluded from the match — a missing-timestamp fallback must
                # not force a needless reindex.
                common = 0
                for old, new in zip(existing, content_messages):
                    if old["role"] == new.get("role") and old["content"] == new.get("content"):
                        common += 1
                    else:
                        break

                if common < len(existing):
                    # Divergence (compaction rewrote the head, or a message was
                    # edited): re-index this session only. New rows get fresh
                    # high ids, so the dreamer reprocesses just this tail once.
                    self._conn.execute(
                        "DELETE FROM messages WHERE session_key = ?", (key,)
                    )
                    to_insert = content_messages
                else:
                    # Existing rows are a prefix of the incoming set → append
                    # only the new tail, preserving every existing id.
                    to_insert = content_messages[common:]

                for m in to_insert:
                    self._conn.execute(
                        "INSERT INTO messages (session_key, role, content, timestamp) "
                        "VALUES (?, ?, ?, ?)",
                        (key, m["role"], m["content"], self._parse_ts(m, now)),
                    )

                created_at = now
                if content_messages:
                    created_at = self._parse_ts(content_messages[0], now)
                self._conn.execute(
                    "INSERT INTO sessions (key, created_at, updated_at, msg_count) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "updated_at=excluded.updated_at, msg_count=excluded.msg_count",
                    (key, created_at, now, len(content_messages)),
                )
        except Exception as e:
            logger.debug("Session index failed for {}: {}", key, e)

    @staticmethod
    def _parse_ts(msg: dict[str, Any], fallback: float) -> float:
        """Message timestamp as epoch seconds, or ``fallback`` if unparseable."""
        from datetime import datetime
        try:
            return datetime.fromisoformat(msg.get("timestamp", "")).timestamp()
        except (ValueError, TypeError):
            return fallback

    # ── Search ─────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 20,
        exclude_session: str | None = None,
        context_window: int = 3,
        include_bookends: bool = True,
        bookend_count: int = 3,
    ) -> list[dict[str, Any]]:
        """Full-text search across all session messages.

        Returns matches with snippet, rank, session_key, role, timestamp,
        ±``context_window`` surrounding context messages, an ``anchor_id``
        the caller can use to scroll deeper, and optional bookends (first
        + last few user/assistant messages of the session).
        """
        q = _sanitize_fts5_query(query)
        if not q:
            return []

        params: list[Any] = [q]
        exclude_clause = ""
        if exclude_session:
            exclude_clause = "AND m.session_key != ?"
            params.append(exclude_session)
        params.append(limit)

        sql = f"""
            SELECT
                m.id,
                m.session_key,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.timestamp,
                s.created_at AS session_created,
                s.msg_count
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.key = m.session_key
            WHERE messages_fts MATCH ?
            {exclude_clause}
            ORDER BY rank
            LIMIT ?
        """
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

        results = []
        seen_sessions: set[str] = set()
        for row in rows:
            r = dict(row)
            sk = r["session_key"]
            # Deduplicate by session (show best match per session)
            if sk in seen_sessions:
                continue
            seen_sessions.add(sk)
            # ±context_window surrounding messages — wider than the
            # original ±1 so the agent gets enough turn-shape to judge
            # relevance without a follow-up scroll.
            anchor_id = r["id"]
            try:
                ctx = self._conn.execute(
                    "SELECT id, role, content FROM messages "
                    "WHERE session_key = ? AND id BETWEEN ? AND ? "
                    "ORDER BY id",
                    (sk, anchor_id - context_window, anchor_id + context_window),
                ).fetchall()
                r["context"] = [
                    {"role": c["role"], "content": (c["content"] or "")[:300]}
                    for c in ctx
                ]
            except Exception:
                r["context"] = []
            # Expose the anchor message id so the caller can scroll
            # via session_search's scroll mode (session_id + around_message_id).
            r["anchor_id"] = anchor_id
            if include_bookends:
                try:
                    bookends = self.session_bookends(sk, count=bookend_count)
                    r["bookend_start"] = [
                        {"role": m["role"], "content": (m["content"] or "")[:300]}
                        for m in bookends["start"]
                    ]
                    r["bookend_end"] = [
                        {"role": m["role"], "content": (m["content"] or "")[:300]}
                        for m in bookends["end"]
                    ]
                except Exception:
                    r["bookend_start"] = []
                    r["bookend_end"] = []
            r.pop("id", None)
            results.append(r)

        return results

    # ── Recent sessions ────────────────────────────────────────────

    def list_recent(
        self,
        limit: int = 10,
        exclude_session: str | None = None,
    ) -> list[dict[str, Any]]:
        """List most recent sessions with a preview (no FTS, no LLM)."""
        params: list[Any] = []
        exclude = ""
        if exclude_session:
            exclude = "WHERE s.key != ?"
            params.append(exclude_session)
        params.append(limit)

        sql = f"""
            SELECT
                s.key,
                s.created_at,
                s.updated_at,
                s.msg_count,
                (SELECT SUBSTR(m.content, 1, 80)
                 FROM messages m
                 WHERE m.session_key = s.key AND m.role = 'user'
                 ORDER BY m.timestamp LIMIT 1) AS preview
            FROM sessions s
            {exclude}
            ORDER BY s.updated_at DESC
            LIMIT ?
        """
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── Scroll / browse around an anchor message ───────────────────

    def messages_around(
        self,
        session_key: str,
        anchor_id: int,
        window: int = 5,
    ) -> dict[str, Any]:
        """Return ``window`` messages on each side of ``anchor_id`` from one session.

        Powers ``session_search``'s scroll mode — agent finds a hit in
        discovery, then drills into the surrounding conversation without
        another FTS round-trip. To paginate further, re-anchor on the
        first or last id of the returned window.

        Returns ``{window: [...], messages_before: N, messages_after: N}``
        where the counts are the remaining message totals on either side
        of the slice (so the caller can tell when scrolling has hit the
        session boundary).
        """
        if window < 1:
            window = 1
        try:
            rows_before = self._conn.execute(
                "SELECT id, role, content, timestamp FROM messages "
                "WHERE session_key = ? AND id <= ? "
                "ORDER BY id DESC LIMIT ?",
                (session_key, anchor_id, window + 1),
            ).fetchall()
            rows_after = self._conn.execute(
                "SELECT id, role, content, timestamp FROM messages "
                "WHERE session_key = ? AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (session_key, anchor_id, window),
            ).fetchall()
        except sqlite3.OperationalError:
            return {"window": [], "messages_before": 0, "messages_after": 0}

        # Anchor itself + up to ``window`` predecessors (DESC then flip)
        before = [dict(r) for r in reversed(rows_before)]
        after = [dict(r) for r in rows_after]
        slice_ = before + after
        if not slice_:
            return {"window": [], "messages_before": 0, "messages_after": 0}

        # Remaining-on-either-side counts for the agent's "can I scroll
        # further?" heuristic.
        try:
            first_id = slice_[0]["id"]
            last_id = slice_[-1]["id"]
            messages_before = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_key = ? AND id < ?",
                (session_key, first_id),
            ).fetchone()[0]
            messages_after = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_key = ? AND id > ?",
                (session_key, last_id),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            messages_before = 0
            messages_after = 0

        return {
            "window": slice_,
            "messages_before": messages_before,
            "messages_after": messages_after,
        }

    def session_bookends(
        self,
        session_key: str,
        count: int = 3,
    ) -> dict[str, list[dict[str, Any]]]:
        """First ``count`` and last ``count`` user/assistant messages.

        Folded into discovery results so the agent gets enough of the
        session's opening + closing to judge whether it's the right
        conversation, without a second tool call to fetch context.
        """
        if count < 1:
            count = 1
        try:
            start = self._conn.execute(
                "SELECT id, role, content, timestamp FROM messages "
                "WHERE session_key = ? "
                "ORDER BY id ASC LIMIT ?",
                (session_key, count),
            ).fetchall()
            end = self._conn.execute(
                "SELECT id, role, content, timestamp FROM messages "
                "WHERE session_key = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_key, count),
            ).fetchall()
        except sqlite3.OperationalError:
            return {"start": [], "end": []}
        return {
            "start": [dict(r) for r in start],
            "end": [dict(r) for r in list(reversed(end))],
        }

    def get_session_meta(self, session_key: str) -> dict[str, Any] | None:
        """Single-session metadata. Returns None when the key is unknown."""
        try:
            row = self._conn.execute(
                "SELECT key, created_at, updated_at, msg_count FROM sessions "
                "WHERE key = ?",
                (session_key,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(row) if row else None

    # ── Maintenance ────────────────────────────────────────────────

    def rebuild_from_sessions_dir(self, sessions_dir: Path) -> int:
        """Rebuild entire index from JSONL session files. Returns count."""
        import json as _json
        from flowly.session.manager import iter_session_files
        count = 0
        for path in iter_session_files(sessions_dir):
            try:
                messages = []
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        data = _json.loads(line)
                        if data.get("_type") == "metadata":
                            continue
                        messages.append(data)
                if messages:
                    key = path.stem.replace("_", ":", 1)
                    self.index_session(key, messages)
                    count += 1
            except Exception as e:
                logger.debug("Skipped {} during rebuild: {}", path.name, e)
        logger.info("Session index rebuilt: {} sessions", count)
        return count

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass
