"""SQLite-backed artifact store with FTS5 search and version snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from loguru import logger


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL DEFAULT 'markdown',
    title          TEXT NOT NULL DEFAULT '',
    content        TEXT NOT NULL DEFAULT '',
    metadata       TEXT NOT NULL DEFAULT '{}',
    data_bindings  TEXT NOT NULL DEFAULT '[]',
    pinned         INTEGER NOT NULL DEFAULT 0,
    dashboard_size TEXT NOT NULL DEFAULT 'medium',
    version        INTEGER NOT NULL DEFAULT 1,
    tags           TEXT NOT NULL DEFAULT '[]',
    session_key    TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(type);
CREATE INDEX IF NOT EXISTS idx_artifacts_pinned ON artifacts(pinned) WHERE pinned = 1;
CREATE INDEX IF NOT EXISTS idx_artifacts_updated ON artifacts(updated_at DESC);

CREATE TABLE IF NOT EXISTS artifact_versions (
    id            TEXT PRIMARY KEY,
    artifact_id   TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    content       TEXT NOT NULL,
    data_bindings TEXT NOT NULL DEFAULT '[]',
    created_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_versions_artifact
    ON artifact_versions(artifact_id, version DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
    title, content, id UNINDEXED, tokenize = 'unicode61'
);
"""

_SCHEMA_VERSION = "1"

_VALID_TYPES = frozenset({
    "html", "svg", "markdown", "form", "chart",
    "csv", "json", "code", "mermaid", "latex",
})
_VALID_SIZES = frozenset({"small", "medium", "large", "full"})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _gen_id(prefix: str = "art") -> str:
    ts = int(time.time()).to_bytes(4, "big").hex()
    rand = os.urandom(4).hex()
    return f"{prefix}_{ts}_{rand}"


def _parse_json(value: Any, fallback: Any = None) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return fallback
    return value if value is not None else fallback


# ── Singleton ─────────────────────────────────────────────────────────────────

_CACHE: dict[str, "ArtifactStore"] = {}


def get_store(state_dir: Path | None = None) -> "ArtifactStore":
    """Get or create an ArtifactStore for the given state directory."""
    state_dir = state_dir or Path("~/.flowly").expanduser()
    key = str(state_dir)
    if key not in _CACHE:
        db_path = state_dir / "artifacts.sqlite"
        _CACHE[key] = ArtifactStore(db_path)
    return _CACHE[key]


# ── Store ─────────────────────────────────────────────────────────────────────

