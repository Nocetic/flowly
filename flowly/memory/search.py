"""Hybrid search — combines BM25 keyword results with cosine vector similarity."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class SearchResult:
    path: str
    start_line: int
    end_line: int
    snippet: str
    score: float          # 0.0 – 1.0 combined score
    vector_score: float   # cosine similarity (0 if no vector)
    text_score: float     # BM25 normalized (0 if no keyword match)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _normalize_bm25(rank: float) -> float:
    """
    Convert FTS5 bm25() rank (negative, lower = better) to 0-1 score.

    Uses a sigmoid-like mapping: score = 1 / (1 + exp(rank * 0.5))
    Since rank is negative, higher score for more negative rank.
    """
    # rank is negative from SQLite bm25(); clamp to reasonable range
    clamped = max(-20.0, min(0.0, rank))
    return 1.0 / (1.0 + math.exp(clamped * 0.5))


def vector_search(
    query_embedding: list[float],
    chunks: list[dict[str, Any]],
    limit: int,
    min_score: float,
) -> list[dict[str, Any]]:
    """
    Brute-force cosine similarity over all indexed chunks.

    Returns list of dicts with keys: id, path, start_line, end_line, text, vector_score.
    """
    import json as _json

    scored = []
    for chunk in chunks:
        emb_json = chunk.get("embedding")
        if not emb_json:
            continue
        try:
            emb = _json.loads(emb_json)
        except Exception:
            continue
        score = _cosine(query_embedding, emb)
        if score >= min_score:
            scored.append({**chunk, "vector_score": score})

    scored.sort(key=lambda x: x["vector_score"], reverse=True)
    return scored[:limit]


def hybrid_search(
    *,
    keyword_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    vector_weight: float = 0.7,
    text_weight: float = 0.3,
    max_results: int = 6,
    min_score: float = 0.35,
) -> list[SearchResult]:
    """
    Merge keyword (BM25) and vector results into a ranked list.

    Each result gets: final_score = vector_weight * vector_score + text_weight * text_score
    If only one source available, uses its score directly.
    """
    merged: dict[str, dict[str, Any]] = {}

    # Add vector results
    for r in vector_results:
        cid = r["id"]
        merged[cid] = {
            "path": r["path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "text": r["text"],
            "vector_score": r.get("vector_score", 0.0),
            "text_score": 0.0,
        }

    # Add / update keyword results
    for r in keyword_results:
        cid = r["id"]
        bm25_rank = r.get("rank", 0.0)
        text_score = _normalize_bm25(bm25_rank)
        if cid in merged:
            merged[cid]["text_score"] = text_score
        else:
            merged[cid] = {
                "path": r["path"],
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "text": r["text"],
                "vector_score": 0.0,
                "text_score": text_score,
            }

    # Compute final scores
    results: list[SearchResult] = []
    for cid, data in merged.items():
        vs = data["vector_score"]
        ts = data["text_score"]

        if vs > 0 and ts > 0:
            final = vector_weight * vs + text_weight * ts
        elif vs > 0:
            final = vs
        else:
            final = ts

        if final < min_score:
            continue

        # Truncate snippet to ~700 chars
        snippet = data["text"]
        if len(snippet) > 700:
            snippet = snippet[:697] + "..."

        results.append(SearchResult(
            path=data["path"],
            start_line=data["start_line"],
            end_line=data["end_line"],
            snippet=snippet,
            score=round(final, 4),
            vector_score=round(vs, 4),
            text_score=round(ts, 4),
        ))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:max_results]
