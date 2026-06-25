"""Deterministic, audited apply layer for skill ops (auto-applied under a
snapshot). Mirrors flowly/memory/consolidate.apply_operations: validate against
live state, never delete (archive only), record every op in the log. The LLM
proposes SkillOpSpecs; this is the only thing that writes skills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from flowly.agent.skill_usage import PROV_AGENT, STATE_ARCHIVED, SkillUsageStore
from flowly.skills.op_log import (
    ACTOR_SYSTEM,
    KIND_ARCHIVE,
    KIND_CREATE,
    KIND_DEMOTE,
    KIND_MERGE,
    STATUS_APPLIED,
    STATUS_FAILED,
    VALID_KINDS,
    SkillOpLog,
)
from flowly.skills.snapshot import SkillSnapshots


@dataclass
class SkillOpSpec:
    """A proposed skill operation (before apply)."""
    kind: str
    targets: list[str] = field(default_factory=list)
    draft_name: str = ""
    draft_content: str = ""
    draft_files: dict[str, str] = field(default_factory=dict)
    rationale: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApplyResult:
    applied: int = 0
    failed: int = 0
    op_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    snapshot_id: str = ""


def _err(result: str) -> Optional[str]:
    """SkillManageTool returns 'Error: …' on failure; return the message or None."""
    if isinstance(result, str) and result.lstrip().startswith("Error"):
        return result.strip()
    return None


async def _apply_one(spec: SkillOpSpec, skill_manage, usage: SkillUsageStore) -> None:
    """Execute one op via SkillManageTool. Raises on failure."""
    sm = skill_manage
    if spec.kind == KIND_CREATE:
        e = _err(await sm.execute("create", name=spec.draft_name, content=spec.draft_content))
        if e:
            raise RuntimeError(e)
        for path, content in spec.draft_files.items():
            _err_raise(await sm.execute("write_file", name=spec.draft_name,
                                        file_path=path, file_content=content))
        usage.set_provenance(spec.draft_name, PROV_AGENT)

    elif spec.kind == KIND_MERGE:
        # create umbrella, then archive siblings (never delete)
        _err_raise(await sm.execute("create", name=spec.draft_name, content=spec.draft_content))
        for path, content in spec.draft_files.items():
            _err_raise(await sm.execute("write_file", name=spec.draft_name,
                                        file_path=path, file_content=content))
        usage.set_provenance(spec.draft_name, PROV_AGENT)
        for sib in spec.targets:
            _err_raise(await sm.execute("archive", name=sib))
            usage.set_state(sib, STATE_ARCHIVED)

    elif spec.kind == KIND_DEMOTE:
        target = spec.targets[0]
        _err_raise(await sm.execute("edit", name=target, content=spec.draft_content))
        for path, content in spec.draft_files.items():
            _err_raise(await sm.execute("write_file", name=target,
                                        file_path=path, file_content=content))

    elif spec.kind == KIND_ARCHIVE:
        target = spec.targets[0]
        _err_raise(await sm.execute("archive", name=target))
        usage.set_state(target, STATE_ARCHIVED)
    else:
        raise RuntimeError(f"unknown op kind: {spec.kind}")


def _err_raise(result: str) -> None:
    e = _err(result)
    if e:
        raise RuntimeError(e)


async def apply_ops(
    specs: list[SkillOpSpec],
    *,
    skill_manage,
    op_log: SkillOpLog,
    snapshots: SkillSnapshots,
    usage: SkillUsageStore,
    actor: str = ACTOR_SYSTEM,
    reason: str = "auto-apply",
) -> ApplyResult:
    """Snapshot once, then apply each op; log applied/failed. Never deletes."""
    res = ApplyResult()
    valid = [s for s in specs if s.kind in VALID_KINDS]
    if not valid:
        return res
    snap_id = snapshots.snapshot(reason=reason) or ""
    res.snapshot_id = snap_id
    for spec in valid:
        try:
            await _apply_one(spec, skill_manage, usage)
            op = op_log.add_op(
                kind=spec.kind, status=STATUS_APPLIED, targets=spec.targets,
                draft_name=spec.draft_name, applied_content=spec.draft_content,
                applied_files=spec.draft_files, rationale=spec.rationale,
                evidence=spec.evidence, snapshot_id=snap_id, actor=actor, reason=reason,
            )
            res.applied += 1
            res.op_ids.append(op.id)
        except Exception as exc:
            logger.warning(f"[skill-apply] {spec.kind} {spec.targets or spec.draft_name} failed: {exc}")
            op_log.add_op(
                kind=spec.kind, status=STATUS_FAILED, targets=spec.targets,
                draft_name=spec.draft_name, rationale=spec.rationale,
                evidence=spec.evidence, snapshot_id=snap_id, actor=actor,
                reason=f"failed: {exc}"[:300],
            )
            res.failed += 1
            res.errors.append(str(exc))
    return res
