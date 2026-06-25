"""End-to-end tests for the bundled disk-cleanup plugin.

Loads the plugin through the same discovery path the agent uses at
runtime and verifies that:
  - hooks fire on simulated tool calls
  - test files get auto-tracked
  - session_end triggers a quick cleanup that actually deletes them
  - the slash command surfaces status / dry-run / track / forget
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flowly.agent.hooks import (
    HookRegistry,
    SessionHookContext,
    ToolHookContext,
)
from flowly.agent.tools.registry import ToolRegistry
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


@pytest.fixture
def loaded_plugin(tmp_path, monkeypatch):
    """Boot the disk-cleanup plugin against an isolated FLOWLY_HOME."""
    flowly_home = tmp_path / "home"
    flowly_home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

    tools = ToolRegistry()
    hooks = HookRegistry()
    mgr = get_plugin_manager(tool_registry=tools, hook_registry=hooks)
    # bundled disk-cleanup is default-on; explicit empty enabled set ok
    mgr.discover_and_load(enabled=set(), disabled=set())

    info = {p["key"]: p for p in mgr.list_plugins()}
    assert "disk-cleanup" in info, info
    assert info["disk-cleanup"]["enabled"] is True, info["disk-cleanup"]
    return flowly_home, tools, hooks, mgr


# ── Discovery ─────────────────────────────────────────────────


class TestBundledDiscovery:
    def test_plugin_appears_in_list(self, loaded_plugin):
        _, _, _, mgr = loaded_plugin
        info = {p["key"]: p for p in mgr.list_plugins()}
        assert info["disk-cleanup"]["source"] == "bundled"
        assert "post_tool_call" in info["disk-cleanup"]["hooks"]
        assert "on_session_end" in info["disk-cleanup"]["hooks"]

    def test_slash_handler_registered(self, loaded_plugin):
        _, _, _, mgr = loaded_plugin
        assert mgr.get_slash_handler("disk-cleanup") is not None


# ── Hook integration: write_file → tracking ───────────────────


class TestPostToolCallHook:
    @pytest.mark.asyncio
    async def test_test_file_in_workspace_gets_tracked(
        self, loaded_plugin,
    ):
        flowly_home, _, hooks, _ = loaded_plugin
        # Create a test_*.py file under FLOWLY_HOME (eligible by category).
        target = flowly_home / "test_unit.py"
        target.write_text("# placeholder")

        ctx = ToolHookContext(
            tool_name="write_file",
            params={"path": str(target)},
            result="ok",
            session_id="sess-1",
        )
        await hooks.fire("post_tool_call", ctx)

        import sys
        dg = sys.modules["flowly_plugins.disk_cleanup"].dg
        tracked = dg.load_tracked()
        paths = [t["path"] for t in tracked]
        assert str(target.resolve()) in paths

    @pytest.mark.asyncio
    async def test_safe_dir_is_not_tracked(self, loaded_plugin, tmp_path):
        # Plain non-test name in the user's home — should be ignored.
        _, _, hooks, _ = loaded_plugin
        target = tmp_path / "scratch.txt"
        target.write_text("data")

        ctx = ToolHookContext(
            tool_name="write_file",
            params={"path": str(target)},
            result="ok",
            session_id="sess-1",
        )
        await hooks.fire("post_tool_call", ctx)

        import sys
        dg = sys.modules["flowly_plugins.disk_cleanup"].dg
        tracked = dg.load_tracked()
        paths = [t["path"] for t in tracked]
        assert str(target.resolve()) not in paths


# ── Hook integration: session_end → quick cleanup ─────────────


class TestSessionEndHook:
    @pytest.mark.asyncio
    async def test_session_end_deletes_tracked_test_file(
        self, loaded_plugin,
    ):
        flowly_home, _, hooks, _ = loaded_plugin
        target = flowly_home / "test_will_die.py"
        target.write_text("garbage")

        # Fire post_tool_call so the file gets tracked.
        await hooks.fire("post_tool_call", ToolHookContext(
            tool_name="write_file",
            params={"path": str(target)},
            session_id="sess-X",
        ))
        assert target.exists()

        # Fire on_session_end → quick cleanup runs.
        await hooks.fire("on_session_end", SessionHookContext(
            session_id="sess-X", completed=True,
        ))

        assert not target.exists(), "test file should be deleted at session end"

    @pytest.mark.asyncio
    async def test_session_end_no_op_without_tracked_tests(
        self, loaded_plugin,
    ):
        # No tracking happened — session_end should not blow up.
        _, _, hooks, _ = loaded_plugin
        await hooks.fire("on_session_end", SessionHookContext(
            session_id="empty-sess",
        ))


# ── Slash command ─────────────────────────────────────────────


class TestSlashCommand:
    def test_status_includes_breakdown_header(self, loaded_plugin):
        _, _, _, mgr = loaded_plugin
        handler = mgr.get_slash_handler("disk-cleanup")
        out = handler("status")
        assert "Category" in out

    def test_help_returned_for_no_args(self, loaded_plugin):
        _, _, _, mgr = loaded_plugin
        handler = mgr.get_slash_handler("disk-cleanup")
        out = handler("")
        assert "/disk-cleanup" in out
        assert "status" in out
        assert "dry-run" in out

    def test_track_then_forget_round_trip(
        self, loaded_plugin,
    ):
        flowly_home, _, _, mgr = loaded_plugin
        target = flowly_home / "manual.txt"
        target.write_text("payload")

        handler = mgr.get_slash_handler("disk-cleanup")
        out = handler(f"track {target} temp")
        assert "Tracked" in out

        out2 = handler(f"forget {target}")
        assert "Removed" in out2

    def test_track_rejects_unknown_category(self, loaded_plugin):
        _, _, _, mgr = loaded_plugin
        handler = mgr.get_slash_handler("disk-cleanup")
        out = handler("track /tmp/whatever bogus-cat")
        assert "Unknown category" in out
