"""A built-in the owner removes stays removed; a skill turned off is out of everything the agent reads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowly.agent import skills as skills_module
from flowly.agent.skills import SkillsLoader, clear_skills_snapshot
from flowly.channels import feature_rpc
from flowly.skills import state, sync


def _write(root: Path, name: str, description: str = "test skill") -> Path:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
                                    encoding="utf-8")
    return skill


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A Flowly that ships `notion` and `weather`, and an agent home synced from it."""
    bundled = tmp_path / "package"
    _write(bundled, "notion", "Notion pages.")
    _write(bundled, "weather", "Forecasts.")
    home = tmp_path / "home"
    managed = home / "skills"
    monkeypatch.setattr(state, "BUNDLED_DIR", bundled)
    monkeypatch.setattr(skills_module, "BUILTIN_SKILLS_DIR", bundled)
    monkeypatch.setattr(skills_module, "_managed_skills_dir", lambda: managed)
    monkeypatch.setattr(skills_module, "_skills_snapshot_path", lambda: home / ".skills_prompt_snapshot.json")
    monkeypatch.setattr(sync, "_get_bundled_dir", lambda: bundled)
    monkeypatch.setattr(sync, "_get_user_dir", lambda: managed)
    monkeypatch.setattr(feature_rpc, "get_flowly_home", lambda: home)
    monkeypatch.setattr("flowly.hub.manager.get_flowly_home", lambda: home)
    clear_skills_snapshot()
    sync.sync_skills()
    yield managed
    clear_skills_snapshot()


def _names(managed: Path, **kwargs) -> set[str]:
    loader = SkillsLoader(managed.parent / "workspace", managed_skills_dir=managed)
    return {s["name"] for s in loader.list_skills(filter_unavailable=False, **kwargs)}


def test_before_the_first_sync_the_package_copy_loads(tmp_path, monkeypatch):
    bundled = tmp_path / "package"
    _write(bundled, "notion")
    monkeypatch.setattr(state, "BUNDLED_DIR", bundled)
    loader = SkillsLoader(tmp_path / "ws", builtin_skills_dir=bundled, managed_skills_dir=tmp_path / "empty")
    assert {s["name"] for s in loader.list_skills(filter_unavailable=False)} == {"notion"}


def test_a_removed_built_in_stays_removed_across_syncs(agent):
    assert _names(agent) == {"notion", "weather"}
    import shutil

    shutil.rmtree(agent / "notion")
    assert _names(agent) == {"weather"}
    loader = SkillsLoader(agent.parent / "workspace", managed_skills_dir=agent)
    assert loader.load_skill("notion") is None
    # The sync used to drop the entry, and the sync after that copied it back.
    sync.sync_skills()
    sync.sync_skills()
    assert not (agent / "notion").exists()
    assert state.removed_builtins(agent) == ["notion"]


def test_a_built_in_shipped_after_the_last_sync_loads_until_synced(agent):
    _write(state.BUNDLED_DIR, "maps")
    assert "maps" in _names(agent)
    sync.sync_skills()
    assert (agent / "maps" / "SKILL.md").exists()


def test_restore_brings_back_the_shipped_copy_and_sync_keeps_updating_it(agent):
    import shutil

    shutil.rmtree(agent / "notion")
    assert state.restore_builtin(agent, "notion")
    assert _names(agent) == {"notion", "weather"}
    assert state.removed_builtins(agent) == []
    # Restored as shipped: a newer Flowly's copy replaces it, like any unmodified built-in.
    (state.BUNDLED_DIR / "notion" / "SKILL.md").write_text("---\nname: notion\ndescription: v2\n---\n")
    sync.sync_skills()
    assert "v2" in (agent / "notion" / "SKILL.md").read_text()
    assert not state.restore_builtin(agent, "weather")  # not removed
    assert not state.restore_builtin(agent, "unknown")


