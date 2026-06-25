"""SkillGovernance — facade over the op log + apply layer + usage + snapshots.

Backs the `flowly skill` CLI and the autonomous miner/curator. Skill ops are
auto-applied (snapshot-guarded); this exposes history (log), per-op undo, and
whole-tree rollback. Parallel to flowly/memory/coordinator.MemoryGovernance.
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from flowly.agent.skill_lifecycle import SkillLifecycle
from flowly.agent.skill_usage import STATE_ACTIVE, SkillUsageStore
from flowly.skills.apply import ApplyResult, SkillOpSpec, apply_ops
from flowly.skills.op_log import (
    ACTOR_USER,
    KIND_ARCHIVE,
    KIND_CREATE,
    KIND_DEMOTE,
    KIND_MERGE,
    STATUS_APPLIED,
    STATUS_UNDONE,
    SkillOpError,
    SkillOpLog,
)
from flowly.skills.snapshot import SkillSnapshots

_DIRTY_KEY = "curate_dirty"


class SkillGovernance:
    def __init__(
        self,
        op_log: SkillOpLog,
        usage: SkillUsageStore,
        skill_manage,
        snapshots: SkillSnapshots,
        *,
        lifecycle: Optional[SkillLifecycle] = None,
    ):
        self.log = op_log
        self.usage = usage
        self.skill_manage = skill_manage
        self.snapshots = snapshots
        self.lifecycle = lifecycle or SkillLifecycle(usage)

    # -- apply (auto) -------------------------------------------------------

    async def apply_specs(self, specs: list[SkillOpSpec], *, actor: str, reason: str) -> ApplyResult:
        return await apply_ops(
            specs, skill_manage=self.skill_manage, op_log=self.log,
            snapshots=self.snapshots, usage=self.usage, actor=actor, reason=reason,
        )

    # -- history / undo / rollback -----------------------------------------

    def list_ops(self, *, status: Optional[str] = None, limit: int = 50):
        return self.log.list_ops(status=status, limit=limit)

    async def undo(self, op_id: str) -> str:
        """Reverse a single applied op (create→archive, archive→restore,
        merge→restore siblings+archive umbrella). demote isn't surgically
        reversible → use rollback(snapshot_id)."""
        op = self.log.get(op_id)
        if op is None:
            raise SkillOpError(f"op not found: {op_id}")
        if op.status != STATUS_APPLIED:
            raise SkillOpError(f"op {op_id} is {op.status}, not applied")
        sm = self.skill_manage
        if op.kind == KIND_CREATE:
            await sm.execute("archive", name=op.draft_name)
        elif op.kind == KIND_ARCHIVE:
            await sm.execute("restore", name=op.targets[0])
            self.usage.set_state(op.targets[0], STATE_ACTIVE)
        elif op.kind == KIND_MERGE:
            for sib in op.targets:
                await sm.execute("restore", name=sib)
                self.usage.set_state(sib, STATE_ACTIVE)
            await sm.execute("archive", name=op.draft_name)
        elif op.kind == KIND_DEMOTE:
            if op.snapshot_id:
                self.snapshots.restore(op.snapshot_id)
            else:
                raise SkillOpError("demote not surgically undoable and no snapshot")
        self.log.transition(op_id, STATUS_UNDONE, actor=ACTOR_USER, reason="undo")
        return f"undone {op_id} ({op.kind})"

    def rollback(self, snapshot_id: Optional[str] = None) -> str:
        """Whole-tree restore from a snapshot (latest if not given)."""
        if snapshot_id is None:
            snaps = self.snapshots.list_snapshots()
            if not snaps:
                return "no snapshots to roll back to"
            snapshot_id = snaps[0]
        ok = self.snapshots.restore(snapshot_id)
        return f"rolled back to {snapshot_id}" if ok else f"rollback failed ({snapshot_id})"

    # -- usage / lifecycle / direct actions --------------------------------

    def usage_report(self) -> list[dict[str, Any]]:
        return [u.to_dict() for u in self.usage.all()]

    def run_staling(self):
        return self.lifecycle.run()

    async def archive(self, name: str) -> str:
        out = await self.skill_manage.execute("archive", name=name)
        if not out.lstrip().startswith("Error"):
            from flowly.agent.skill_usage import STATE_ARCHIVED
            self.usage.set_state(name, STATE_ARCHIVED)
        return out

    async def restore(self, name: str) -> str:
        out = await self.skill_manage.execute("restore", name=name)
        if not out.lstrip().startswith("Error"):
            self.usage.set_state(name, STATE_ACTIVE)
        return out

    # -- dirty (drives autonomous curation) --------------------------------

    def mark_dirty(self) -> None:
        self.log.set_meta(_DIRTY_KEY, "1")

    def is_dirty(self) -> bool:
        return self.log.get_meta(_DIRTY_KEY) == "1"

    def clear_dirty(self) -> None:
        self.log.set_meta(_DIRTY_KEY, "")
