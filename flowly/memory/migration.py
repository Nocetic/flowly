"""One-time migration of legacy free-form MEMORY.md into governance items.

The legacy ``MEMORY.md`` is timestamped free-form markdown written by
``memory_append`` as ``<!-- YYYY-MM-DD HH:MM -->\\n<content>`` entries. This
module:

1. Backs up the original file (``MEMORY.md.bak-<runid>``).
2. Parses it into discrete entries.
3. Skips entries already represented as structured facts in the KG (so we don't
   re-import e.g. an email that already lives in the knowledge graph as a
   free-form preference).
4. Inserts the rest as ``candidate`` governance items (``ref_kind='memory_md'``).

It is idempotent: a ``memory_md_migrated`` flag in ``memory_meta`` short-circuits
repeat runs.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flowly.agent.memory import MemoryStore
from flowly.memory.governance import (
    ACTOR_MIGRATION,
    STATUS_CANDIDATE,
    GovernanceStore,
)

_MIGRATED_FLAG = "memory_md_migrated"
_TIMESTAMP_MARKER = re.compile(r"<!--\s*\d{4}-\d{2}-\d{2}[^>]*-->")
_MIN_TOKEN_LEN = 4


@dataclass
class MigrationResult:
    migrated: bool
    imported: int = 0
    kg_skipped: int = 0
    duplicates: int = 0
    backup_path: Optional[str] = None
    reason: str = ""


def parse_freeform_entries(md: str) -> list[str]:
    """Split legacy MEMORY.md into entries.

    Entries are delimited by ``<!-- timestamp -->`` markers. If no markers are
    present (hand-written file), split on blank-line paragraphs instead.
    """
    if not md.strip():
        return []
    if _TIMESTAMP_MARKER.search(md):
        # Drop the markers; the text after each marker (up to the next) is an entry.
        parts = _TIMESTAMP_MARKER.split(md)
        entries = [p.strip() for p in parts]
    else:
        entries = [p.strip() for p in re.split(r"\n\s*\n", md)]
    out: list[str] = []
    for e in entries:
        # Drop markdown headers like "# 2026-06-05" and empties.
        cleaned = e.strip()
        if not cleaned:
            continue
        if cleaned.startswith("#") and "\n" not in cleaned:
            continue
        out.append(cleaned)
    return out


def kg_value_tokens(kg) -> set[str]:
    """Read-only: collect current KG object/value display strings for dedup.

    Reads the KG sqlite directly (no mutation, no dependency on KG internals
    beyond its public ``db_path``). Returns lowercased tokens of length >= 4.
    """
    tokens: set[str] = set()
    try:
        conn = sqlite3.connect(str(kg.db_path), timeout=5)
        try:
            rows = conn.execute(
                """SELECT COALESCE(e.name, t.object) AS obj
                   FROM triples t
                   LEFT JOIN entities e ON t.object = e.id
                   WHERE t.valid_to IS NULL"""
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return tokens
    for (obj,) in rows:
        if not obj:
            continue
        norm = str(obj).replace("_", " ").strip().lower()
        if len(norm) >= _MIN_TOKEN_LEN:
            tokens.add(norm)
    return tokens


def _is_kg_covered(text: str, tokens: set[str]) -> bool:
    if not tokens:
        return False
    low = text.lower()
    return any(tok in low for tok in tokens)


def _slugify_key(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    return "memory_md:" + "-".join(words)


def migrate_memory_md(
    gov: GovernanceStore,
    memory_store: MemoryStore,
    *,
    kg_tokens: Optional[set[str]] = None,
    backup: bool = True,
) -> MigrationResult:
    """Import legacy MEMORY.md entries into governance as candidate items.

    ``kg_tokens`` are precomputed via :func:`kg_value_tokens` (passed explicitly so
    this is trivially testable without a live KG). Idempotent via a meta flag.
    """
    if gov.get_meta(_MIGRATED_FLAG) == "1":
        return MigrationResult(migrated=False, reason="already_migrated")

    raw = memory_store.read_long_term()
    if not raw.strip():
        gov.set_meta(_MIGRATED_FLAG, "1")
        return MigrationResult(migrated=False, reason="empty")

    backup_path: Optional[str] = None
    if backup:
        runid = uuid.uuid4().hex[:8]
        dest = Path(memory_store.memory_file).with_name(
            f"{memory_store.memory_file.name}.bak-{runid}"
        )
        dest.write_text(raw, encoding="utf-8")
        backup_path = str(dest)

    tokens = kg_tokens or set()
    entries = parse_freeform_entries(raw)

    imported = kg_skipped = duplicates = 0
    seen_text: set[str] = set()
    for entry in entries:
        norm = " ".join(entry.split()).lower()
        if norm in seen_text:
            duplicates += 1
            continue
        seen_text.add(norm)
        if _is_kg_covered(entry, tokens):
            kg_skipped += 1
            continue
        gov.add_item(
            kind="preference",
            text=entry,
            status=STATUS_CANDIDATE,
            ref_kind="memory_md",
            ref_id=None,
            normalized_key=_slugify_key(entry),
            confidence=0.0,
            actor=ACTOR_MIGRATION,
            reason="legacy_memory_md_import",
        )
        imported += 1

    gov.set_meta(_MIGRATED_FLAG, "1")
    return MigrationResult(
        migrated=True,
        imported=imported,
        kg_skipped=kg_skipped,
        duplicates=duplicates,
        backup_path=backup_path,
    )
