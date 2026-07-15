"""feature_rpc plan surface — served identically over gateway + relay. Verifies
plan.* dispatch, chat.inflight carrying the plan, and capability advertisement."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import flowly.channels.feature_rpc as frpc
import flowly.plans.manager as manager_mod
from flowly.plans.approval import PlanApprovalManager
from flowly.plans.manager import PlanManager
from flowly.plans.store import PlanStore


@pytest.fixture
def mgr(tmp_path: Path, monkeypatch):
    m = PlanManager(
        store=PlanStore(root=tmp_path, hydrate=False),
        approvals=PlanApprovalManager(),
    )
    # feature_rpc handlers read the process singleton — point it at ours.
    monkeypatch.setattr(manager_mod, "_singleton", m)
    return m


def _steps(m, *contents):
    return m.build_steps([{"id": i, "content": c} for i, c in enumerate(contents, 1)])


def test_capabilities_advertise_plan_methods():
    for method in ("plan.get", "plan.list", "plan.resolve", "plan.resume", "plan.cancel"):
        assert method in frpc.FEATURE_METHODS


@pytest.mark.asyncio
async def test_plan_get_empty(mgr):
    result, _ = await frpc.dispatch("plan.get", {"sessionKey": "web:1"})
    assert result == {"plan": None}


@pytest.mark.asyncio
async def test_resolve_over_rpc(mgr):
    async def resolve():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        res, _ = await frpc.dispatch(
            "plan.resolve",
            {
                "planId": cur.id,
                "decision": "approve",
                "expectedRevision": cur.approval.revision,
                "decisionId": "d1",
            },
        )
        assert res["resolved"] is True

    asyncio.create_task(resolve())
    plan, decision = await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=5)
    assert decision.decision == "approve"

    # plan.get now returns the executing plan
    got, _ = await frpc.dispatch("plan.get", {"sessionKey": "web:1"})
    assert got["plan"]["status"] == "executing"


@pytest.mark.asyncio
async def test_chat_inflight_carries_plan(mgr):
    async def approve():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        await frpc.dispatch(
            "plan.resolve",
            {"planId": cur.id, "decision": "approve",
             "expectedRevision": cur.approval.revision, "decisionId": "d"},
        )

    asyncio.create_task(approve())
    await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=5)

    inflight, _ = await frpc.dispatch("chat.inflight", {"sessionKey": "web:1"})
    assert "plan" in inflight
    assert inflight["plan"]["status"] == "executing"


@pytest.mark.asyncio
async def test_resolve_rejects_bad_decision(mgr):
    with pytest.raises(frpc.FeatureRpcError):
        await frpc.dispatch("plan.resolve", {"planId": "p", "decision": "maybe"})
    with pytest.raises(frpc.FeatureRpcError):
        await frpc.dispatch("plan.resolve", {"decision": "approve"})


@pytest.mark.asyncio
async def test_cancel_over_rpc(mgr):
    async def cancel():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        res, _ = await frpc.dispatch("plan.cancel", {"planId": cur.id})
        assert res["plan"]["status"] == "aborted"

    asyncio.create_task(cancel())
    # propose blocks; cancel doesn't resolve the approval, so it will time out —
    # use a short window and just assert cancel marked it aborted mid-flight.
    plan, _ = await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=0.3)
    assert mgr.store.get(plan.id).status == "aborted"


@pytest.mark.asyncio
async def test_plan_mode_rpc_roundtrip(mgr):
    got, _ = await frpc.dispatch("plan.mode.get", {"sessionKey": "web:1"})
    assert got == {"sticky": False}
    set_, _ = await frpc.dispatch("plan.mode.set", {"sessionKey": "web:1", "sticky": True})
    assert set_ == {"sticky": True}
    got2, _ = await frpc.dispatch("plan.mode.get", {"sessionKey": "web:1"})
    assert got2 == {"sticky": True}
    assert mgr.is_sticky("web:1")
    off, _ = await frpc.dispatch("plan.mode.set", {"sessionKey": "web:1", "sticky": False})
    assert off == {"sticky": False}


@pytest.mark.asyncio
async def test_plan_mode_set_requires_session(mgr):
    with pytest.raises(frpc.FeatureRpcError):
        await frpc.dispatch("plan.mode.set", {"sticky": True})
