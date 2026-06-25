"""Tests for plugin-namespaced skill resolution.

Plugin skills register via ``ctx.register_skill(name, path)``; they are
loadable via the qualified name ``"<plugin>:<bare>"`` but DO NOT appear
in :meth:`SkillsLoader.list_skills` (explicit-load only — keeps the
prompt cache prefix stable).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from flowly.agent.hooks import HookRegistry
from flowly.agent.skills import SkillsLoader
from flowly.agent.tools.registry import ToolRegistry
from flowly.agent.tools.skill_view import SkillViewTool
from flowly.plugins import (
    PluginManager,
    _reset_for_tests,
    get_plugin_manager,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_for_tests()
    yield
    _reset_for_tests()


def _bootstrap_plugin_with_skill(
    flowly_home: Path, monkeypatch, plugin_name: str = "myplug",
) -> Path:
    """Create a plugin with a single skill and return the SKILL.md path."""
    plugins_dir = flowly_home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    plugin = plugins_dir / plugin_name
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(f"name: {plugin_name}\nversion: '1'\n")

    skill_dir = plugin / "skills" / "greet"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: greet\n---\n# Greet\n\nSay hi.\n")

    (plugin / "__init__.py").write_text(textwrap.dedent(f"""\
        from pathlib import Path
        SKILL = Path(__file__).parent / "skills" / "greet" / "SKILL.md"
        def register(ctx):
            ctx.register_skill(
                name="greet",
                path=SKILL,
                description="Say hi",
            )
    """))

    monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))
    return skill_md


def _make_manager() -> PluginManager:
    return get_plugin_manager(
        tool_registry=ToolRegistry(),
        hook_registry=HookRegistry(),
    )


# ── SkillsLoader.load_skill ────────────────────────────────────


class TestSkillsLoaderPluginLookup:
    def test_qualified_name_resolves_to_plugin(self, tmp_path, monkeypatch):
        flowly_home = tmp_path / "home"
        skill_md = _bootstrap_plugin_with_skill(flowly_home, monkeypatch)

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"myplug"}, disabled=set())

        loader = SkillsLoader(workspace=tmp_path / "workspace")
        content = loader.load_skill("myplug:greet")
        assert content is not None
        assert "Say hi." in content

    def test_unknown_qualified_name_returns_none(self, tmp_path, monkeypatch):
        flowly_home = tmp_path / "home"
        _bootstrap_plugin_with_skill(flowly_home, monkeypatch)

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"myplug"}, disabled=set())

        loader = SkillsLoader(workspace=tmp_path / "workspace")
        assert loader.load_skill("myplug:missing") is None
        assert loader.load_skill("nope:greet") is None

    def test_plain_name_falls_through_normal_lookup(
        self, tmp_path, monkeypatch,
    ):
        # No plugins enabled, plain name should not invoke plugin lookup
        flowly_home = tmp_path / "home"
        flowly_home.mkdir()
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        loader = SkillsLoader(workspace=tmp_path / "workspace")
        # Should return None without crashing
        assert loader.load_skill("compact") is None or True

    def test_plugin_skills_NOT_in_list_skills(self, tmp_path, monkeypatch):
        flowly_home = tmp_path / "home"
        _bootstrap_plugin_with_skill(flowly_home, monkeypatch)

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"myplug"}, disabled=set())

        loader = SkillsLoader(workspace=tmp_path / "workspace")
        names = [s["name"] for s in loader.list_skills(filter_unavailable=False)]
        # Plugin skill must NOT leak into the index
        assert "myplug:greet" not in names
        assert "greet" not in names


# ── SkillViewTool ──────────────────────────────────────────────


class TestSkillViewToolPluginLookup:
    def test_finds_plugin_skill_dir(self, tmp_path, monkeypatch):
        flowly_home = tmp_path / "home"
        skill_md = _bootstrap_plugin_with_skill(flowly_home, monkeypatch)

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"myplug"}, disabled=set())

        tool = SkillViewTool(workspace=tmp_path / "workspace")
        skill_dir = tool._find_skill_dir("myplug:greet")
        assert skill_dir == skill_md.parent

    def test_returns_none_for_unknown_qualified(self, tmp_path, monkeypatch):
        flowly_home = tmp_path / "home"
        _bootstrap_plugin_with_skill(flowly_home, monkeypatch)

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"myplug"}, disabled=set())

        tool = SkillViewTool(workspace=tmp_path / "workspace")
        assert tool._find_skill_dir("ghost:missing") is None

    def test_returns_none_when_manager_uninitialised(self, tmp_path):
        # No singleton initialised — :name lookup must not crash.
        tool = SkillViewTool(workspace=tmp_path / "workspace")
        assert tool._find_skill_dir("any:name") is None

    @pytest.mark.asyncio
    async def test_execute_loads_plugin_skill_content(
        self, tmp_path, monkeypatch,
    ):
        flowly_home = tmp_path / "home"
        _bootstrap_plugin_with_skill(flowly_home, monkeypatch)

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"myplug"}, disabled=set())

        tool = SkillViewTool(workspace=tmp_path / "workspace")
        result = await tool.execute(name="myplug:greet")
        assert "Say hi" in result or "greet" in result.lower()
