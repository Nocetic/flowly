"""P5/live: memory consolidation — parse, apply, run (LLM seam faked)."""

from __future__ import annotations

import pytest

from flowly.agent.memory import MemoryStore
from flowly.memory.consolidate import (
    ConsolidateOp,
    Consolidator,
    apply_operations,
    build_context,
    parse_operations,
)
from flowly.memory.governance import (
    GovernanceStore,
    STATUS_ACTIVE,
    STATUS_STALE,
    STATUS_SUPERSEDED,
)


@pytest.fixture
def gov(tmp_path):
    s = GovernanceStore(tmp_path / "gov.sqlite3")
    yield s
    s.close()


class FakeMirror:
    def __init__(self):
        self.superseded = []

    def supersede(self, tid, ended=None):
        self.superseded.append(tid)
        return 1


def _active(gov, **kw):
    it = gov.add_item(**kw)
    gov.transition(it.id, STATUS_ACTIVE)
    return gov.get_item(it.id)


# -- parse ------------------------------------------------------------------


def test_parse_plain_json():
    ops = parse_operations('{"operations":[{"op":"stale","item_id":"m_1","reason":"old"}]}')
    assert len(ops) == 1 and ops[0].op == "stale" and ops[0].item_id == "m_1"


def test_parse_fenced_json():
    raw = '```json\n{"operations":[{"op":"merge","item_id":"m_1","into_id":"m_2"}]}\n```'
    ops = parse_operations(raw)
    assert ops[0].op == "merge" and ops[0].into_id == "m_2"


def test_parse_garbage_returns_empty():
    assert parse_operations("not json at all") == []
    assert parse_operations("") == []


def test_parse_filters_invalid_ops():
    ops = parse_operations('{"operations":[{"op":"delete","item_id":"m_1"},{"op":"stale"}]}')
    assert ops == []  # unknown op + missing item_id both dropped


# -- build_context ----------------------------------------------------------


def test_build_context_only_active(gov):
    a = _active(gov, kind="fact", text="f1")
    gov.add_item(kind="preference", text="candidate")  # not active
    ctx = build_context(gov, kg_summary="KG")
    assert [i["id"] for i in ctx["items"]] == [a.id]
    assert ctx["kg_summary"] == "KG"


# -- apply ------------------------------------------------------------------


def test_apply_stale(gov):
    it = _active(gov, kind="preference", text="old email note")
    res = apply_operations(gov, [ConsolidateOp("stale", it.id, reason="outdated")])
    assert res.staled == 1
    assert gov.get_item(it.id).status == STATUS_STALE


def test_apply_supersede_redundant_freeform(gov):
    it = _active(gov, kind="preference", text="dup of KG")
    res = apply_operations(gov, [ConsolidateOp("supersede", it.id, reason="dup")])
    assert res.superseded == 1
    assert gov.get_item(it.id).status == STATUS_SUPERSEDED


def test_apply_merge_into_survivor_with_kg_mirror(gov):
    survivor = _active(gov, kind="fact", text="Hakan email a@b.com",
                       ref_kind="kg_triple", ref_id="t_keep")
    loser = _active(gov, kind="fact", text="a@b.com email a@b.com",
                    ref_kind="kg_triple", ref_id="t_drop")
    mirror = FakeMirror()
    res = apply_operations(
        gov, [ConsolidateOp("merge", loser.id, into_id=survivor.id, reason="same fact")],
        kg_mirror=mirror,
    )
    assert res.merged == 1
    assert gov.get_item(loser.id).status == STATUS_SUPERSEDED
    assert gov.get_item(loser.id).supersedes == survivor.id
    assert "t_drop" in mirror.superseded
    assert gov.get_item(survivor.id).status == STATUS_ACTIVE  # survivor untouched


def test_apply_skips_non_active_and_bad_merge(gov):
    cand = gov.add_item(kind="fact", text="not active")  # candidate
    surv = _active(gov, kind="fact", text="s")
    loser = _active(gov, kind="fact", text="l")
    res = apply_operations(gov, [
        ConsolidateOp("stale", cand.id),                    # skip: not active
        ConsolidateOp("merge", loser.id, into_id=loser.id), # skip: into_id == item_id
        ConsolidateOp("merge", surv.id, into_id="m_missing"),# skip: survivor missing
    ])
    assert res.applied() == 0
    assert res.skipped == 3
    assert len(res.errors) == 3


def test_apply_never_raises_on_bad_id(gov):
    res = apply_operations(gov, [ConsolidateOp("stale", "m_does_not_exist")])
    assert res.skipped == 1 and res.applied() == 0


# -- Consolidator.run -------------------------------------------------------


def test_run_dry_run_does_not_mutate(gov):
    it = _active(gov, kind="preference", text="x")
    c = Consolidator(gov, propose_fn=lambda ctx: [ConsolidateOp("stale", it.id)])
    ops, res = c.run(dry_run=True)
    assert len(ops) == 1
    assert res.applied() == 0
    assert gov.get_item(it.id).status == STATUS_ACTIVE  # untouched


def test_run_applies_and_refreshes_memory_md(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    keep = _active(gov, kind="preference", text="keep me")
    drop = _active(gov, kind="preference", text="drop me")
    c = Consolidator(
        gov,
        propose_fn=lambda ctx: [ConsolidateOp("supersede", drop.id, reason="dup")],
        memory_store=ms,
    )
    ops, res = c.run()
    assert res.superseded == 1
    assert gov.get_item(drop.id).status == STATUS_SUPERSEDED
    md = ms.read_long_term()
    assert "keep me" in md and "drop me" not in md  # MEMORY.md reflects survivors
