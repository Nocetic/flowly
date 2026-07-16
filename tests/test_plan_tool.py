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


def _tool(tmp_path: Path) -> tuple[PlanTool, PlanManager]:
    mgr = PlanManager(
        store=PlanStore(root=tmp_path, hydrate=False),
        approvals=PlanApprovalManager(),
    )
    tool = PlanTool(manager=mgr, default_session_key="web:1")
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
