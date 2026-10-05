"""What of the agent's memory may leave the agent for a voice call.

One read of the governance index, applied identically by recall
(``voice.context``) and the call's memory snapshot (``voice.memory.snapshot``),
so the two can never disagree about what is private. A memory item reaches a
call only while it is ``active``, of ``normal`` privacy and inside its
validity window. Every other item is *withheld*: its text must not appear in
anything sent (a manual copy in a file does not bypass it), and a knowledge
graph triple it governs is excluded.

A governance index that cannot be read is not the same as one with nothing
private in it: the view is then unavailable and callers export no memory at
all (fail closed).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MAX_SOURCE_BYTES = 128_000
MAX_GOVERNED = 10_000


def revision_of(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def read_db(path: Path, sql: str, values: tuple = ()) -> list[dict]:
    connection = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True, timeout=0.25)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, values)]
    finally:
        connection.close()


@dataclass
class GovernedMemory:
    """The governance index as a call may see it."""
    status: str                                   # 'ok' | 'empty' | 'unavailable'
    allowed: list[dict] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    excluded_triples: set[str] = field(default_factory=set)
    revision: str = ''

    @property
    def available(self) -> bool:
        return self.status != 'unavailable'

    def withholds(self, text: str) -> bool:
        """True when ``text`` carries a withheld item's words."""
        folded = text.casefold()
        return any(item in folded for item in self.protected)

    @classmethod
    def load(cls, state_db: Callable[[str], Path], now: str | None = None) -> 'GovernedMemory':
        now = now or datetime.now(timezone.utc).isoformat()
        path = state_db('memory_governance.sqlite3')
        try:
            rows = read_db(path, 'SELECT * FROM memory_items LIMIT ?', (MAX_GOVERNED + 1,)) if path.exists() else []
            if len(rows) > MAX_GOVERNED:
                raise ValueError('Governance projection limit')
            view = cls(status='ok' if rows else 'empty', revision=revision_of(rows))
            for item in rows:
                allowed = (item['status'] == 'active' and item['privacy_level'] == 'normal'
                           and (not item['valid_to'] or item['valid_to'] > now)
                           and (not item['valid_from'] or item['valid_from'] <= now))
                if allowed:
                    view.allowed.append(item)
                    continue
                if item['text'].strip():
                    view.protected.append(item['text'].casefold().strip())
                if item['ref_kind'] == 'kg_triple' and item['ref_id']:
                    view.excluded_triples.add(str(item['ref_id']))
            return view
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            return cls(status='unavailable')


def read_memory_file(workspace: Path, relative: str) -> tuple[str, str, str] | None:
    """A workspace memory file's text, revision and mtime; None when absent.

    Only files inside the workspace, up to ``MAX_SOURCE_BYTES``; a larger file
    raises ``ValueError`` (the source is reported unavailable, never cut
    silently).
    """
    path = (workspace / relative).resolve()
    if not path.is_relative_to(workspace) or not path.is_file():
        return None
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('Memory source limit')
    with path.open('rb') as handle:
        raw = handle.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('Memory source limit')
    updated_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return raw.decode('utf-8'), revision_of(raw.hex()), updated_at
