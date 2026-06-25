"""F1 apply + facade: deterministic auto-apply (create/merge/demote/archive),
never delete, per-op undo, whole-tree rollback."""

from __future__ import annotations

import pytest

from flowly.agent.skill_usage import PROV_AGENT, STATE_ARCHIVED, SkillUsageStore
from flowly.skills.apply import SkillOpSpec
from flowly.skills.governance import SkillGovernance
from flowly.skills.op_log import STATUS_APPLIED, STATUS_FAILED, STATUS_UNDONE, SkillOpLog
from flowly.skills.snapshot import SkillSnapshots


def _skill_md(desc="x", body="body"):
    return f"---\ndescription: {desc}\n---\n{body}"


@pytest.fixture
def gov(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.agent.tools.skill_manage import SkillManageTool, _skills_dir, _archive_dir
    skills = _skills_dir(); skills.mkdir(parents=True, exist_ok=True)
    g = SkillGovernance(
        SkillOpLog(tmp_path / "skill_gov.sqlite3"),
        SkillUsageStore(skills),
        SkillManageTool(),
        SkillSnapshots(skills_dir=skills, backups_dir=tmp_path / "backups"),
    )
    g._skills, g._archive = skills, _archive_dir()
    yield g
    g.log.close()


async def test_apply_create(gov):
    res = await gov.apply_specs(
        [SkillOpSpec(kind="create", draft_name="pr-helper", draft_content=_skill_md(),
                     draft_files={"references/g.md": "guide"})],
        actor="miner", reason="t")
    assert res.applied == 1 and res.failed == 0
    assert (gov._skills / "pr-helper" / "SKILL.md").exists()
    assert (gov._skills / "pr-helper" / "references" / "g.md").read_text() == "guide"
    assert gov.usage.get("pr-helper").provenance == PROV_AGENT
    assert gov.log.list_ops()[0].status == STATUS_APPLIED


async def test_apply_merge_archives_siblings(gov):
    for n in ("pr-a", "pr-b"):
        await gov.apply_specs([SkillOpSpec(kind="create", draft_name=n, draft_content=_skill_md())],
                              actor="miner", reason="seed")
    res = await gov.apply_specs(
        [SkillOpSpec(kind="merge", draft_name="pr", draft_content=_skill_md(desc="umbrella"),
                     targets=["pr-a", "pr-b"])], actor="curator", reason="merge")
    assert res.applied == 1
    assert (gov._skills / "pr" / "SKILL.md").exists()
    assert not (gov._skills / "pr-a").exists()          # archived, not deleted
    assert (gov._archive / "pr-a" / "SKILL.md").exists()
    assert gov.usage.get("pr-a").state == STATE_ARCHIVED


async def test_apply_archive_and_failure(gov):
    await gov.apply_specs([SkillOpSpec(kind="create", draft_name="old", draft_content=_skill_md())],
                          actor="miner", reason="seed")
    res = await gov.apply_specs([SkillOpSpec(kind="archive", targets=["old"])],
                                actor="curator", reason="stale")
    assert res.applied == 1 and not (gov._skills / "old").exists()
    # bad create (no frontmatter) → failed, not applied
    bad = await gov.apply_specs([SkillOpSpec(kind="create", draft_name="bad", draft_content="no fm")],
                                actor="miner", reason="t")
    assert bad.failed == 1 and bad.applied == 0
    assert gov.log.list_ops(status=STATUS_FAILED)


async def test_undo_create_archives_it(gov):
    res = await gov.apply_specs([SkillOpSpec(kind="create", draft_name="tmp", draft_content=_skill_md())],
                                actor="miner", reason="t")
    op_id = res.op_ids[0]
    await gov.undo(op_id)
    assert not (gov._skills / "tmp").exists()
    assert gov.log.get(op_id).status == STATUS_UNDONE


async def test_rollback_restores_tree(gov):
    await gov.apply_specs([SkillOpSpec(kind="create", draft_name="keep", draft_content=_skill_md())],
                          actor="miner", reason="seed")
    # a second op snapshots the tree (with 'keep') before creating 'extra'
    res = await gov.apply_specs([SkillOpSpec(kind="create", draft_name="extra", draft_content=_skill_md())],
                                actor="miner", reason="t")
    gov.rollback(res.snapshot_id)
    assert (gov._skills / "keep").exists()
    assert not (gov._skills / "extra").exists()   # rolled back


def test_no_delete_action_used(gov):
    # apply layer must never call skill_manage delete — guard by source inspection
    import inspect
    from flowly.skills import apply as apply_mod
    src = inspect.getsource(apply_mod)
    assert '"delete"' not in src and "'delete'" not in src
