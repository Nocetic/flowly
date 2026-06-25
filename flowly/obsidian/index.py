"""FTS index over an Obsidian vault.

Thin wrapper over :class:`flowly.memory.indexer.MemoryIndexer`, pointed at a
*separate* SQLite database (``obsidian_index.sqlite``) with the vault as its
workspace root. Keyword-only (BM25) — embeddings are intentionally left out of
v1; a missing embedding provider must never break search.

Indexing is incremental: ``MemoryIndexer.needs_reindex`` skips unchanged files
by hash/mtime. To keep per-search cost bounded on large vaults, a full re-walk
runs at most once per ``_SYNC_INTERVAL`` seconds.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from flowly.memory.indexer import MemoryIndexer
from flowly.obsidian.vault import iter_notes

logger = logging.getLogger(__name__)

_SYNC_INTERVAL = 60.0  # seconds between automatic re-walks


class ObsidianIndex:
    def __init__(
        self,
        db_path: Path,
        vault_root: Path,
        *,
        include_globs: list[str] | None = None,
        exclude_globs: list[str] | None = None,
        max_note_bytes: int = 1_000_000,
    ) -> None:
        self._vault_root = vault_root.resolve()
        self._include = include_globs or ["**/*.md"]
        self._exclude = exclude_globs or []
        self._max_bytes = max_note_bytes
        self._indexer = MemoryIndexer(db_path)
        self._last_sync = 0.0

    def close(self) -> None:
        self._indexer.close()

    def sync(self, *, force: bool = False) -> dict[str, int]:
        """Bring the index in line with the vault. Returns simple stats."""
        if not force and (time.monotonic() - self._last_sync) < _SYNC_INTERVAL:
            return {"indexed": 0, "removed": 0, "skipped": -1}

        seen: set[str] = set()
        indexed = 0
        for note in iter_notes(
            self._vault_root,
            include_globs=self._include,
            exclude_globs=self._exclude,
            max_note_bytes=self._max_bytes,
        ):
            seen.add(note.rel_path)
            try:
                if self._indexer.needs_reindex(note.abs_path):
                    self._indexer.index_file(note.abs_path, self._vault_root)
                    indexed += 1
            except Exception as exc:  # noqa: BLE001 — one bad note shouldn't abort sync
                logger.debug("[obsidian] index failed for %s: %s", note.rel_path, exc)

        removed = 0
        for stale in self._indexer.indexed_paths() - seen:
            self._indexer.remove_file(stale)
            removed += 1

        self._last_sync = time.monotonic()
        return {"indexed": indexed, "removed": removed, "skipped": 0}

    def search(self, query: str, *, max_results: int = 6) -> list[dict[str, Any]]:
        """Keyword search. Returns ranked chunks with citable line ranges.

        Never raises — a sync or FTS failure degrades to an empty result set.
        """
        try:
            self.sync()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[obsidian] sync during search failed: %s", exc)
        try:
            rows = self._indexer.get_chunks_fts(query, limit=max_results)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[obsidian] fts search failed: %s", exc)
            return []
        results: list[dict[str, Any]] = []
        for r in rows:
            text = (r.get("text") or "").strip()
            results.append(
                {
                    "source": "obsidian",
                    "path": r["path"],
                    "lines": f"L{r['start_line']}-L{r['end_line']}",
                    "start_line": r["start_line"],
                    "end_line": r["end_line"],
                    "score": round(float(r.get("rank", 0.0)), 4),
                    "snippet": text,
                }
            )
        return results
