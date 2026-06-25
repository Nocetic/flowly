"""F1/F4: trajectory miner (detect_signals), proposal parsing, curate context,
and the skill_improve tool driving auto-apply with a fake provider."""

from __future__ import annotations

import pytest

from flowly.memory.dreamer import MessageRow
from flowly.skills.miner import detect_signals
from flowly.skills.curator import build_curate_context
from flowly.skills.proposer import parse_specs


def _msgs(pairs):
    # pairs: list of (session_key, content)
    return [MessageRow(id=i + 1, session_key=s, role="user", content=c, timestamp=float(i))
            for i, (s, c) in enumerate(pairs)]


# -- detect_signals ---------------------------------------------------------


def test_detect_signals_fires_on_cross_session_repeat():
    delta = _msgs([
        ("s1", "generate the weekly revenue report"),
        ("s2", "generate the weekly revenue report"),
        ("s3", "generate the weekly revenue report"),
    ])
    sig = detect_signals(delta, min_evidence_sessions=2, min_repeat_count=3)
    assert sig is not None and sig.repeated[0].count == 3


def test_detect_signals_none_below_threshold():
    delta = _msgs([("s1", "do a one-off thing"), ("s1", "do a one-off thing")])
    assert detect_signals(delta, min_evidence_sessions=2, min_repeat_count=3) is None


def test_detect_signals_ignores_non_user_and_short():
    delta = [MessageRow(id=1, session_key="s", role="assistant", content="x" * 50, timestamp=1.0)]
    assert detect_signals(delta) is None


# -- parse_specs ------------------------------------------------------------


def test_parse_create_op():
    raw = '{"ops":[{"op":"create","name":"rev-report","skill_md":"---\\ndescription: r\\n---\\nbody","rationale":"x"}]}'
    specs = parse_specs(raw)
    assert len(specs) == 1 and specs[0].kind == "create" and specs[0].draft_name == "rev-report"


def test_parse_fenced_and_filters_invalid():
    raw = '```json\n{"ops":[{"op":"merge","targets":["a","b"],"draft_name":"u","draft_content":"---\\ndescription: u\\n---\\n"},{"op":"bogus"},{"op":"archive"}]}\n```'
    specs = parse_specs(raw)
    kinds = [s.kind for s in specs]
    assert kinds == ["merge"]   # bogus dropped; archive w/o targets dropped


def test_parse_garbage():
    assert parse_specs("not json") == []


# -- curate context ---------------------------------------------------------


def test_build_curate_context_excludes_archived():
    rows = [{"name": "a", "state": "active"}, {"name": "b", "state": "archived"}]
    ctx = build_curate_context(rows)
    assert [s["name"] for s in ctx["skills"]] == ["a"]


# -- skill_improve tool (mine) with fake provider ---------------------------


class _Delta:
    def __init__(self, rows):
        self.rows = rows

    def read_since(self, wm, limit):
        return [r for r in self.rows if r.id > wm][:limit]


class _FakeDelta2:
    content = None


class _FakeProvider:
    def __init__(self, text):
        self._text = text

    async def chat_stream(self, messages, **kw):
        class D:
            pass
        d = D(); d.content = self._text
        yield d


async def test_skill_improve_mine_applies(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.agent.tools.skill_manage import SkillManageTool, _skills_dir
    from flowly.agent.skill_usage import SkillUsageStore
    from flowly.skills.op_log import SkillOpLog
    from flowly.skills.snapshot import SkillSnapshots
    from flowly.skills.governance import SkillGovernance
    from flowly.agent.tools.skill_improve import SkillImproveTool

    skills = _skills_dir(); skills.mkdir(parents=True, exist_ok=True)
    gov = SkillGovernance(
        SkillOpLog(tmp_path / "sg.sqlite3"), SkillUsageStore(skills),
        SkillManageTool(), SkillSnapshots(skills_dir=skills, backups_dir=tmp_path / "b"),
    )
    delta = _Delta(_msgs([("s1", "make the weekly report"), ("s2", "make the weekly report"),
                          ("s3", "make the weekly report")]))
    llm = _FakeProvider('{"ops":[{"op":"create","name":"weekly-report",'
                        '"skill_md":"---\\ndescription: weekly report\\n---\\nsteps","rationale":"recurring"}]}')
    tool = SkillImproveTool(facade=gov, provider=llm, model="m", delta_source=delta,
                            skills_loader=None, usage=gov.usage,
                            min_evidence_sessions=2, min_repeat_count=3)
    out = await tool.execute(mode="mine", dry_run=False)
    assert "applied=1" in out
    assert (skills / "weekly-report" / "SKILL.md").exists()
    gov.log.close()


async def test_skill_improve_mine_dry_run_no_write(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.agent.tools.skill_manage import SkillManageTool, _skills_dir
    from flowly.agent.skill_usage import SkillUsageStore
    from flowly.skills.op_log import SkillOpLog
    from flowly.skills.snapshot import SkillSnapshots
    from flowly.skills.governance import SkillGovernance
    from flowly.agent.tools.skill_improve import SkillImproveTool

    skills = _skills_dir(); skills.mkdir(parents=True, exist_ok=True)
    gov = SkillGovernance(SkillOpLog(tmp_path / "sg.sqlite3"), SkillUsageStore(skills),
                          SkillManageTool(), SkillSnapshots(skills_dir=skills, backups_dir=tmp_path / "b"))
    delta = _Delta(_msgs([("s1", "make the weekly report"), ("s2", "make the weekly report"),
                          ("s3", "make the weekly report")]))
    llm = _FakeProvider('{"ops":[{"op":"create","name":"weekly-report","skill_md":"---\\ndescription: r\\n---\\nx","rationale":"r"}]}')
    tool = SkillImproveTool(facade=gov, provider=llm, model="m", delta_source=delta,
                            skills_loader=None, usage=gov.usage, min_repeat_count=3)
    out = await tool.execute(mode="mine", dry_run=True)
    assert "dry-run" in out
    assert not (skills / "weekly-report").exists()
    gov.log.close()
