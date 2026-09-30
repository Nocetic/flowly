"""SQLite-backed artifact store with FTS5 search and version snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.live_voice.authority import current_request_owner
from flowly.session.control_access import SessionControlScope
from flowly.session.manager import session_file_lock
from flowly.session.ownership import SessionAccessError, is_owned_session, owner_metadata

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
CREATE INDEX IF NOT EXISTS idx_artifacts_session_updated
    ON artifacts(session_key, updated_at DESC);

CREATE TABLE IF NOT EXISTS artifact_session_outputs (
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    session_key TEXT NOT NULL,
    PRIMARY KEY (artifact_id, session_key)
);
CREATE INDEX IF NOT EXISTS idx_artifact_outputs_session
    ON artifact_session_outputs(session_key, artifact_id);

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

_SCHEMA_VERSION = "2"

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
_CACHE_LOCK = threading.RLock()


def get_store(state_dir: Path | None = None) -> "ArtifactStore":
    """Get or create an ArtifactStore for the given state directory."""
    from flowly.profile import get_flowly_home

    state_dir = state_dir or get_flowly_home()
    key = str(state_dir)
    with _CACHE_LOCK:
        if key not in _CACHE:
            db_path = state_dir / "artifacts.sqlite"
            _CACHE[key] = ArtifactStore(db_path)
        return _CACHE[key]


# ── Store ─────────────────────────────────────────────────────────────────────

class ArtifactStore:
    """SQLite-backed artifact persistence with FTS5 and version history."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._sessions_dir = db_path.parent / 'sessions'
        self._lock = threading.RLock()
        self._savepoint = 0
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.create_function('flowly_artifact_visible', 2, self._owner_visible)
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Init ──────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        with self._transaction():
            for table in ('artifacts', 'artifact_session_outputs'):
                columns = {row['name'] for row in self._conn.execute(f'PRAGMA table_info({table})')}
                if 'session_owner_json' not in columns:
                    self._conn.execute(f'ALTER TABLE {table} ADD COLUMN session_owner_json TEXT')
            self._conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (_SCHEMA_VERSION,)
            )

    @contextmanager
    def _transaction(self):
        """Serialize read-modify-write across threads and SQLite connections."""
        with self._lock:
            nested = self._conn.in_transaction
            self._savepoint += 1
            name = f'artifact_write_{self._savepoint}'
            self._conn.execute(f'SAVEPOINT {name}' if nested else 'BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                if nested:
                    self._conn.execute(f'ROLLBACK TO {name}')
                    self._conn.execute(f'RELEASE {name}')
                else:
                    self._conn.rollback()
                raise
            else:
                if nested:
                    self._conn.execute(f'RELEASE {name}')
                else:
                    self._conn.commit()

    def _record_scope(self, key, raw) -> SessionControlScope:
        try:
            owner = (json.loads(raw) if raw is not None else
                     {'kind': 'host'} if key and is_owned_session(key, {}) else None)
        except (TypeError, ValueError):
            raise SessionAccessError() from None
        if owner is None and key and is_owned_session(key, {}):
            raise SessionAccessError()
        return SessionControlScope.bind(key or None, owner, sessions_dir=self._sessions_dir)

    def _new_scope(self, key) -> SessionControlScope:
        if key is not None and not isinstance(key, str):
            raise SessionAccessError()
        owner = current_request_owner()
        if not key and (owner is None or owner.uid is None):
            # Ordinary host library entries retain their shared visibility.
            return SessionControlScope.bind(None, None)
        return SessionControlScope.capture(key, sessions_dir=self._sessions_dir)

    def _owner_visible(self, key, raw) -> int:
        """Cheap SQL prefilter; canonical authorization still happens under locks."""
        owner = current_request_owner()
        if owner is None:
            return 1
        try:
            scope = self._record_scope(key, raw)
        except SessionAccessError:
            return 0
        return int(scope.owner is None or scope.owner == owner_metadata(owner))

    @contextmanager
    def _scopes_guard(self, scopes):
        with ExitStack() as stack:
            try:
                paths = sorted({scope.path for scope in scopes if scope.path is not None})
                for path in paths:
                    stack.enter_context(session_file_lock(path))
                allowed = all(stack.enter_context(scope.guard(scope.key)) for scope in scopes)
            except (SessionAccessError, OSError):
                allowed = False
            yield allowed

    @contextmanager
    def _guard(self, artifact_id, *, session_key=None, membership=False, write=False, summary=False):
        # Discover immutable authority without holding the DB lock while
        # acquiring canonical locks. Recheck it inside the transaction.
        with self._lock:
            hint = self._conn.execute(
                'SELECT session_key, session_owner_json FROM artifacts WHERE id = ?', (artifact_id,),
            ).fetchone()
            link = (self._conn.execute(
                'SELECT session_owner_json FROM artifact_session_outputs WHERE artifact_id = ? AND session_key = ?',
                (artifact_id, session_key),
            ).fetchone() if session_key is not None else None)
        if hint is None or (membership and session_key != hint['session_key'] and link is None):
            yield None
            return
        try:
            scopes = [self._record_scope(hint['session_key'], hint['session_owner_json'])]
            target = None
            if session_key is not None:
                target = (self._record_scope(session_key, link['session_owner_json']) if link is not None
                          else scopes[0] if session_key == hint['session_key'] else self._new_scope(session_key))
                scopes.append(target)
        except SessionAccessError:
            yield None
            return
        with self._scopes_guard(scopes) as allowed:
            if not allowed:
                yield None
                return
            with self._lock, self._transaction() if write else nullcontext():
                columns = ('id, type, title, version, updated_at, tags, metadata, session_key, session_owner_json'
                           if summary else '*')
                row = self._conn.execute(f'SELECT {columns} FROM artifacts WHERE id = ?', (artifact_id,)).fetchone()
                if row is None or (row['session_key'], row['session_owner_json']) != tuple(hint):
                    yield None
                    return
                if target is not None:
                    current_link = self._conn.execute(
                        'SELECT session_owner_json FROM artifact_session_outputs WHERE artifact_id = ? AND session_key = ?',
                        (artifact_id, session_key),
                    ).fetchone()
                    try:
                        link_matches = (self._record_scope(session_key, current_link['session_owner_json']).owner
                                        == target.owner if current_link is not None else link is None)
                    except SessionAccessError:
                        link_matches = False
                    if not link_matches:
                        yield None
                        return
                yield row

    @contextmanager
    def output_guard(self, artifact_id: str, session_key: str | None):
        """Authorize an export before filesystem effects; no await inside."""
        with self._guard(artifact_id, session_key=session_key, write=True) as row:
            yield self._row_to_dict(row) if row is not None else None

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

        scope = self._new_scope(session_key)
        with self._scopes_guard([scope]) as allowed:
            if not allowed:
                raise SessionAccessError()
            with self._transaction():
                self._conn.execute(
                    """INSERT INTO artifacts
                       (id, type, title, content, metadata, data_bindings, pinned,
                        dashboard_size, version, tags, session_key, created_at, updated_at, session_owner_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
                    (artifact_id, type, title, content, metadata_json, bindings_json,
                     1 if pinned else 0, dashboard_size, tags_json,
                     scope.key, now, now, json.dumps(scope.owner)),
                )
                self._fts_sync(artifact_id, title, content)
                row = self._conn.execute('SELECT * FROM artifacts WHERE id = ?', (artifact_id,)).fetchone()
                result = self._row_to_dict(row)

        logger.debug("Artifact created: {} ({})", artifact_id, type)
        return result

    def get(self, artifact_id: str) -> dict | None:
        """Get a single artifact by ID."""
        with self._guard(artifact_id) as row:
            return self._row_to_dict(row) if row is not None else None

    def control_scope(self, artifact_id: str) -> SessionControlScope | None:
        """Read original event authority, not an authorization or public DTO."""
        with self._lock:
            row = self._conn.execute(
                'SELECT session_key, session_owner_json FROM artifacts WHERE id = ?', (artifact_id,),
            ).fetchone()
            return self._record_scope(row['session_key'], row['session_owner_json']) if row is not None else None

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
        output_session_key: str | None = None,
    ) -> dict | None:
        """Update an artifact. Creates version snapshot if content changes."""
        with self._guard(artifact_id, session_key=output_session_key, write=True) as row:
            if row is None:
                return None
            existing = self._row_to_dict(row)
            version_bump = False
            now = time.time()
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

            if output_session_key:
                self._insert_session_output(artifact_id, output_session_key)
            updated = self._conn.execute('SELECT * FROM artifacts WHERE id = ?', (artifact_id,)).fetchone()
            return self._row_to_dict(updated)

    def delete(self, artifact_id: str) -> bool:
        """Delete an artifact and all its versions."""
        with self._guard(artifact_id, write=True) as row:
            if row is None:
                return False
            cur = self._conn.execute(
                "DELETE FROM artifacts WHERE id = ?", (artifact_id,)
            )
            if cur.rowcount > 0:
                self._fts_delete(artifact_id)
                return True
        return False

    def _insert_session_output(self, artifact_id: str, session_key: str) -> bool:
        """Join the caller's transaction; never create a dangling association."""
        scope = self._new_scope(session_key)
        cursor = self._conn.execute(
            """INSERT OR IGNORE INTO artifact_session_outputs (artifact_id, session_key, session_owner_json)
               SELECT id, ?, ? FROM artifacts WHERE id = ?""", (session_key, json.dumps(scope.owner), artifact_id),
        )
        return cursor.rowcount > 0

    def attach_session_output(self, artifact_id: str, session_key: str) -> bool:
        """Record a successfully exported output without replacing its origin."""
        if not session_key:
            return False
        with self._guard(artifact_id, session_key=session_key, write=True) as row:
            if row is None:
                return False
            return self._insert_session_output(artifact_id, session_key)

    def has_session_output(self, artifact_id: str, session_key: str) -> bool:
        if not isinstance(session_key, str) or not session_key:
            return False
        with self._guard(artifact_id, session_key=session_key, membership=True, summary=True) as row:
            return row is not None

    def get_session_output(self, artifact_id: str, session_key: str) -> dict | None:
        if not isinstance(session_key, str) or not session_key:
            return None
        with self._guard(artifact_id, session_key=session_key, membership=True) as row:
            return self._row_to_dict(row) if row is not None else None

    def session_summaries(self, session_key: str, offset: int, limit: int, *, include_internal: bool = True) -> list[dict]:
        """A bounded work-output page that does not load artifact content."""
        if not isinstance(session_key, str) or not session_key:
            return []
        with self._lock:
            ids = [row[0] for row in self._conn.execute(
                """SELECT id FROM artifacts AS a
                   WHERE flowly_artifact_visible(a.session_key, a.session_owner_json) AND
                   (session_key = ? OR EXISTS
                    (SELECT 1 FROM artifact_session_outputs AS o
                     WHERE o.artifact_id = a.id AND o.session_key = ?))
                   ORDER BY created_at DESC, id""", (session_key, session_key),
            ).fetchall()]
        return self._page(ids, offset, limit, session_key=session_key,
                          include_internal=include_internal, summary=True)

    def _page(self, ids, offset, limit, *, session_key=None, include_internal=True, summary=False):
        from flowly.artifacts.context import is_internal_context_artifact

        offset, limit = max(0, int(offset)), max(0, int(limit))
        result, visible = [], 0
        if limit == 0:
            return result
        for artifact_id in ids:
            with self._guard(artifact_id, session_key=session_key,
                             membership=session_key is not None, summary=True) as row:
                if row is None:
                    continue
                item = self._row_to_dict(row)
                if not include_internal and is_internal_context_artifact(item):
                    continue
                if visible < offset:
                    visible += 1
                    continue
                if summary:
                    item = {key: item[key] for key in ('id', 'type', 'title', 'version', 'updated_at', 'tags', 'metadata')}
                else:
                    full = self._conn.execute('SELECT * FROM artifacts WHERE id = ?', (artifact_id,)).fetchone()
                    if full is None:
                        continue
                    item = self._row_to_dict(full)
                result.append(item)
                if len(result) == limit:
                    break
        return result

    def list(
        self,
        type: str | None = None,
        pinned: bool | None = None,
        search: str | None = None,
        tags: list[str] | None = None,
        session_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_internal: bool = True,
    ) -> list[dict]:
        """List artifacts with optional filters. FTS5 for search.

        ``tags`` filter: returns rows whose tag list contains EVERY tag
        listed (AND, not OR). Tags are stored as a JSON array string in
        the `tags` column; we use SQLite's LIKE with a defensive quoted
        pattern that matches `"tag"` substrings. Cheap and good enough
        because tag values are slug-like and the column is small.
        """
        if search:
            return self._list_fts(
                search, type, pinned, tags, session_key, limit, offset, include_internal
            )

        conditions: list[str] = ['flowly_artifact_visible(session_key, session_owner_json)']
        params: list[Any] = []

        if type is not None:
            conditions.append("type = ?")
            params.append(type)
        if pinned is not None:
            conditions.append("pinned = ?")
            params.append(1 if pinned else 0)
        if session_key is not None:
            conditions.append("session_key = ?")
            params.append(session_key)
        if tags:
            for tag in tags:
                # Match the JSON-encoded form: "tag" with surrounding quotes.
                # json.dumps gives us proper escaping for unusual chars.
                conditions.append("tags LIKE ?")
                params.append(f"%{json.dumps(tag)}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            ids = [row[0] for row in self._conn.execute(
                f"SELECT id FROM artifacts {where} ORDER BY updated_at DESC, id", params,
            ).fetchall()]
        # Canonical ownership and internal visibility precede pagination. Only
        # the selected page loads content, even if hidden rows fill the index.
        return self._page(ids, offset, limit, include_internal=include_internal)

    def _list_fts(
        self,
        search: str,
        type: str | None,
        pinned: bool | None,
        tags: list[str] | None,
        session_key: str | None,
        limit: int,
        offset: int,
        include_internal: bool = True,
    ) -> list[dict]:
        """Full-text search via FTS5, with optional tag intersection."""
        # Escape and quote terms for FTS5
        terms = search.strip().split()
        if not terms:
            return self.list(
                type=type,
                pinned=pinned,
                tags=tags,
                session_key=session_key,
                limit=limit,
                offset=offset,
                include_internal=include_internal,
            )
        fts_query = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms[:20])

        conditions = ["artifacts_fts MATCH ?", 'flowly_artifact_visible(a.session_key, a.session_owner_json)']
        params: list[Any] = [fts_query]

        if type is not None:
            conditions.append("a.type = ?")
            params.append(type)
        if pinned is not None:
            conditions.append("a.pinned = ?")
            params.append(1 if pinned else 0)
        if session_key is not None:
            conditions.append("a.session_key = ?")
            params.append(session_key)
        if tags:
            for tag in tags:
                conditions.append("a.tags LIKE ?")
                params.append(f"%{json.dumps(tag)}%")

        where = " AND ".join(conditions)
        with self._lock:
            ids = [row[0] for row in self._conn.execute(
                f"""SELECT a.id FROM artifacts_fts
                    JOIN artifacts a ON a.id = artifacts_fts.id
                    WHERE {where} ORDER BY rank, a.id""", params,
            ).fetchall()]
        return self._page(ids, offset, limit, include_internal=include_internal)

    def pin(self, artifact_id: str, pinned: bool = True) -> dict | None:
        """Pin or unpin an artifact."""
        return self.update(artifact_id, pinned=pinned)

    def get_versions(self, artifact_id: str) -> list[dict]:
        """Get version history for an artifact, newest first."""
        with self._guard(artifact_id, summary=True) as artifact:
            if artifact is None:
                return []
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
        d.pop('session_owner_json', None)
        d["metadata"] = _parse_json(d.get("metadata"), {})
        d["data_bindings"] = _parse_json(d.get("data_bindings"), [])
        d["tags"] = _parse_json(d.get("tags"), [])
        d["pinned"] = bool(d.get("pinned", 0))
        return d