def test_a_skill_turned_off_is_out_of_everything_the_agent_reads(agent, tmp_path):
    state.set_enabled(agent, "weather", False)
    assert _names(agent) == {"notion"}
    assert _names(agent, include_disabled=True) == {"notion", "weather"}
    loader = SkillsLoader(agent.parent / "workspace", managed_skills_dir=agent)
    assert loader.load_skill("weather") is None
    assert "weather" not in loader.build_skills_summary()

    from flowly import profile

    assert profile._installed_skill_count(agent.parent) == 1

    state.set_enabled(agent, "weather", True)
    assert "weather" in loader.build_skills_summary()  # the cached summary follows the change


async def test_skill_view_cannot_open_a_removed_or_disabled_skill(agent, monkeypatch):
    from flowly.agent.tools.skill_view import SkillViewTool

    tool = SkillViewTool(agent.parent / "workspace")
    tool._managed_dir, tool._builtin_dir = agent, state.BUNDLED_DIR
    import shutil

    shutil.rmtree(agent / "notion")
    state.set_enabled(agent, "weather", False)
    for name in ("notion", "weather"):
        assert "not found" in await tool.execute(name=name)
    listed = json.loads(await tool.execute(action="list"))
    assert listed["total"] == 0


async def test_the_apps_list_says_what_each_skill_is_and_what_can_be_done(agent):
    _write(agent, "my-notes")  # the agent's own
    market = _write(agent, "pdf-tools")
    (market / ".flowly-skill.json").write_text(json.dumps({"slug": "pdf-tools", "source": "pdf-tools"}))
    state.set_enabled(agent, "my-notes", False)

    rows = {row["slug"]: row for row in feature_rpc.skills_list()["skills"]}
    assert (rows["notion"]["source"], rows["notion"]["removable"]) == ("bundled", True)
    assert (rows["pdf-tools"]["source"], rows["pdf-tools"]["removable"]) == ("marketplace", True)
    assert (rows["my-notes"]["source"], rows["my-notes"]["removable"], rows["my-notes"]["enabled"]) == (
        "local", False, False,
    )
    assert rows["notion"]["description"] == "Notion pages."


async def test_remove_toggle_and_restore_over_the_wire(agent):
    result = await feature_rpc.skills_remove({"slug": "notion", "restart": False})
    assert result == {"ok": True, "restorable": True, "willRestart": False}
    listed = feature_rpc.skills_list()
    assert "notion" not in {row["slug"] for row in listed["skills"]}
    assert listed["removedBuiltIns"] == [
        {"slug": "notion", "name": "notion", "description": "Notion pages.", "category": "General"}
    ]

    _write(agent, "my-notes")
    with pytest.raises(feature_rpc.FeatureRpcError) as raised:
        await feature_rpc.skills_remove({"slug": "my-notes", "restart": False})
    assert raised.value.code == "NOT_REMOVABLE"
    assert (agent / "my-notes").exists()

    assert await feature_rpc.skills_toggle({"slug": "my-notes", "enabled": False}) == {
        "ok": True, "slug": "my-notes", "enabled": False,
    }
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.skills_toggle({"slug": "my-notes", "enabled": "no"})
    with pytest.raises(feature_rpc.FeatureRpcError) as raised:
        await feature_rpc.skills_toggle({"slug": "ghost", "enabled": False})
    assert raised.value.code == "NOT_FOUND"

    assert await feature_rpc.skills_restore({"slug": "notion"}) == {"ok": True, "slug": "notion"}
    assert "notion" in {row["slug"] for row in feature_rpc.skills_list()["skills"]}
    with pytest.raises(feature_rpc.FeatureRpcError) as raised:
        await feature_rpc.skills_restore({"slug": "notion"})
    assert raised.value.code == "NOT_FOUND"

    actions = [json.loads(line)["action"] for log in (agent.parent / "audit").glob("*.jsonl")
               for line in log.read_text().splitlines()]
    assert actions == ["remove", "disable", "restore"]


async def test_a_disabled_skill_that_is_removed_comes_back_on(agent):
    state.set_enabled(agent, "notion", False)
    await feature_rpc.skills_remove({"slug": "notion", "restart": False})
    assert "notion" not in state.disabled(agent)
