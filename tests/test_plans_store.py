"""PlanStore: atomic write-through + hydration from disk (the real resume
source, unlike the browser planner which writes but never reads back)."""

from __future__ import annotations

from pathlib import Path

from flowly.plans.models import GeneralPlan, PlanStep
from flowly.plans.store import PlanStore, safe_filename


def _plan(session="web:1", goal="g", n=2) -> GeneralPlan:
    steps = [PlanStep(id=i, content=f"step {i}") for i in range(1, n + 1)]
    return GeneralPlan.new(session, goal, steps, title="T")


def test_save_then_hydrate_roundtrips(tmp_path: Path):
    store = PlanStore(root=tmp_path, hydrate=False)
    plan = _plan()
    plan.status = "executing"
    plan.touch("go")
    store.save(plan)

    # Fresh store over the same dir must recover it (hydration).
    store2 = PlanStore(root=tmp_path, hydrate=True)
    got = store2.get(plan.id)
    assert got is not None
    assert got.status == "executing"
    assert got.revision == plan.revision
    assert [s.content for s in got.steps] == ["step 1", "step 2"]


def test_current_for_session_skips_terminal(tmp_path: Path):
    store = PlanStore(root=tmp_path, hydrate=False)
    old = _plan()
    old.status = "aborted"
    old.touch("done")
    store.save(old)
    assert store.current_for_session("web:1") is None

    live = _plan()
    live.status = "executing"
    live.touch("go")
    store.save(live)
    cur = store.current_for_session("web:1")
    assert cur is not None and cur.id == live.id


def test_atomic_write_leaves_no_partial(tmp_path: Path):
    store = PlanStore(root=tmp_path, hydrate=False)
    plan = _plan()
    store.save(plan)
    sess_dir = tmp_path / safe_filename("web:1")
    # No leftover temp files, exactly one plan json + one revisions log.
    names = sorted(p.name for p in sess_dir.iterdir())
    assert f"{plan.id}.json" in names
    assert f"{plan.id}.revisions.log" in names
    assert not any(n.startswith(".") and "tmp" in n for n in names)


def test_corrupt_file_is_skipped_not_fatal(tmp_path: Path):
    store = PlanStore(root=tmp_path, hydrate=False)
    plan = _plan()
    store.save(plan)
    # Drop a garbage + a foreign-schema file next to it.
    sess_dir = tmp_path / safe_filename("web:1")
    (sess_dir / "plan_garbage.json").write_text("{not json", encoding="utf-8")
    (sess_dir / "plan_foreign.json").write_text(
        '{"id":"plan_x","sessionKey":"web:1","steps":[{"id":1,"content":"c",'
        '"successCriteria":"legacy browser field"}]}',
        encoding="utf-8",
    )
    store2 = PlanStore(root=tmp_path, hydrate=True)
    # The good plan loads; the foreign one loads too (unknown keys filtered).
    assert store2.get(plan.id) is not None


def test_persist_disabled_via_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLOWLY_PLAN_PERSIST", "0")
    store = PlanStore(root=tmp_path, hydrate=False)
    store.save(_plan())
    # Nothing written to disk.
    assert not any(tmp_path.rglob("plan_*.json"))


def test_safe_filename_strips_separators():
    assert "/" not in safe_filename("web:../../etc/passwd")
    assert safe_filename("") == "_unknown"
