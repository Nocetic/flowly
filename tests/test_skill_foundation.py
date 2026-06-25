"""F1-P0: skill foundation — usage telemetry, lifecycle, op log, snapshots,
config, and the skill_manage archive/restore actions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from flowly.agent.skill_usage import (
    PROV_AGENT,
    PROV_BUNDLED,
    STATE_ACTIVE,
    STATE_STALE,
    SkillUsageStore,
)
from flowly.agent.skill_lifecycle import SkillLifecycle
from flowly.skills.op_log import (
    InvalidTransition,
    STATUS_APPLIED,
    STATUS_UNDONE,
    SkillOpError,
    SkillOpLog,
)
from flowly.skills.snapshot import SkillSnapshots


# -- usage telemetry --------------------------------------------------------


def test_usage_bump_and_lazy_create(tmp_path):
    us = SkillUsageStore(tmp_path / "skills")
    assert us.get("pr-helper") is None
    us.bump_use("pr-helper", provenance=PROV_AGENT)
    rec = us.get("pr-helper")
    assert rec.use_count == 1 and rec.provenance == PROV_AGENT and rec.created_at
    us.bump_use("pr-helper")
    assert us.get("pr-helper").use_count == 2


def test_usage_reactivates_stale_on_use(tmp_path):
    us = SkillUsageStore(tmp_path / "skills")
    us.bump_use("s")
    us.set_state("s", STATE_STALE)
    us.bump_use("s")
    assert us.get("s").state == STATE_ACTIVE


# -- lifecycle --------------------------------------------------------------


def test_lifecycle_marks_old_agent_skill_stale(tmp_path):
    us = SkillUsageStore(tmp_path / "skills")
    us.bump_use("old", provenance=PROV_AGENT)
    # backdate last_used_at
    data = us._read(); data["old"]["last_used_at"] = (
        datetime.now(timezone.utc) - timedelta(days=90)
    ).isoformat(); us._write(data)
    res = SkillLifecycle(us, stale_after_days=60).run()
    assert res.marked_stale == 1
    assert us.get("old").state == STATE_STALE


def test_lifecycle_exempts_pinned_and_bundled(tmp_path):
    us = SkillUsageStore(tmp_path / "skills")
    us.bump_use("pinned", provenance=PROV_AGENT); us.set_pinned("pinned", True)
    us.bump_use("bundled", provenance=PROV_BUNDLED)
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    data = us._read()
    for n in ("pinned", "bundled"):
        data[n]["last_used_at"] = old
    us._write(data)
    res = SkillLifecycle(us, stale_after_days=60).run()
    assert res.marked_stale == 0   # pinned + bundled exempt (agent_only)


# -- op log -----------------------------------------------------------------


def test_op_log_add_transition_undo(tmp_path):
    log = SkillOpLog(tmp_path / "skill_gov.sqlite3")
    op = log.add_op(kind="create", targets=["x"], draft_name="x", rationale="r")
    assert op.status == STATUS_APPLIED
    log.transition(op.id, STATUS_UNDONE, actor="user", reason="undo")
    assert log.get(op.id).status == STATUS_UNDONE
    log.close()


def test_op_log_illegal_transition(tmp_path):
    log = SkillOpLog(tmp_path / "g.sqlite3")
    op = log.add_op(kind="archive", status="failed", targets=["x"])
    with pytest.raises(InvalidTransition):   # failed is terminal
        log.transition(op.id, STATUS_UNDONE)
    log.close()


def test_op_log_invalid_kind(tmp_path):
    log = SkillOpLog(tmp_path / "g.sqlite3")
    with pytest.raises(SkillOpError):
        log.add_op(kind="bogus")
    log.close()


def test_op_log_meta(tmp_path):
    log = SkillOpLog(tmp_path / "g.sqlite3")
    assert log.get_meta("wm") is None
    log.set_meta("wm", "42")
    assert log.get_meta("wm") == "42"
    log.close()


# -- snapshots --------------------------------------------------------------


def test_snapshot_and_restore_roundtrip(tmp_path):
    skills = tmp_path / "skills"
    (skills / "a").mkdir(parents=True)
    (skills / "a" / "SKILL.md").write_text("v1", encoding="utf-8")
    snaps = SkillSnapshots(skills_dir=skills, backups_dir=tmp_path / "backups")
    sid = snaps.snapshot(reason="test")
    assert sid is not None
    # mutate then restore
    (skills / "a" / "SKILL.md").write_text("v2", encoding="utf-8")
    (skills / "b").mkdir()
    (skills / "b" / "SKILL.md").write_text("new", encoding="utf-8")
    assert snaps.restore(sid) is True
    assert (skills / "a" / "SKILL.md").read_text() == "v1"
    assert not (skills / "b").exists()   # b didn't exist in the snapshot


# -- config -----------------------------------------------------------------


def test_skill_improvement_config_defaults():
    from flowly.config.schema import SkillImprovementConfig, AgentDefaults
    cfg = SkillImprovementConfig()
    assert cfg.enabled is False           # off until rollout
    assert cfg.stale_after_days == 60
    assert isinstance(AgentDefaults().skill_improvement, SkillImprovementConfig)


# -- archive / restore action ----------------------------------------------


def test_skill_manage_archive_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.agent.tools.skill_manage import SkillManageTool, _skills_dir, _archive_dir
    import asyncio

    skill = _skills_dir() / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\ndescription: x\n---\nbody", encoding="utf-8")
    tool = SkillManageTool()

    out = asyncio.run(tool.execute("archive", name="demo"))
    assert "Archived" in out
    assert not skill.exists()
    assert (_archive_dir() / "demo" / "SKILL.md").exists()

    out2 = asyncio.run(tool.execute("restore", name="demo"))
    assert "Restored" in out2
    assert (skill / "SKILL.md").exists()