class ArtifactStore:
    """SQLite-backed artifact persistence with FTS5 and version history."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    # ── Init ──────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        cur = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        )
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta VALUES ('schema_version', ?)", (_SCHEMA_VERSION,)
            )
        self._conn.commit()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(
        self,
        type: str,
        title: str,
        content: str,
        metadata: dict | None = None,
        data_bindings: list | None = None,
        pinned: bool = False,
        dashboard_size: str = "medium",
        tags: list[str] | None = None,
        session_key: str | None = None,
    ) -> dict:
        """Create a new artifact. Returns the full artifact dict."""
        if type not in _VALID_TYPES:
            type = "markdown"
        if dashboard_size not in _VALID_SIZES:
            dashboard_size = "medium"

        artifact_id = _gen_id("art")
        now = time.time()
        metadata_json = json.dumps(metadata or {})
        bindings_json = json.dumps(data_bindings or [])
        tags_json = json.dumps(tags or [])

        with self._conn:
            self._conn.execute(
                """INSERT INTO artifacts
                   (id, type, title, content, metadata, data_bindings, pinned,
                    dashboard_size, version, tags, session_key, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (artifact_id, type, title, content, metadata_json, bindings_json,
                 1 if pinned else 0, dashboard_size, tags_json,
                 session_key, now, now),
            )
            self._fts_sync(artifact_id, title, content)

        logger.debug("Artifact created: {} ({})", artifact_id, type)
        return self.get(artifact_id)  # type: ignore[return-value]

    def get(self, artifact_id: str) -> dict | None:
        """Get a single artifact by ID."""
        cur = self._conn.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        )
        row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def update(
        self,
        artifact_id: str,
        title: str | None = None,
        content: str | None = None,
        metadata: dict | None = None,
        data_bindings: list | None = None,
        pinned: bool | None = None,
        dashboard_size: str | None = None,
        tags: list[str] | None = None,
    ) -> dict | None:
        """Update an artifact. Creates version snapshot if content changes."""
        existing = self.get(artifact_id)
        if not existing:
            return None

        version_bump = False
        now = time.time()

        with self._conn:
            # Snapshot old version if content is changing
            if content is not None and content != existing["content"]:
                ver_id = _gen_id("ver")
                self._conn.execute(
                    """INSERT INTO artifact_versions
                       (id, artifact_id, version, content, data_bindings, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (ver_id, artifact_id, existing["version"],
                     existing["content"], json.dumps(existing["data_bindings"]),
                     now),
                )
                version_bump = True

            # Build SET clause dynamically
            sets: list[str] = ["updated_at = ?"]
            params: list[Any] = [now]

            if title is not None:
                sets.append("title = ?")
                params.append(title)
            if content is not None:
                sets.append("content = ?")
                params.append(content)
            if metadata is not None:
                sets.append("metadata = ?")
                params.append(json.dumps(metadata))
            if data_bindings is not None:
                sets.append("data_bindings = ?")
                params.append(json.dumps(data_bindings))
            if pinned is not None:
                sets.append("pinned = ?")
                params.append(1 if pinned else 0)
            if dashboard_size is not None and dashboard_size in _VALID_SIZES:
                sets.append("dashboard_size = ?")
                params.append(dashboard_size)
            if tags is not None:
                sets.append("tags = ?")
                params.append(json.dumps(tags))
            if version_bump:
                sets.append("version = version + 1")

            params.append(artifact_id)
            self._conn.execute(
                f"UPDATE artifacts SET {', '.join(sets)} WHERE id = ?",
                params,
            )

            # Sync FTS if title or content changed
            new_title = title if title is not None else existing["title"]
            new_content = content if content is not None else existing["content"]
            if title is not None or content is not None:
                self._fts_sync(artifact_id, new_title, new_content)

        return self.get(artifact_id)

    def delete(self, artifact_id: str) -> bool:
        """Delete an artifact and all its versions."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM artifacts WHERE id = ?", (artifact_id,)
            )
            if cur.rowcount > 0:
                self._fts_delete(artifact_id)
                return True
        return False

    def list(
        self,
        type: str | None = None,
        pinned: bool | None = None,
        search: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List artifacts with optional filters. FTS5 for search.

        ``tags`` filter: returns rows whose tag list contains EVERY tag
        listed (AND, not OR). Tags are stored as a JSON array string in
        the `tags` column; we use SQLite's LIKE with a defensive quoted
        pattern that matches `"tag"` substrings. Cheap and good enough
        because tag values are slug-like and the column is small.
        """
        if search:
            return self._list_fts(search, type, pinned, tags, limit, offset)

        conditions: list[str] = []
        params: list[Any] = []

        if type is not None:
            conditions.append("type = ?")
            params.append(type)
        if pinned is not None:
            conditions.append("pinned = ?")
            params.append(1 if pinned else 0)
        if tags:
            for tag in tags:
                # Match the JSON-encoded form: "tag" with surrounding quotes.
                # json.dumps gives us proper escaping for unusual chars.
                conditions.append("tags LIKE ?")
                params.append(f"%{json.dumps(tag)}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        cur = self._conn.execute(
            f"SELECT * FROM artifacts {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [self._row_to_dict(row) for row in cur.fetchall()]

    def _list_fts(
        self,
        search: str,
        type: str | None,
        pinned: bool | None,
        tags: list[str] | None,
        limit: int,
        offset: int,
    ) -> list[dict]:
        """Full-text search via FTS5, with optional tag intersection."""
        # Escape and quote terms for FTS5
        terms = search.strip().split()
        if not terms:
            return self.list(type=type, pinned=pinned, tags=tags, limit=limit, offset=offset)
        fts_query = " OR ".join(f'"{t}"' for t in terms[:20])

        conditions = ["a.id = f.id"]
        params: list[Any] = [fts_query]

        if type is not None:
            conditions.append("a.type = ?")
            params.append(type)
        if pinned is not None:
            conditions.append("a.pinned = ?")
            params.append(1 if pinned else 0)
        if tags:
            for tag in tags:
                conditions.append("a.tags LIKE ?")
                params.append(f"%{json.dumps(tag)}%")

        where = " AND ".join(conditions)
        params.extend([limit, offset])

        cur = self._conn.execute(
            f"""SELECT a.* FROM artifacts_fts f
                JOIN artifacts a ON {where}
                WHERE artifacts_fts MATCH ?
                ORDER BY rank
                LIMIT ? OFFSET ?""",
            params,
        )
        return [self._row_to_dict(row) for row in cur.fetchall()]

    def pin(self, artifact_id: str, pinned: bool = True) -> dict | None:
        """Pin or unpin an artifact."""
        return self.update(artifact_id, pinned=pinned)

    def get_versions(self, artifact_id: str) -> list[dict]:
        """Get version history for an artifact, newest first."""
        cur = self._conn.execute(
            """SELECT * FROM artifact_versions
               WHERE artifact_id = ?
               ORDER BY version DESC""",
            (artifact_id,),
        )
        results = []
        for row in cur.fetchall():
            d = dict(row)
            d["data_bindings"] = _parse_json(d.get("data_bindings"), [])
            results.append(d)
        return results

    # ── FTS helpers ───────────────────────────────────────────────────────────

    def _fts_sync(self, artifact_id: str, title: str, content: str) -> None:
        """Sync FTS5 index for an artifact (DELETE + INSERT)."""
        self._conn.execute(
            "DELETE FROM artifacts_fts WHERE id = ?", (artifact_id,)
        )
        self._conn.execute(
            "INSERT INTO artifacts_fts (id, title, content) VALUES (?, ?, ?)",
            (artifact_id, title, content),
        )

    def _fts_delete(self, artifact_id: str) -> None:
        """Remove from FTS5 index."""
        self._conn.execute(
            "DELETE FROM artifacts_fts WHERE id = ?", (artifact_id,)
        )

    # ── Row conversion ────────────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a Row to dict, parsing JSON fields."""
        d = dict(row)
        d["metadata"] = _parse_json(d.get("metadata"), {})
        d["data_bindings"] = _parse_json(d.get("data_bindings"), [])
        d["tags"] = _parse_json(d.get("tags"), [])
        d["pinned"] = bool(d.get("pinned", 0))
        return d
