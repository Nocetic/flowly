"""PlanManager lifecycle: restart recovery (executing → paused), resume,
step ticks, and the forced-mode side-effect gate."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from flowly.plans.approval import PlanApprovalManager
from flowly.plans.manager import SIDE_EFFECT_TOOLS, PlanManager
from flowly.plans.store import PlanStore


def _mgr(tmp_path: Path, store: PlanStore | None = None) -> PlanManager:
    return PlanManager(
        store=store or PlanStore(root=tmp_path, hydrate=False),
        approvals=PlanApprovalManager(),
    )


def _steps(mgr, *contents):
    return mgr.build_steps([{"id": i, "content": c} for i, c in enumerate(contents, 1)])


async def _approve_soon(mgr, session):
    await asyncio.sleep(0.02)
    cur = mgr.get_current(session)
    await mgr.resolve_approval(
        cur.id, "approve", expected_revision=cur.approval.revision, decision_id="d"
    )


@pytest.mark.asyncio
async def test_restart_recovery_pauses_executing(tmp_path: Path):
    mgr = _mgr(tmp_path)
    asyncio.create_task(_approve_soon(mgr, "web:1"))
    plan, _ = await mgr.propose("web:1", "g", _steps(mgr, "A", "B"), timeout_s=5)
    await mgr.update_step(plan.id, 1, "completed")
    assert plan.status == "executing"

    # Simulate a restart: a brand-new manager over the same dir.
    store2 = PlanStore(root=tmp_path, hydrate=True)
    mgr2 = _mgr(tmp_path, store=store2)
    n = mgr2.recover_on_start()
    assert n == 1
    recovered = store2.get(plan.id)
    assert recovered.status == "paused"
    # Completed step survives the restart.
    assert recovered.get_step(1).status == "completed"


@pytest.mark.asyncio
async def test_resume_flips_paused_to_executing(tmp_path: Path):
    mgr = _mgr(tmp_path)
    asyncio.create_task(_approve_soon(mgr, "web:1"))
    plan, _ = await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=5)
    plan.status = "paused"
    plan.touch("paused")
    mgr.store.save(plan)

    resumed = await mgr.resume(plan.id)
    assert resumed is not None and resumed.status == "executing"
    # Resuming a completed plan is a no-op.
    await mgr.complete(plan.id, "done")
    assert await mgr.resume(plan.id) is None


@pytest.mark.asyncio
async def test_gate_blocks_side_effects_when_forced(tmp_path: Path):
    mgr = _mgr(tmp_path)
    mgr.arm_forced("web:1")
    assert mgr.is_gate_active("web:1")
    # side-effect tools blocked, read tools allowed
    assert mgr.gate_blocks("web:1", "exec")
    assert mgr.gate_blocks("web:1", "write_file")
    assert mgr.gate_blocks("web:1", "message")
    assert not mgr.gate_blocks("web:1", "read_file")
    assert not mgr.gate_blocks("web:1", "plan")
    mgr.disarm_forced("web:1")
    assert not mgr.gate_blocks("web:1", "exec")


@pytest.mark.asyncio
async def test_gate_active_while_awaiting_approval(tmp_path: Path):
    mgr = _mgr(tmp_path)

    async def check_then_approve():
        await asyncio.sleep(0.02)
        # While awaiting approval the gate blocks side effects.
        assert mgr.gate_blocks("web:1", "exec")
        cur = mgr.get_current("web:1")
        await mgr.resolve_approval(
            cur.id, "approve", expected_revision=cur.approval.revision, decision_id="d"
        )

    asyncio.create_task(check_then_approve())
    plan, _ = await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=5)
    # After approval the plan is executing → gate lifts.
    assert not mgr.gate_blocks("web:1", "exec")


def test_side_effect_set_covers_the_dangerous_tools():
    for t in ("exec", "write_file", "edit_file", "email", "message", "spawn"):
        assert t in SIDE_EFFECT_TOOLS
    for t in ("read_file", "list_dir", "plan", "clarify"):
        assert t not in SIDE_EFFECT_TOOLS


@pytest.mark.asyncio
async def test_abort_active_for_session(tmp_path: Path):
    mgr = _mgr(tmp_path)
    asyncio.create_task(_approve_soon(mgr, "web:1"))
    plan, _ = await mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=5)
    await mgr.abort_active_for_session("web:1", "user cleared chat")
    assert mgr.store.get(plan.id).status == "aborted"
    assert mgr.get_current("web:1") is None


@pytest.mark.asyncio
async def test_sticky_mode_toggles_and_clears_oneshot(tmp_path: Path):
    mgr = _mgr(tmp_path)
    mgr.set_sticky("web:1", True)
    assert mgr.is_sticky("web:1")
    # Sticky arms per message via the loop; simulate one armed message.
    mgr.arm_forced("web:1")
    assert mgr.gate_blocks("web:1", "exec")
    # Turning the mode off also drops the armed one-shot gate.
    mgr.set_sticky("web:1", False)
    assert not mgr.is_sticky("web:1")
    assert not mgr.gate_blocks("web:1", "exec")


def test_sticky_mode_survives_restart(tmp_path: Path):
    """/plan on, gateway restarts, mode is still on — like the exec policy."""
    mgr = _mgr(tmp_path)
    mgr.set_sticky("web:conv1", True)

    mgr2 = _mgr(tmp_path)  # fresh manager over the same dir = restart
    assert mgr2.is_sticky("web:conv1")
    assert not mgr2.is_sticky("web:other")

    # Turning it off persists too — it must not resurrect on the NEXT restart.
    mgr2.set_sticky("web:conv1", False)
    assert not _mgr(tmp_path).is_sticky("web:conv1")


# ── compaction re-injection + content bounds ────────────────────────────
# The plan survives compaction on disk, but the model's context does not:
# without the note, a mid-plan compaction leaves the model executing an
# approved contract it can no longer see. And every mutation is persisted +
# broadcast to every client, so model-authored content must be bounded.


@pytest.mark.asyncio
async def test_compaction_note_lists_only_unfinished_steps(tmp_path: Path):
    mgr = _mgr(tmp_path)
    asyncio.create_task(_approve_soon(mgr, "web:1"))
    plan, _ = await mgr.propose("web:1", "g", _steps(mgr, "A", "B", "C"), timeout_s=5)
    await mgr.update_step(plan.id, 1, "completed")
    await mgr.update_step(plan.id, 2, "in_progress")

    note = mgr.compaction_note("web:1")
    assert note is not None
    assert "do NOT re-propose" in note
    assert "1/3 steps are already finished" in note
    assert "1. A" not in note        # completed → omitted
    assert "[>] 2. B" in note        # in_progress → marked
    assert "[ ] 3. C" in note        # pending → listed
    assert "update_step" in note


def test_compaction_note_absent_without_an_active_plan(tmp_path: Path):
    assert _mgr(tmp_path).compaction_note("web:none") is None


@pytest.mark.asyncio
async def test_compaction_note_absent_while_awaiting_approval(tmp_path: Path):
    """draft/awaiting plans are still covered by the side-effect gate — the
    note is only for approved work the model could silently drift from."""
    mgr = _mgr(tmp_path)
    task = asyncio.create_task(
        mgr.propose("web:1", "g", _steps(mgr, "A"), timeout_s=0.15)
    )
    await asyncio.sleep(0.02)
    assert mgr.compaction_note("web:1") is None
    await task  # times out → not approved


def test_build_steps_caps_count_and_content(tmp_path: Path):
    mgr = _mgr(tmp_path)
    raw = [{"id": i, "content": f"s{i}"} for i in range(1, 200)]
    steps = mgr.build_steps(raw)
    assert len(steps) == 64

    long = mgr.build_steps([{"id": 1, "content": "x" * 5000}])
    assert len(long[0].content) == 2000
    assert long[0].content.endswith("… [truncated]")


@pytest.mark.asyncio
async def test_propose_caps_goal_and_details(tmp_path: Path):
    mgr = _mgr(tmp_path)
    asyncio.create_task(_approve_soon(mgr, "web:1"))
    plan, _ = await mgr.propose(
        "web:1", "g" * 9000, _steps(mgr, "A"),
        details_md="d" * 50_000, timeout_s=5,
    )
    assert len(plan.goal) == 2000
    assert len(plan.detailsMd) == 20_000
