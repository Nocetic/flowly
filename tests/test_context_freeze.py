"""F3b: frozen-snapshot of the injected memory block (flag-gated, default OFF)."""

from __future__ import annotations

import pytest

from flowly.agent.context import ContextBuilder
from flowly.config.schema import MemoryDreamingConfig


def _ctx(tmp_path, memory_text):
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "memory" / "MEMORY.md").write_text(memory_text, encoding="utf-8")
    return ContextBuilder(ws), ws


def test_config_flag_default_off():
    assert MemoryDreamingConfig().freeze_injected_memory is False


def test_freeze_off_reads_fresh_each_time(tmp_path):
    ctx, ws = _ctx(tmp_path, "first")
    b1 = ctx._memory_block_for("s1", memory_search_enabled=True)
    assert "first" in b1
    # change the file → fresh read reflects it immediately (current behavior)
    (ws / "memory" / "MEMORY.md").write_text("second", encoding="utf-8")
    b2 = ctx._memory_block_for("s1", memory_search_enabled=True)
    assert "second" in b2


def test_freeze_on_caches_per_session(tmp_path):
    ctx, ws = _ctx(tmp_path, "first")
    ctx.set_freeze_injected_memory(True)
    b1 = ctx._memory_block_for("s1", memory_search_enabled=True)
    assert "first" in b1
    # change the file → frozen block does NOT change until invalidated
    (ws / "memory" / "MEMORY.md").write_text("second", encoding="utf-8")
    b2 = ctx._memory_block_for("s1", memory_search_enabled=True)
    assert b2 == b1 and "second" not in b2
    # a different session computes fresh
    b_other = ctx._memory_block_for("s2", memory_search_enabled=True)
    assert "second" in b_other


def test_invalidate_refreshes(tmp_path):
    ctx, ws = _ctx(tmp_path, "first")
    ctx.set_freeze_injected_memory(True)
    ctx._memory_block_for("s1", memory_search_enabled=True)
    (ws / "memory" / "MEMORY.md").write_text("second", encoding="utf-8")
    ctx.invalidate_memory_snapshot("s1")
    b = ctx._memory_block_for("s1", memory_search_enabled=True)
    assert "second" in b


def test_disabling_clears_snapshot(tmp_path):
    ctx, ws = _ctx(tmp_path, "first")
    ctx.set_freeze_injected_memory(True)
    ctx._memory_block_for("s1", memory_search_enabled=True)
    ctx.set_freeze_injected_memory(False)
    (ws / "memory" / "MEMORY.md").write_text("second", encoding="utf-8")
    assert "second" in ctx._memory_block_for("s1", memory_search_enabled=True)


def test_no_session_key_never_freezes(tmp_path):
    ctx, ws = _ctx(tmp_path, "first")
    ctx.set_freeze_injected_memory(True)
    ctx._memory_block_for(None, memory_search_enabled=True)
    (ws / "memory" / "MEMORY.md").write_text("second", encoding="utf-8")
    # session_key None → always fresh (we never freeze a key we can't evict)
    assert "second" in ctx._memory_block_for(None, memory_search_enabled=True)


def test_snapshot_cap_evicts_oldest(tmp_path):
    ctx, ws = _ctx(tmp_path, "x")
    ctx.set_freeze_injected_memory(True)
    ctx._SESSION_MEMORY_CAP = 3
    for i in range(5):
        ctx._memory_block_for(f"s{i}", memory_search_enabled=True)
    assert len(ctx._session_memory_snapshot) <= 3
