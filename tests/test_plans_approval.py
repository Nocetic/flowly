"""PlanManager approval flow: approve / reject / revise / timeout, plus the
distributed-systems guards — revision conflict + decision idempotency + the
in-place revise that keeps the same plan id."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from flowly.plans.approval import PlanApprovalManager
from flowly.plans.manager import PlanManager
from flowly.plans.store import PlanStore


def _mgr(tmp_path: Path) -> PlanManager:
    return PlanManager(
        store=PlanStore(root=tmp_path, hydrate=False),
        approvals=PlanApprovalManager(),
    )


def _steps(mgr, *contents):
    return mgr.build_steps([{"id": i, "content": c} for i, c in enumerate(contents, 1)])


async def _resolve_soon(mgr, session, decision, **kw):
    await asyncio.sleep(0.02)
    cur = mgr.get_current(session)
    return await mgr.resolve_approval(
        cur.id, decision, expected_revision=cur.approval.revision, **kw
    )


@pytest.mark.asyncio
async def test_approve_executes(tmp_path: Path):
    mgr = _mgr(tmp_path)
    asyncio.create_task(_resolve_soon(mgr, "web:1", "approve", decision_id="d1"))
    plan, decision = await mgr.propose("web:1", "g", _steps(mgr, "A", "B"), timeout_s=5)
    assert decision.decision == "approve"
    assert plan.status == "executing"


@pytest.mark.asyncio
async def test_reject_stops(tmp_path: Path):
    mgr = _mgr(tmp_path)
    asyncio.create_task(_resolve_soon(mgr, "web:1", "reject", decision_id="d1"))
    plan, decision = await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=5)
    assert decision.decision == "reject"
    assert plan.status == "rejected"


@pytest.mark.asyncio
async def test_timeout_is_not_approved(tmp_path: Path):
    mgr = _mgr(tmp_path)
    plan, decision = await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=0.05)
    assert decision.decision == "timeout"
    assert plan.status == "aborted"  # nothing runs without an explicit approve


@pytest.mark.asyncio
async def test_revise_keeps_same_plan_id(tmp_path: Path):
    mgr = _mgr(tmp_path)
    ids = {}

    async def revise_then():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        ids["first"] = cur.id
        await mgr.resolve_approval(
            cur.id, "revise", feedback="add step", expected_revision=cur.approval.revision,
            decision_id="r1",
        )

    asyncio.create_task(revise_then())
    p1, d1 = await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=5)
    assert d1.decision == "revise" and d1.feedback == "add step"

    asyncio.create_task(_resolve_soon(mgr, "web:1", "approve", decision_id="r2"))
    p2, d2 = await mgr.propose("web:1", "g", _steps(mgr, "A", "B"), timeout_s=5)
    assert d2.decision == "approve"
    assert p2.id == ids["first"]  # same plan, not a new one
    assert len(p2.steps) == 2


@pytest.mark.asyncio
async def test_revision_conflict_and_idempotency(tmp_path: Path):
    mgr = _mgr(tmp_path)

    async def racer():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        # stale revision loses
        r1 = await mgr.resolve_approval(cur.id, "approve", expected_revision=999, decision_id="x")
        assert not r1.ok and r1.reason == "revision_conflict"
        # correct wins
        r2 = await mgr.resolve_approval(
            cur.id, "approve", expected_revision=cur.approval.revision, decision_id="ok"
        )
        assert r2.ok
        # same decision_id replayed → idempotent
        r3 = await mgr.resolve_approval(
            cur.id, "approve", expected_revision=cur.approval.revision, decision_id="ok"
        )
        assert r3.ok and r3.reason == "idempotent_replay"
        # a different decision after resolution → rejected
        r4 = await mgr.resolve_approval(cur.id, "reject", decision_id="z")
        assert not r4.ok and r4.reason == "already_resolved"

    asyncio.create_task(racer())
    _, decision = await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=5)
    assert decision.decision == "approve"


@pytest.mark.asyncio
async def test_resolve_unknown_plan(tmp_path: Path):
    mgr = _mgr(tmp_path)
    r = await mgr.resolve_approval("plan_nope", "approve")
    assert not r.ok and r.reason == "not_found"


@pytest.mark.asyncio
async def test_broadcast_emits_full_snapshots(tmp_path: Path):
    mgr = _mgr(tmp_path)
    events: list[tuple[str, str, int]] = []

    async def bc(name, data):
        events.append((name, data["status"], data["revision"]))

    mgr.set_on_change(bc)
    asyncio.create_task(_resolve_soon(mgr, "web:1", "approve", decision_id="d1"))
    plan, _ = await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=5)
    await mgr.update_step(plan.id, 1, "completed")
    await mgr.complete(plan.id, "done")

    names = [e[0] for e in events]
    assert "plan.updated" in names
    assert "plan.approval.requested" in names
    # revisions are monotonic across the broadcast stream
    revs = [e[2] for e in events]
    assert revs == sorted(revs)
    assert events[-1][1] == "completed"


@pytest.mark.asyncio
async def test_cron_context_auto_rejects(tmp_path: Path, monkeypatch):
    mgr = _mgr(tmp_path)
    monkeypatch.setattr("flowly.plans.approval._in_cron_context", lambda: True)
    plan, decision = await mgr.propose("cron:job", "g", _steps(mgr, "A"), timeout_s=5)
    assert decision.via == "cron"
    assert decision.decision == "reject"
    assert plan.status == "rejected"
