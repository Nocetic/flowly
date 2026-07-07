"""Incremental session indexing must preserve message ids.

Regression for the memory-dreamer "proposes memories with no conversation" bug:
delete-all + reinsert on every save/rebuild reassigned every message a new
autoincrement id, so the dreamer's id-based watermark saw old messages as new
and reprocessed history forever. index_session is now append-only for the
common case and only re-ids a session on genuine divergence.
"""

from __future__ import annotations

from flowly.session.indexer import SessionIndexer


def _msg(role: str, content: str, ts: str = "2026-07-01T10:00:00") -> dict:
    return {"role": role, "content": content, "timestamp": ts}


def _ids(idx: SessionIndexer, key: str) -> list[int]:
    rows = idx._conn.execute(
        "SELECT id FROM messages WHERE session_key = ? ORDER BY id", (key,)
    ).fetchall()
    return [r[0] for r in rows]


def test_reindexing_identical_session_preserves_ids(tmp_path):
    idx = SessionIndexer(db_path=tmp_path / "idx.sqlite")
    msgs = [_msg("user", "hello"), _msg("assistant", "hi there")]

    idx.index_session("cli:a", msgs)
    first = _ids(idx, "cli:a")

    # Re-index the SAME messages (what every startup rebuild + no-op save does).
    idx.index_session("cli:a", msgs)
    assert _ids(idx, "cli:a") == first, "identical reindex must not churn ids"


def test_appending_preserves_existing_ids_and_only_adds_tail(tmp_path):
    idx = SessionIndexer(db_path=tmp_path / "idx.sqlite")
    idx.index_session("cli:a", [_msg("user", "q1"), _msg("assistant", "a1")])
    before = _ids(idx, "cli:a")

    idx.index_session(
        "cli:a",
        [_msg("user", "q1"), _msg("assistant", "a1"), _msg("user", "q2"), _msg("assistant", "a2")],
    )
    after = _ids(idx, "cli:a")

    assert after[: len(before)] == before, "existing ids must be untouched on append"
    assert len(after) == len(before) + 2
    assert after[-1] > before[-1], "new tail gets fresh higher ids (watermark sees them)"


def test_divergence_reindexes_only_that_session(tmp_path):
    idx = SessionIndexer(db_path=tmp_path / "idx.sqlite")
    idx.index_session("cli:a", [_msg("user", "q1"), _msg("assistant", "a1")])
    idx.index_session("cli:b", [_msg("user", "other")])
    b_ids = _ids(idx, "cli:b")

    # Compaction: head replaced by a summary → prefix diverges.
    idx.index_session(
        "cli:a",
        [_msg("assistant", "[summary of earlier turns]"), _msg("user", "q2")],
    )
    a_after = _ids(idx, "cli:a")

    assert len(a_after) == 2
    assert min(a_after) > max(b_ids), "diverged session re-ids above the high-water"
    assert _ids(idx, "cli:b") == b_ids, "an unrelated session is never touched"


def test_search_still_finds_appended_message(tmp_path):
    idx = SessionIndexer(db_path=tmp_path / "idx.sqlite")
    idx.index_session("cli:a", [_msg("user", "first message")])
    idx.index_session("cli:a", [_msg("user", "first message"), _msg("assistant", "zebra reply")])

    hits = idx.search("zebra", limit=5)
    assert any("zebra" in h.get("snippet", "") for h in hits), "FTS stays in sync on append"


def test_dreamer_watermark_only_advances_on_real_new_messages(tmp_path):
    """End-to-end of the fix: with a stable watermark, a rebuild-then-reindex of
    an unchanged session yields no new rows past the last-seen id."""
    idx = SessionIndexer(db_path=tmp_path / "idx.sqlite")
    idx.index_session("cli:a", [_msg("user", "q1"), _msg("assistant", "a1")])
    watermark = max(_ids(idx, "cli:a"))

    # Simulate a bunch of startup rebuilds (no content change).
    for _ in range(5):
        idx.index_session("cli:a", [_msg("user", "q1"), _msg("assistant", "a1")])

    new_rows = idx._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE id > ?", (watermark,)
    ).fetchone()[0]
    assert new_rows == 0, "reindexing unchanged history must produce no 'new' rows"
