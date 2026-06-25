"""P4 tests: MemoryGovernance facade — user actions, recall, undo, stats."""

from __future__ import annotations

import pytest

from flowly.agent.memory import MemoryStore
from flowly.memory.coordinator import MemoryGovernance
from flowly.memory.governance import (
    GovernanceError,
    GovernanceStore,
    STATUS_ACTIVE,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
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
        self.restored = []

    def supersede(self, tid, ended=None):
        self.superseded.append(tid)
        return 1

    def restore(self, tid):
        self.restored.append(tid)
        return 1


# -- user actions -----------------------------------------------------------


def test_accept_promotes_review_to_active(gov):
    mg = MemoryGovernance(gov)
    it = gov.add_item(kind="preference", text="x")
    gov.transition(it.id, STATUS_NEEDS_REVIEW)
    out = mg.accept(it.id)
    assert out.status == STATUS_ACTIVE
    assert gov.audit_log(it.id)[-1].actor == "user"


def test_reject(gov):
    mg = MemoryGovernance(gov)
    it = gov.add_item(kind="preference", text="x")
    out = mg.reject(it.id)
    assert out.status == STATUS_REJECTED


def test_correct_edits_text_and_activates(gov):
    mg = MemoryGovernance(gov)
    it = gov.add_item(kind="preference", text="old text")
    gov.transition(it.id, STATUS_NEEDS_REVIEW)
    out = mg.correct(it.id, "new text", confidence=0.95)
    assert out.text == "new text"
    assert out.confidence == 0.95
    assert out.status == STATUS_ACTIVE


def test_correct_missing_raises(gov):
    mg = MemoryGovernance(gov)
    with pytest.raises(GovernanceError):
        mg.correct("m_nope", "x")


# -- undo -------------------------------------------------------------------


def test_undo_restores_superseded_and_demotes_winner(gov):
    mirror = FakeMirror()
    mg = MemoryGovernance(gov, kg_mirror=mirror)
    loser = gov.add_item(kind="profile", text="email old", normalized_key="k",
                         ref_kind="kg_triple", ref_id="t_old")
    winner = gov.add_item(kind="profile", text="email new", normalized_key="k",
                          ref_kind="kg_triple", ref_id="t_new")
    gov.transition(loser.id, STATUS_ACTIVE)
    gov.transition(winner.id, STATUS_ACTIVE, supersedes=loser.id)
    gov.transition(loser.id, STATUS_SUPERSEDED)

    restored = mg.undo(loser.id)
    assert restored.status == STATUS_ACTIVE
    assert gov.get_item(winner.id).status == STATUS_SUPERSEDED  # demoted
    # KG mirror: winner's triple closed, loser's reopened
    assert "t_new" in mirror.superseded
    assert "t_old" in mirror.restored
    # exactly one active on the key
    assert len(gov.find_by_key("k", statuses={STATUS_ACTIVE})) == 1


def test_undo_rejects_non_superseded(gov):
    mg = MemoryGovernance(gov)
    it = gov.add_item(kind="preference", text="x")
    gov.transition(it.id, STATUS_ACTIVE)
    with pytest.raises(GovernanceError):
        mg.undo(it.id)


# -- recall + privacy -------------------------------------------------------


def test_recall_excludes_secret_includes_normal(gov):
    mg = MemoryGovernance(gov)
    pub = gov.add_item(kind="preference", text="likes tea")
    sec = gov.add_item(kind="profile", text="ssn", privacy_level="secret")
    gov.transition(pub.id, STATUS_ACTIVE)
    gov.transition(sec.id, STATUS_ACTIVE)
    out = mg.recall()
    texts = [i["text"] for i in out["items"]]
    assert "likes tea" in texts
    assert "ssn" not in texts
    assert out["count"] == 1


def test_recall_sensitive_opt_in(gov):
    mg = MemoryGovernance(gov)
    s = gov.add_item(kind="profile", text="home address", privacy_level="sensitive")
    gov.transition(s.id, STATUS_ACTIVE)
    assert mg.recall()["count"] == 0
    assert mg.recall(include_sensitive=True)["count"] == 1


def test_recall_marks_used_and_carries_provenance(gov):
    mg = MemoryGovernance(gov)
    it = gov.add_item(kind="preference", text="x", source_session="telegram:1",
                      source_message_ids=["m5"])
    gov.transition(it.id, STATUS_ACTIVE)
    out = mg.recall()
    assert out["items"][0]["source_session"] == "telegram:1"
    assert out["items"][0]["source_message_ids"] == ["m5"]
    assert gov.get_item(it.id).last_used_at is not None  # touch_used fired


def test_recall_includes_kg_summary(gov):
    mg = MemoryGovernance(gov, kg_summary_fn=lambda: "- Hakan (person): email=a@b.com")
    out = mg.recall()
    assert "email=a@b.com" in out["kg_summary"]


# -- stats + refresh --------------------------------------------------------


def test_stats(gov):
    mg = MemoryGovernance(gov)
    a = gov.add_item(kind="fact", text="f")
    gov.transition(a.id, STATUS_ACTIVE)
    gov.add_item(kind="preference", text="p")  # candidate
    s = mg.stats()
    assert s["active"] == 1
    assert s["total"] == 2


def test_ingest_append_creates_active_and_coalesces_regen(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    mg = MemoryGovernance(gov, memory_store=ms)
    it = mg.ingest_append("prefers dark mode", source_session="telegram:1")
    assert it is not None and it.status == STATUS_ACTIVE
    assert it.source_session == "telegram:1"
    # Coalesced: ingest does NOT rewrite MEMORY.md inline (cache-friendly); it
    # only marks the summary dirty. The regen happens once at end of turn.
    assert mg._summary_dirty is True
    assert ms.read_long_term() == ""              # not written yet
    out = mg.refresh_if_dirty()
    assert "prefers dark mode" in out
    assert "prefers dark mode" in ms.read_long_term()
    assert mg._summary_dirty is False
    assert mg.refresh_if_dirty() is None          # second call: no-op
    # duplicate append → no new item
    dup = mg.ingest_append("prefers   dark mode")
    assert dup is None
    assert len(gov.list_items(status=STATUS_ACTIVE)) == 1


def test_ingest_kg_fact_creates_and_supersedes(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    mirror = FakeMirror()
    mg = MemoryGovernance(gov, memory_store=ms, kg_mirror=mirror)
    f1 = mg.ingest_kg_fact("Hakan", "email", "old@x.com", "t_old")
    assert f1.status == STATUS_ACTIVE
    # same exact triple again → dedup
    assert mg.ingest_kg_fact("Hakan", "email", "old@x.com", "t_old") is None
    # new email on same subject+predicate → supersede the old fact + close its triple
    f2 = mg.ingest_kg_fact("Hakan", "email", "new@x.com", "t_new")
    assert f2.status == STATUS_ACTIVE
    assert gov.find_by_ref("kg_triple", "t_old")[0].status == STATUS_SUPERSEDED
    assert "t_old" in mirror.superseded
    # exactly one active fact on the key
    actives = [i for i in gov.list_items(status=STATUS_ACTIVE) if i.kind == "fact"]
    assert len(actives) == 1 and "new@x.com" in actives[0].text


def test_dirty_tracking(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    mg = MemoryGovernance(gov, memory_store=ms)
    assert mg.is_dirty() is False
    mg.ingest_append("a new pref")          # ingest marks dirty
    assert mg.is_dirty() is True
    mg.clear_dirty()
    assert mg.is_dirty() is False
    mg.ingest_kg_fact("Hakan", "role", "CEO", "t_role")  # kg ingest marks dirty too
    assert mg.is_dirty() is True


def test_ingest_feedback_helpful_raises_confidence(gov, tmp_path):
    mg = MemoryGovernance(gov)
    it = gov.add_item(kind="preference", text="x", confidence=0.7)
    gov.transition(it.id, STATUS_ACTIVE)
    out = mg.ingest_feedback(it.id, helpful=True, note="useful")
    assert out.confidence == pytest.approx(0.80)
    assert out.status == STATUS_ACTIVE
    assert gov.feedback_counts(it.id) == (1, 0)
    assert mg._summary_dirty is True


def test_ingest_feedback_unhelpful_lowers_and_demotes(gov):
    mg = MemoryGovernance(gov)
    it = gov.add_item(kind="preference", text="x", confidence=0.62)
    gov.transition(it.id, STATUS_ACTIVE)
    out = mg.ingest_feedback(it.id, helpful=False)   # 0.62 - 0.15 = 0.47 < 0.55
    assert out.confidence == pytest.approx(0.47)
    assert out.status == STATUS_NEEDS_REVIEW         # demoted below review floor
    assert gov.feedback_counts(it.id) == (0, 1)


def test_ingest_feedback_unhelpful_above_floor_stays_active(gov):
    mg = MemoryGovernance(gov)
    it = gov.add_item(kind="preference", text="x", confidence=0.9)
    gov.transition(it.id, STATUS_ACTIVE)
    out = mg.ingest_feedback(it.id, helpful=False)   # 0.75 ≥ floor
    assert out.status == STATUS_ACTIVE


def test_ingest_feedback_missing_raises(gov):
    mg = MemoryGovernance(gov)
    with pytest.raises(GovernanceError):
        mg.ingest_feedback("m_nope", helpful=True)


def test_recall_ordered_by_confidence(gov):
    mg = MemoryGovernance(gov)
    for text, conf in [("low", 0.6), ("high", 0.95), ("mid", 0.8)]:
        it = gov.add_item(kind="preference", text=text, confidence=conf)
        gov.transition(it.id, STATUS_ACTIVE)
    out = mg.recall()
    assert [i["text"] for i in out["items"]] == ["high", "mid", "low"]


def test_ingest_kg_fact_skips_self_referential_garbage(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    mg = MemoryGovernance(gov, memory_store=ms)
    # subject == object (agent set both to the email before knowing the name)
    out = mg.ingest_kg_fact("demo@x.com", "email", "demo@x.com", "t_garbage")
    assert out is None
    assert len(gov.list_items(kind="fact")) == 0


def test_refresh_writes_memory_md(gov, tmp_path):
    ms = MemoryStore(tmp_path / "ws")
    mg = MemoryGovernance(gov, memory_store=ms,
                          kg_summary_fn=lambda: "- KG line")
    a = gov.add_item(kind="preference", text="dark mode")
    gov.transition(a.id, STATUS_ACTIVE)
    out = mg.refresh()
    assert "dark mode" in out
    assert "KG line" in out
    assert "dark mode" in ms.read_long_term()
