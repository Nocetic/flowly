"""PlanTool: the agent-facing surface. Verifies propose blocks-then-reports,
update_step/complete tick the plan, and the emergency kill switch."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from flowly.agent.tools.plan import PlanTool
from flowly.plans.approval import PlanApprovalManager
from flowly.plans.manager import PlanManager
from flowly.plans.store import PlanStore


class _ExecPolicy:
    def __init__(self, unattended: bool = False, *, fail: bool = False):
        self.unattended = unattended
        self.fail = fail

    def runs_unattended(self) -> bool:
        if self.fail:
            raise RuntimeError("policy unavailable")
        return self.unattended


class _Registry:
    _active_session_id = "web:1"
    _active_run_id = ""

    def __init__(self, unattended: bool = False, *, fail: bool = False):
        self.exec = _ExecPolicy(unattended, fail=fail)

    def get(self, name: str):
        return self.exec if name == "exec" else None


def _tool(
    tmp_path: Path, *, unattended: bool = False, policy_failure: bool = False
) -> tuple[PlanTool, PlanManager]:
    mgr = PlanManager(
        store=PlanStore(root=tmp_path, hydrate=False),
        approvals=PlanApprovalManager(),
    )
    tool = PlanTool(
        manager=mgr,
        registry=_Registry(unattended, fail=policy_failure),
        default_session_key="web:1",
    )
    return tool, mgr


@pytest.mark.asyncio
async def test_propose_approved_then_execute(tmp_path: Path):
    tool, mgr = _tool(tmp_path)

    async def approve():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        await mgr.resolve_approval(
            cur.id, "approve", expected_revision=cur.approval.revision, decision_id="d"
        )

    asyncio.create_task(approve())
    out = json.loads(
        await tool.execute(
            action="propose",
            goal="Ship the feature",
            title="Ship",
            steps=[{"id": 1, "content": "Do A"}, {"id": 2, "content": "Do B"}],
        )
    )
    assert out["decision"] == "approved"
    assert out["plan"]["status"] == "executing"

    # tick a step
    up = json.loads(await tool.execute(action="update_step", id=1, status="completed"))
    assert up["success"] and up["progress"]["completed"] == 1

    # complete
    done = json.loads(await tool.execute(action="complete", summary="done"))
    assert done["success"] and done["plan"]["status"] == "completed"


@pytest.mark.asyncio
async def test_yolo_plan_auto_starts(tmp_path: Path):
    tool, mgr = _tool(tmp_path, unattended=True)

    out = json.loads(
        await tool.execute(
            action="propose",
            goal="Ship the feature",
            steps=[{"id": 1, "content": "Do A"}],
        )
    )

    assert out["decision"] == "approved"
    assert out["via"] == "policy"
    assert out["plan"]["mode"] == "auto"
    assert out["plan"]["status"] == "executing"
    assert out["plan"]["approval"] is None
    assert not mgr.is_gate_active("web:1")


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_review", [False, True])
async def test_yolo_still_waits_for_forced_or_requested_review(
    tmp_path: Path, explicit_review: bool
):
    tool, mgr = _tool(tmp_path, unattended=True)
    if not explicit_review:
        mgr.arm_forced("web:1")

    async def approve():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        assert cur.status == "awaiting_approval"
        await mgr.resolve_approval(
            cur.id, "approve", expected_revision=cur.approval.revision,
            decision_id="d",
        )

    asyncio.create_task(approve())
    out = json.loads(
        await tool.execute(
            action="propose",
            goal="Review first",
            steps=[{"id": 1, "content": "Do A"}],
            requiresApproval=explicit_review,
        )
    )

    assert out["decision"] == "approved"
    assert out["via"] == "surface"
    assert out["plan"]["mode"] == "forced"


@pytest.mark.asyncio
async def test_policy_failure_fails_closed_to_review(tmp_path: Path):
    tool, mgr = _tool(tmp_path, policy_failure=True)

    async def approve():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        await mgr.resolve_approval(
            cur.id, "approve", expected_revision=cur.approval.revision,
            decision_id="d",
        )

    asyncio.create_task(approve())
    out = json.loads(
        await tool.execute(
            action="propose",
            goal="Fail closed",
            steps=[{"id": 1, "content": "Do A"}],
        )
    )

    assert out["via"] == "surface"
    assert out["plan"]["mode"] == "forced"


@pytest.mark.asyncio
async def test_propose_rejected(tmp_path: Path):
    tool, mgr = _tool(tmp_path)

    async def reject():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        await mgr.resolve_approval(
            cur.id, "reject", expected_revision=cur.approval.revision, decision_id="d"
        )

    asyncio.create_task(reject())
    out = json.loads(
        await tool.execute(action="propose", goal="g", steps=[{"id": 1, "content": "X"}])
    )
    assert out["decision"] == "rejected"


@pytest.mark.asyncio
async def test_rejected_one_shot_plan_clears_forced_gate(tmp_path: Path):
    tool, mgr = _tool(tmp_path)
    mgr.arm_forced("web:1")

    async def reject():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        await mgr.resolve_approval(
            cur.id, "reject", expected_revision=cur.approval.revision,
            decision_id="d",
        )

    asyncio.create_task(reject())
    out = json.loads(
        await tool.execute(
            action="propose", goal="g", steps=[{"id": 1, "content": "X"}]
        )
    )

    assert out["decision"] == "rejected"
    assert not mgr.is_forced_pending("web:1")
    assert not mgr.is_gate_active("web:1")


@pytest.mark.asyncio
async def test_propose_revise_returns_feedback(tmp_path: Path):
    tool, mgr = _tool(tmp_path)

    async def revise():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        await mgr.resolve_approval(
            cur.id, "revise", feedback="split step 1",
            expected_revision=cur.approval.revision, decision_id="d",
        )

    asyncio.create_task(revise())
    out = json.loads(
        await tool.execute(action="propose", goal="g", steps=[{"id": 1, "content": "X"}])
    )
    assert out["decision"] == "revise"
    assert out["feedback"] == "split step 1"


@pytest.mark.asyncio
async def test_propose_validates_input(tmp_path: Path):
    tool, _ = _tool(tmp_path)
    assert "error" in json.loads(await tool.execute(action="propose", goal=""))
    assert "error" in json.loads(
        await tool.execute(action="propose", goal="g", steps=[])
    )


@pytest.mark.asyncio
async def test_view_and_unknown_action(tmp_path: Path):
    tool, _ = _tool(tmp_path)
    v = json.loads(await tool.execute(action="view"))
    assert v["plan"] is None
    bad = json.loads(await tool.execute(action="frobnicate"))
    assert "error" in bad


@pytest.mark.asyncio
async def test_kill_switch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLOWLY_PLAN_ENABLED", "0")
    tool, _ = _tool(tmp_path)
    out = json.loads(await tool.execute(action="view"))
    assert "disabled" in out["error"]
