"""Plan tracking must not cost model round trips.

Every model-visible plan call is a full LLM turn unless it rides along with a
real tool call, so the server owns the bookkeeping: a started plan begins step
1, finishing a step begins the next, several steps tick in one call, and the
plan completes itself when nothing is left.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from flowly.agent.prompt_blocks import build_plan_mode_block
from flowly.agent.tools.plan import PlanTool
from flowly.plans.approval import PlanApprovalManager
from flowly.plans.manager import PlanManager
from flowly.plans.store import PlanStore


class _Exec:
    def runs_unattended(self) -> bool:
        return True


class _Registry:
    _active_session_id = "web:1"
    _active_run_id = ""

    def get(self, name: str):
        return _Exec() if name == "exec" else None


def _tool(tmp_path: Path) -> tuple[PlanTool, PlanManager]:
    mgr = PlanManager(store=PlanStore(root=tmp_path, hydrate=False),
                      approvals=PlanApprovalManager())
    return PlanTool(manager=mgr, registry=_Registry(), default_session_key="web:1"), mgr


async def _propose(tool: PlanTool, *contents: str) -> dict:
    return json.loads(await tool.execute(
        action="propose", goal="g",
        steps=[{"id": i, "content": c} for i, c in enumerate(contents, start=1)],
    ))


def _statuses(mgr: PlanManager) -> list[str]:
    plan = mgr.list_for_session("web:1")[0]
    return [s.status for s in plan.steps]


@pytest.mark.asyncio
async def test_started_plan_puts_first_step_in_progress(tmp_path):
    tool, mgr = _tool(tmp_path)
    out = await _propose(tool, "A", "B", "C")
    assert out["decision"] == "approved"
    assert _statuses(mgr) == ["in_progress", "pending", "pending"]
    assert "same response" in out["note"]


@pytest.mark.asyncio
async def test_finishing_a_step_starts_the_next(tmp_path):
    tool, mgr = _tool(tmp_path)
    await _propose(tool, "A", "B", "C")
    out = json.loads(await tool.execute(action="update", steps=[{"id": 1, "status": "completed"}]))
    assert out == {"ok": True, "status": "executing", "progress": "1/3",
                   "current": {"id": 2, "content": "B"}}
    assert _statuses(mgr) == ["completed", "in_progress", "pending"]


@pytest.mark.asyncio
async def test_batch_finishing_every_step_completes_the_plan(tmp_path):
    tool, mgr = _tool(tmp_path)
    events: list[str] = []

    async def bc(name, data):
        events.append(data["status"])

    mgr.set_on_change(bc)
    await _propose(tool, "A", "B", "C")
    events.clear()
    out = json.loads(await tool.execute(action="update", steps=[
        {"id": 1, "status": "completed"}, {"id": 2, "status": "skipped"},
        {"id": 3, "status": "completed"},
    ]))
    assert out["status"] == "completed" and out["progress"] == "3/3"
    assert mgr.get_current("web:1") is None
    assert events == ["completed"]          # one mutation, one broadcast
    # An explicit complete afterwards is harmless, not an error.
    done = json.loads(await tool.execute(action="complete", summary="all set"))
    assert done == {"ok": True, "status": "completed"}
    assert mgr.list_for_session("web:1")[0].completionSummary == "all set"


@pytest.mark.asyncio
async def test_blocked_step_never_auto_completes(tmp_path):
    tool, mgr = _tool(tmp_path)
    await _propose(tool, "A", "B")
    out = json.loads(await tool.execute(action="update", steps=[
        {"id": 1, "status": "completed"}, {"id": 2, "status": "blocked"}]))
    assert out["status"] == "executing"
    assert _statuses(mgr) == ["completed", "blocked"]


@pytest.mark.asyncio
async def test_invalid_batch_changes_nothing(tmp_path):
    tool, mgr = _tool(tmp_path)
    await _propose(tool, "A", "B")
    out = json.loads(await tool.execute(action="update", steps=[
        {"id": 1, "status": "completed"}, {"id": 9, "status": "completed"}]))
    assert "no step 9" in out["error"]
    out = json.loads(await tool.execute(action="update", steps=[{"id": 1, "status": "done"}]))
    assert "invalid status" in out["error"]
    assert _statuses(mgr) == ["in_progress", "pending"]


@pytest.mark.asyncio
async def test_legacy_update_step_still_works(tmp_path):
    tool, mgr = _tool(tmp_path)
    await _propose(tool, "A", "B")
    out = json.loads(await tool.execute(action="update_step", id=1, status="completed"))
    assert out["ok"] and out["progress"] == "1/2"
    assert _statuses(mgr) == ["completed", "in_progress"]


@pytest.mark.asyncio
async def test_approved_plan_starts_its_first_step(tmp_path):
    mgr = PlanManager(store=PlanStore(root=tmp_path, hydrate=False),
                      approvals=PlanApprovalManager())

    async def approve():
        await asyncio.sleep(0.02)
        cur = mgr.get_current("web:1")
        await mgr.resolve_approval(cur.id, "approve",
                                   expected_revision=cur.approval.revision, decision_id="d")

    asyncio.create_task(approve())
    plan, _ = await mgr.propose("web:1", "g", mgr.build_steps(
        [{"content": "A"}, {"content": "B"}]), timeout_s=5)
    assert [s.status for s in plan.steps] == ["in_progress", "pending"]


def test_guidance_tells_the_model_not_to_spend_round_trips():
    tool = PlanTool(manager=PlanManager(store=PlanStore(root=Path("/nonexistent"), hydrate=False),
                                        approvals=PlanApprovalManager()))
    for text in (tool.description, build_plan_mode_block()):
        assert "same response" in text.lower() or "SAME response" in text
        assert "one or two tool calls" in text
        assert "update_step" not in text
    assert tool.parameters["properties"]["action"]["enum"] == [
        "propose", "update", "complete", "block", "abort", "view"]
