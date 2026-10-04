"""MemoryIndexManager — singleton per workspace, lazy init, sync + optional file watch."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.memory.embeddings import (
    embed_single,
    embed_texts,
    resolve_embedding_settings,
)
from flowly.memory.indexer import MemoryIndexer
from flowly.memory.search import SearchResult, hybrid_search, vector_search

# ── Singleton cache ────────────────────────────────────────────────────────────
_CACHE: dict[str, "MemoryIndexManager"] = {}


def get_manager(
    workspace: Path,
    state_dir: Path,
    config: Any = None,
    provider: str = "auto",
    model: str = "",
    api_key: str = "",
    api_base: str = "",
    chunk_tokens: int = 400,
    overlap_tokens: int = 80,
    max_results: int = 6,
    min_score: float = 0.35,
    vector_weight: float = 0.7,
    text_weight: float = 0.3,
) -> "MemoryIndexManager":
    """Get or create a MemoryIndexManager for the given workspace."""
    key = str(workspace)
    if key not in _CACHE:
        _CACHE[key] = MemoryIndexManager(
            workspace=workspace,
            state_dir=state_dir,
            config=config,
            provider=provider,
            model=model,
            api_key=api_key,
            api_base=api_base,
            chunk_tokens=chunk_tokens,
            overlap_tokens=overlap_tokens,
            max_results=max_results,
            min_score=min_score,
            vector_weight=vector_weight,
            text_weight=text_weight,
        )
    return _CACHE[key]


class MemoryIndexManager:
    """
    Manages memory file indexing and search for one workspace.

    - Indexes all .md files under workspace/memory/
    - Lazily syncs on search if files have changed
    - Optionally generates embeddings for vector search
    - Falls back to FTS5-only keyword search if no embedding provider
    """

    def __init__(
        self,
        workspace: Path,
        state_dir: Path,
        config: Any = None,
        provider: str = "auto",
        model: str = "",
        api_key: str = "",
        api_base: str = "",
        chunk_tokens: int = 400,
        overlap_tokens: int = 80,
        max_results: int = 6,
        min_score: float = 0.35,
        vector_weight: float = 0.7,
        text_weight: float = 0.3,
    ):
        self._workspace = workspace
        self._memory_dir = workspace / "memory"
        self._chunk_tokens = chunk_tokens
        self._overlap_tokens = overlap_tokens
        self._max_results = max_results
        self._min_score = min_score
        self._vector_weight = vector_weight
        self._text_weight = text_weight

        # Resolve embedding provider
        resolved_provider, resolved_model, key, base = resolve_embedding_settings(
            provider, model, api_key, api_base, config
        )
        self._emb_provider = resolved_provider   # None = FTS5 only
        self._emb_model = resolved_model or ""
        self._api_key = key
        self._api_base = base

        if resolved_provider:
            logger.info(
                f"[Memory] Embedding provider: {resolved_provider}/{resolved_model}"
            )
        else:
            logger.info("[Memory] No embedding provider — using FTS5 keyword search only")

        # SQLite index
        db_path = state_dir / "memory_index.sqlite"
        self._indexer = MemoryIndexer(db_path)

        self._last_sync: float = 0.0
        self._sync_lock = asyncio.Lock()
        self._embedding_lock = asyncio.Lock()
        self._embedding_retry_at = 0.0

    # ── Public API ─────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        max_results: int | None = None,
        min_score: float | None = None,
        *,
        embedding_timeout: float = 5.0,
    ) -> list[SearchResult]:
        """
        Search memory files for relevant chunks.

        Syncs index if files have changed, then runs hybrid search.
        """
        await self._sync_if_needed()

        max_r = max_results if max_results is not None else self._max_results
        min_s = min_score if min_score is not None else self._min_score

        # Keyword search (always)
        candidate_limit = max_r * 4
        try:
            keyword_results = self._indexer.get_chunks_fts(query, limit=candidate_limit)
        except Exception as e:
            logger.warning(f"[Memory] FTS5 search failed (database locked?): {e}")
            keyword_results = []

        # Local indexing and FTS complete before optional network work. A slow
        # provider must never take the already-available keyword hits with it.
        vector_results: list[dict] = []
        if self._emb_provider and embedding_timeout > 0 and time.monotonic() >= self._embedding_retry_at:
            try:
                vector_results = await asyncio.wait_for(
                    self._search_vectors(query, candidate_limit), timeout=embedding_timeout,
                )
            except Exception as e:
                self._embedding_retry_at = time.monotonic() + 30
                logger.warning("[Memory] Vector search unavailable: {}; keeping keyword results", type(e).__name__)

        return hybrid_search(
            keyword_results=keyword_results,
            vector_results=vector_results,
            vector_weight=self._vector_weight,
            text_weight=self._text_weight,
            max_results=max_r,
            min_score=min_s,
        )

    async def _search_vectors(self, query: str, limit: int) -> list[dict]:
        async with self._embedding_lock:
            if time.monotonic() < self._embedding_retry_at:
                return []
            missing = [c for c in self._indexer.get_all_chunks()
                       if not c["embedding"] or c["model"] != self._emb_model]
            # Bounded batches preserve completed enrichment across searches.
            # No detached task outlives a caller's timeout/cancellation.
            for start in range(0, len(missing), 32):
                batch = missing[start:start + 32]
                embeddings = await embed_texts(
                    [c["text"] for c in batch], provider=self._emb_provider,
                    model=self._emb_model, api_key=self._api_key, api_base=self._api_base,
                )
                if not embeddings or len(embeddings) != len(batch):
                    self._embedding_retry_at = time.monotonic() + 30
                    return []
                self._indexer.store_embeddings(batch, embeddings, self._emb_model)
            query_emb = await embed_single(
                query, provider=self._emb_provider, model=self._emb_model,
                api_key=self._api_key, api_base=self._api_base,
            )
            if not query_emb:
                self._embedding_retry_at = time.monotonic() + 30
                return []
            chunks = [c for c in self._indexer.get_all_chunks() if c["model"] == self._emb_model]
            return vector_search(query_emb, chunks, limit=limit, min_score=0.0)

    def get_snippet(self, rel_path: str, from_line: int, lines: int = 20) -> str | None:
        """Read a snippet from an indexed file by line range."""
        # Try reading directly from disk first (most accurate)
        abs_path = self._workspace / rel_path
        if abs_path.exists():
            try:
                file_lines = abs_path.read_text(encoding="utf-8").splitlines()
                start = max(0, from_line - 1)
                end = start + lines
                return "\n".join(file_lines[start:end])
            except OSError:
                pass
        return self._indexer.get_snippet(rel_path, from_line, lines)

    def status(self) -> dict[str, Any]:
        return {
            "provider": self._emb_provider or "none",
            "model": self._emb_model,
            "vector_enabled": self._emb_provider is not None,
            "last_sync": self._last_sync,
        }

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def _sync_if_needed(self) -> None:
        """Re-index any changed memory files (debounced to 1s)."""
        if not self._memory_dir.exists():
            return

        now = time.monotonic()
        if now - self._last_sync < 1.0:
            return

        async with self._sync_lock:
            # Double-check after acquiring lock
            if time.monotonic() - self._last_sync < 1.0:
                return
            await self._sync()
            self._last_sync = time.monotonic()

    async def _sync(self) -> None:
        """Sync all memory .md files to the index."""
        if not self._memory_dir.exists():
            return

        disk_files = set(self._memory_dir.rglob("*.md"))
        rel_disk = {
            str(p.relative_to(self._workspace)): p for p in disk_files
        }
        indexed = self._indexer.indexed_paths()

        # Remove deleted files
        for old_rel in indexed - set(rel_disk.keys()):
            self._indexer.remove_file(old_rel)
            logger.debug(f"[Memory] Removed index for {old_rel}")

        # Index new/changed files
        changed = [
            (rel, path) for rel, path in rel_disk.items()
            if self._indexer.needs_reindex(path, self._workspace)
        ]

        if not changed:
            return

        logger.info(f"[Memory] Syncing {len(changed)} changed file(s)")

        for rel, path in changed:
            await self._index_file(path)

    async def _index_file(self, path: Path) -> None:
        """Make current text searchable locally before attempting embeddings."""
        count = self._indexer.index_file(
            path=path,
            workspace=self._workspace,
            chunk_tokens=self._chunk_tokens,
            overlap_tokens=self._overlap_tokens,
        )
        logger.debug(f"[Memory] Indexed {path.name}: {count} chunks")
