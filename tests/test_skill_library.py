"""Flowly's skill library (2026-11-13): 145 built-in skills made the agent's
list too long to choose from. A core set ships installed; the rest ship in
``flowly/skills_library`` and install in one step, with no network."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from flowly.hub.client import HubClient
from flowly.hub.manager import SkillManager
from flowly.skills import sync
from flowly.skills.library import LIBRARY_DIR, library_skills


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    bundled = tmp_path / "bundled"
    library = tmp_path / "library"
    user = tmp_path / "home" / "skills"
    for root, names in ((bundled, ["weather"]), (library, ["dcf-model", "pixel-art"])):
        for name in names:
            (root / name).mkdir(parents=True)
            (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\nbody\n")
    monkeypatch.setattr(sync, "_get_bundled_dir", lambda: bundled)
    monkeypatch.setattr(sync, "_get_user_dir", lambda: user)
    monkeypatch.setattr("flowly.skills.library.LIBRARY_DIR", library)
    monkeypatch.setattr(sync, "_SYNCED_THIS_PROCESS", False)
    return tmp_path


def _was_bundled(home: Path, *names: str) -> None:
    """An agent installed when these skills were still bundled (synced, manifest written)."""
    user = home / "home" / "skills"
    for name in names:
        shutil.copytree(home / "library" / name, user / name)
    entries = {name: sync._dir_hash(user / name) for name in names}
    entries["weather"] = sync._dir_hash(home / "bundled" / "weather")
    shutil.copytree(home / "bundled" / "weather", user / "weather")
    sync._write_manifest(entries)


def test_a_skill_that_moved_to_the_library_leaves_an_agent_that_never_changed_it(home: Path) -> None:
    _was_bundled(home, "dcf-model", "pixel-art")
    user = home / "home" / "skills"
    # The owner made pixel-art their own.
    (user / "pixel-art" / "SKILL.md").write_text("---\nname: pixel-art\n---\nmine\n")

    result = sync.sync_skills()

    assert result["retired"] == ["dcf-model"]
    assert not (user / "dcf-model").exists()
    assert (user / "pixel-art" / "SKILL.md").read_text().endswith("mine\n")
    assert (user / "weather").exists()
    assert set(sync._read_manifest()) == {"weather"}


def test_a_moved_skill_is_retired_even_when_no_bundled_file_is_newer(home: Path) -> None:
    """A git checkout keeps the files' times: the manifest alone shows the move."""
    _was_bundled(home, "dcf-model")
    assert sync._manifest_is_fresh() is False
    sync.sync_skills()
    assert sync._manifest_is_fresh() is True


def test_a_library_skill_installs_from_the_package_and_stays_installed(home: Path) -> None:
    user = home / "home" / "skills"
    sync.sync_skills()
    assert HubClient().parse_skill_source("dcf-model")[0] == "library"

    with SkillManager(managed_dir=user) as manager:
        skill = manager.install("dcf-model")

    assert skill is not None and (user / "dcf-model" / "SKILL.md").exists()
    # Installed by its owner, not by the sync: the next sync keeps it.
    sync.sync_skills()
    assert (user / "dcf-model" / "SKILL.md").exists()


def test_the_agent_lists_and_installs_library_skills(home: Path, monkeypatch) -> None:
    from flowly.agent.tools import skill_manage

    user = home / "home" / "skills"
    monkeypatch.setattr(skill_manage, "_skills_dir", lambda: user)
    tool = skill_manage.SkillManageTool()

    listed = asyncio.run(tool.execute("library", name="dcf"))
    assert "dcf-model" in listed and "pixel-art" not in listed
    assert "Installed 'dcf-model'" in asyncio.run(tool.execute("install", name="dcf-model"))
    assert (user / "dcf-model" / "SKILL.md").exists()
    assert "not in Flowly's skill library" in asyncio.run(tool.execute("install", name="no-such-skill"))


def test_the_shipped_split_keeps_a_short_core_and_no_skill_in_both() -> None:
    core = {d.name for d in Path(sync.__file__).parent.iterdir() if (d / "SKILL.md").exists()}
    library = set(library_skills())
    assert LIBRARY_DIR.is_dir() and library
    assert not core & library
    assert len(core) <= 45
    # The skills Core's own code reads are in the core set.
    assert {"compact", "plan", "flowlets", "flowly-browser", "mcp-usage", "memory-import", "obsidian"} <= core
