"""SQLite indexer — stores chunks with FTS5 and optional vector embeddings."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.memory.chunker import chunk_text, Chunk


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    path       TEXT PRIMARY KEY,
    hash       TEXT NOT NULL,
    mtime      REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id         TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    hash       TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL,
    embedding  TEXT,          -- JSON array of floats, NULL if no embeddings
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    id         UNINDEXED,
    path       UNINDEXED,
    start_line UNINDEXED,
    end_line   UNINDEXED,
    tokenize = 'unicode61'
);
"""

_SCHEMA_VERSION = "1"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _chunk_id(path: str, start: int, end: int) -> str:
    return f"{path}:L{start}-{end}"


# ── Public API ────────────────────────────────────────────────────────────────

class MemoryIndexer:
    """
    Manages the SQLite index for memory files.

    Stores chunks as FTS5 rows (always) and optionally stores embedding
    vectors as JSON in the `embedding` column for vector search.
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
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

    # ── Sync ──────────────────────────────────────────────────────────────────

    def needs_reindex(self, path: Path) -> bool:
        """Return True if the file has changed since last index."""
        rel = str(path)
        cur = self._conn.execute("SELECT hash FROM files WHERE path = ?", (rel,))
        row = cur.fetchone()
        if row is None:
            return True
        try:
            return row["hash"] != _file_hash(path)
        except OSError:
            return True

    def index_file(
        self,
        path: Path,
        workspace: Path,
        embeddings: list[list[float]] | None = None,
        model: str = "",
        chunk_tokens: int = 400,
        overlap_tokens: int = 80,
    ) -> int:
        """
        Index a single file.

        Args:
            path: Absolute path to the .md file.
            workspace: Workspace root (used to compute relative path).
            embeddings: Pre-computed embeddings for each chunk (same order as chunks).
            model: Embedding model name used (stored for cache invalidation).
            chunk_tokens: Target tokens per chunk.
            overlap_tokens: Overlap tokens between chunks.

        Returns:
            Number of chunks indexed.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"[Memory] Cannot read {path}: {e}")
            return 0

        rel = str(path.relative_to(workspace)) if path.is_relative_to(workspace) else str(path)
        file_hash = _file_hash(path)
        now = time.time()

        chunks = chunk_text(text, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)

        with self._conn:
            # Remove old chunks for this file
            self._remove_file_chunks(rel)

            for idx, chunk in enumerate(chunks):
                cid = _chunk_id(rel, chunk.start_line, chunk.end_line)
                emb_json = None
                if embeddings and idx < len(embeddings):
                    emb_json = json.dumps(embeddings[idx])

                chunk_hash = hashlib.sha256(chunk.text.encode()).hexdigest()

                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO chunks
                        (id, path, start_line, end_line, hash, model, text, embedding, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cid, rel, chunk.start_line, chunk.end_line,
                     chunk_hash, model, chunk.text, emb_json, now),
                )
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO chunks_fts
                        (id, path, start_line, end_line, text)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (cid, rel, chunk.start_line, chunk.end_line, chunk.text),
                )

            self._conn.execute(
                """
                INSERT OR REPLACE INTO files (path, hash, mtime, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (rel, file_hash, path.stat().st_mtime, now),
            )

        return len(chunks)

    def remove_file(self, rel_path: str) -> None:
        """Remove all chunks for a file that no longer exists."""
        with self._conn:
            self._remove_file_chunks(rel_path)
            self._conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))

    def _remove_file_chunks(self, rel_path: str) -> None:
        # Get chunk IDs first for FTS delete
        rows = self._conn.execute(
            "SELECT id FROM chunks WHERE path = ?", (rel_path,)
        ).fetchall()
        for row in rows:
            self._conn.execute(
                "DELETE FROM chunks_fts WHERE id = ?", (row["id"],)
            )
        self._conn.execute("DELETE FROM chunks WHERE path = ?", (rel_path,))

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_all_chunks(self) -> list[dict[str, Any]]:
        """Return all chunks with their embeddings (for vector search)."""
        rows = self._conn.execute(
            "SELECT id, path, start_line, end_line, text, embedding FROM chunks"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_chunks_fts(
        self,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """BM25 keyword search via FTS5."""
        # Sanitize query for FTS5: escape special chars
        safe_query = _fts5_escape(query)
        if not safe_query:
            return []
        try:
            rows = self._conn.execute(
                """
                SELECT id, path, start_line, end_line, text,
                       bm25(chunks_fts) AS rank
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            logger.debug(f"[Memory] FTS query error: {e}")
            return []

    def get_snippet(self, rel_path: str, from_line: int, lines: int) -> str | None:
        """Get a text snippet from a file by line range."""
        row = self._conn.execute(
            """
            SELECT text FROM chunks
            WHERE path = ? AND start_line <= ? AND end_line >= ?
            LIMIT 1
            """,
            (rel_path, from_line, from_line),
        ).fetchone()
        if row:
            text_lines = row["text"].splitlines()
            offset = max(0, from_line - 1)
            return "\n".join(text_lines[offset: offset + lines])
        return None

    def indexed_paths(self) -> set[str]:
        """Return set of all indexed relative paths."""
        rows = self._conn.execute("SELECT path FROM files").fetchall()
        return {r["path"] for r in rows}


def _fts5_escape(query: str) -> str:
    """
    Convert a free-text query to a safe FTS5 MATCH expression.

    Wraps each word in double-quotes and joins with OR so that
    documents matching ANY query term are returned (ranked by BM25).
    """
    words = [w.strip() for w in query.split() if w.strip()]
    if not words:
        return ""
    # Use OR so partial matches are found; BM25 ranking handles relevance
    return " OR ".join(f'"{w}"' for w in words[:20])  # cap at 20 terms
