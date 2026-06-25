"""P3 tests: confidence calibration, auto-supersede arbitration, KG mirroring."""

from __future__ import annotations

import pytest

from flowly.memory.calibration import CalibrationWeights, calibrate
from flowly.memory.dreamer import Candidate, MemoryDreamerService, MessageRow
from flowly.memory.governance import (
    STATUS_ACTIVE,
    STATUS_NEEDS_REVIEW,
    STATUS_SUPERSEDED,
    GovernanceStore,
)
from flowly.memory.kg_mirror import SqliteKGMirror


@pytest.fixture
def gov(tmp_path):
    s = GovernanceStore(tmp_path / "gov.sqlite3")
    yield s
    s.close()


# -- calibration rubric -----------------------------------------------------


def test_explicit_fresh_unconflicted_clears_auto_floor():
    c = calibrate(is_explicit=True, seen_count=1, had_conflict=False)
    assert c == pytest.approx(0.85)
    assert c >= 0.80


def test_inferred_once_lands_in_review_band():
    c = calibrate(is_explicit=False, seen_count=1)
    assert 0.55 <= c < 0.80
    assert c == pytest.approx(0.60)


def test_repetition_raises_confidence():
    low = calibrate(is_explicit=False, seen_count=1)
    high = calibrate(is_explicit=False, seen_count=4)
    assert high > low
    # capped
    capped = calibrate(is_explicit=False, seen_count=100)
    assert capped == pytest.approx(0.60 + 0.15)


def test_conflict_penalty_lowers():
    base = calibrate(is_explicit=True, had_conflict=False)
    conf = calibrate(is_explicit=True, had_conflict=True)
    assert conf == pytest.approx(base - 0.30)


def test_temporal_decay():
    fresh = calibrate(is_explicit=True, temporal=True, age_days=0.0)
    half = calibrate(is_explicit=True, temporal=True, age_days=30.0)  # one half-life
    assert half == pytest.approx(fresh * 0.5)
    # non-temporal facts don't decay
    profile = calibrate(is_explicit=True, temporal=False, age_days=365.0)
    assert profile == pytest.approx(0.85)


def test_clamped_to_unit_interval():
    w = CalibrationWeights(base=0.95, explicit_bonus=0.5)
    assert calibrate(is_explicit=True, weights=w) == 1.0
    assert calibrate(is_explicit=False, had_conflict=True,
                     weights=CalibrationWeights(base=0.1)) == 0.0


# -- calibrated dreamer -----------------------------------------------------


class _Delta:
    def __init__(self, rows):
        self.rows = rows

    def read_since(self, wm, limit):
        return [r for r in self.rows if r.id > wm][:limit]


class _Scripted:
    def __init__(self, batches):
        self.batches = batches
        self.calls = 0

    def extract(self, delta):
        b = self.batches[self.calls] if self.calls < len(self.batches) else []
        self.calls += 1
        return b


def _rows(n):
    return [MessageRow(id=i, session_key="s", role="user", content=f"m{i}", timestamp=float(i))
            for i in range(1, n + 1)]


def test_calibrated_explicit_activates(gov):
    # raw confidence intentionally 0 — calibration must drive the decision.
    cand = Candidate(kind="preference", text="uses vim", normalized_key="editor",
                     confidence=0.0, is_explicit=True)
    svc = MemoryDreamerService(gov, _Delta(_rows(1)), _Scripted([[cand]]),
                               injection_check=lambda t: False, calibrate=True)
    res = svc.run()
    assert res.activated == 1


def test_calibrated_inferred_goes_to_review(gov):
    cand = Candidate(kind="preference", text="might like vim", normalized_key="editor",
                     confidence=0.99, is_explicit=False)  # raw high, but inferred
    svc = MemoryDreamerService(gov, _Delta(_rows(1)), _Scripted([[cand]]),
                               injection_check=lambda t: False, calibrate=True)
    res = svc.run()
    assert res.activated == 0
    assert res.needs_review == 1


# -- auto-supersede ---------------------------------------------------------


def _seed_active(gov, text, key, ref_kind="inline", ref_id=None):
    item = gov.add_item(kind="profile", text=text, normalized_key=key,
                        ref_kind=ref_kind, ref_id=ref_id, confidence=0.9)
    gov.transition(item.id, STATUS_ACTIVE)
    return item


def test_explicit_confident_newcomer_supersedes(gov):
    loser = _seed_active(gov, "email old@x.com", "hakan:email")
    new = Candidate(kind="profile", text="email new@x.com", normalized_key="hakan:email",
                    confidence=0.0, is_explicit=True)
    svc = MemoryDreamerService(gov, _Delta(_rows(1)), _Scripted([[new]]),
                               injection_check=lambda t: False, calibrate=True)
    res = svc.run()
    assert res.superseded == 1
    assert res.activated == 1
    assert gov.get_item(loser.id).status == STATUS_SUPERSEDED
    winner = gov.find_by_key("hakan:email", statuses={STATUS_ACTIVE})[0]
    assert winner.text == "email new@x.com"
    assert winner.supersedes == loser.id


def test_inferred_contradiction_does_not_supersede(gov):
    loser = _seed_active(gov, "email old@x.com", "hakan:email")
    new = Candidate(kind="profile", text="email guess@x.com", normalized_key="hakan:email",
                    confidence=0.99, is_explicit=False)  # inferred
    svc = MemoryDreamerService(gov, _Delta(_rows(1)), _Scripted([[new]]),
                               injection_check=lambda t: False, calibrate=True)
    res = svc.run()
    assert res.superseded == 0
    assert res.needs_review == 1
    assert gov.get_item(loser.id).status == STATUS_ACTIVE  # untouched


def test_supersede_mirrors_into_kg(gov, tmp_path):
    from flowly.memory.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(str(tmp_path / "kg.sqlite3"))
    tid = kg.add_triple("Hakan", "email", "old@x.com", subject_type="person")
    # governance item backed by that triple, currently active
    loser = _seed_active(gov, "Hakan email old@x.com", "hakan:email",
                         ref_kind="kg_triple", ref_id=tid)
    mirror = SqliteKGMirror(str(tmp_path / "kg.sqlite3"))

    new = Candidate(kind="profile", text="Hakan email new@x.com", normalized_key="hakan:email",
                    confidence=0.0, is_explicit=True)
    svc = MemoryDreamerService(gov, _Delta(_rows(1)), _Scripted([[new]]),
                               injection_check=lambda t: False, calibrate=True, kg_mirror=mirror)
    svc.run()

    # KG triple is now temporally closed (no current 'email=old@x.com').
    facts = kg.query_entity("Hakan")
    current_emails = [f for f in facts if f.get("predicate") == "email" and f.get("valid_to") is None]
    assert all("old@x.com" not in str(f.get("object", "")) for f in current_emails)


# -- KG mirror unit ---------------------------------------------------------


def test_kg_mirror_supersede_and_restore(tmp_path):
    from flowly.memory.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(str(tmp_path / "kg.sqlite3"))
    tid = kg.add_triple("A", "works_at", "Acme", subject_type="person", object_type="company")
    mirror = SqliteKGMirror(str(tmp_path / "kg.sqlite3"))

    assert mirror.supersede(tid) == 1
    assert mirror.supersede(tid) == 0  # idempotent — already closed
    assert mirror.restore(tid) == 1    # reopened (undo)
    # now supersede works again
    assert mirror.supersede(tid) == 1
