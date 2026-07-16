"""Small shared primitives for composer-mounted picker panels."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")

_SEP_RE = re.compile(r"[\s/_:\-.]+")
MIN_PICKER_WIDTH = 40
MAX_PICKER_WIDTH = 90
PICKER_EDGE_MARGIN = 6


def picker_width_for_columns(columns: int) -> int:
    """Clamp floating picker width, without overflowing tiny panes."""
    available = max(1, int(columns) - PICKER_EDGE_MARGIN)
    if available < MIN_PICKER_WIDTH:
        return available
    return min(MAX_PICKER_WIDTH, available)


def normalize_query_text(value: object) -> str:
    """Lowercase, accent-fold, and normalize common model id separators."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _SEP_RE.sub(" ", text.lower()).strip()


def fuzzy_filter(items: Sequence[T], query: str, text_for: Callable[[T], str]) -> list[T]:
    """Return items ranked by a small deterministic fuzzy score.

    Empty query preserves source order. Non-empty query requires every query
    token to match either by substring or ordered-subsequence, then sorts by
    aggregate score while preserving source order for ties.
    """
    query_text = normalize_query_text(query)
    if not query_text:
        return list(items)

    tokens = query_text.split()
    ranked: list[tuple[int, int, T]] = []
    for index, item in enumerate(items):
        haystack = normalize_query_text(text_for(item))
        score = _score_tokens(haystack, tokens)
        if score is not None:
            ranked.append((score, index, item))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [item for _score, _index, item in ranked]


def visible_window(selected_idx: int, count: int, visible_rows: int) -> tuple[int, int]:
    if count <= 0:
        return 0, 0
    if count <= visible_rows:
        return 0, count
    start = max(0, min(selected_idx - visible_rows // 2, count - visible_rows))
    return start, min(count, start + visible_rows)


def clamp_index(index: int, count: int) -> int:
    if count <= 0:
        return 0
    return max(0, min(index, count - 1))


def is_plain_character(event: object, char: str) -> bool:
    return (
        bool(char)
        and len(char) == 1
        and char >= " "
        and not bool(getattr(event, "ctrl", False))
        and not bool(getattr(event, "meta", False))
    )


def _score_tokens(haystack: str, tokens: list[str]) -> int | None:
    total = 0
    for token in tokens:
        score = _score_token(haystack, token)
        if score is None:
            return None
        total += score
    return total


def _score_token(haystack: str, token: str) -> int | None:
    pos = haystack.find(token)
    if pos >= 0:
        boundary_bonus = 20 if _is_word_boundary(haystack, pos) else 0
        early_bonus = max(0, 40 - pos)
        return 100 + boundary_bonus + early_bonus + len(token)
    subseq_score = _subsequence_score(haystack, token)
    if subseq_score is None:
        return None
    return subseq_score


def _is_word_boundary(text: str, index: int) -> bool:
    return index == 0 or text[index - 1] == " "


def _subsequence_score(haystack: str, token: str) -> int | None:
    cursor = 0
    span_start = -1
    span_end = -1
    for ch in token:
        found = haystack.find(ch, cursor)
        if found < 0:
            return None
        if span_start < 0:
            span_start = found
        span_end = found
        cursor = found + 1
    span = max(1, span_end - span_start + 1)
    compactness = max(0, 30 - span)
    early_bonus = max(0, 20 - span_start)
    return 40 + compactness + early_bonus + len(token)
