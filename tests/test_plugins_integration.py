"""Integration tests for plugin wire-up: config schema + discovery flow."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from flowly.agent.hooks import HookRegistry
from flowly.agent.tools.registry import ToolRegistry
from flowly.config.schema import Config, PluginsConfig
from flowly.plugins import _reset_for_tests, get_plugin_manager


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_for_tests()
    yield
    _reset_for_tests()


# ── Config schema ──────────────────────────────────────────────


class TestPluginsConfig:
    def test_defaults_are_empty(self):
        cfg = PluginsConfig()
        assert cfg.enabled == []
        assert cfg.disabled == []

    def test_attached_to_root_config(self):
        cfg = Config()
        assert isinstance(cfg.plugins, PluginsConfig)
        assert cfg.plugins.enabled == []

    def test_round_trip_via_json(self):
        cfg = Config(plugins=PluginsConfig(
            enabled=["disk-cleanup"], disabled=["something"],
        ))
        # Pydantic v2 — model_dump_json then parse back
        raw = cfg.model_dump()
        assert raw["plugins"]["enabled"] == ["disk-cleanup"]
        assert raw["plugins"]["disabled"] == ["something"]

        restored = Config.model_validate(raw)
        assert restored.plugins.enabled == ["disk-cleanup"]


# ── End-to-end: PluginManager + ToolRegistry integration ──────


class TestPluginManagerWireup:
    def test_singleton_returns_same_instance(self):
        tools = ToolRegistry()
        hooks = HookRegistry()
        mgr1 = get_plugin_manager(tool_registry=tools, hook_registry=hooks)
        mgr2 = get_plugin_manager()
        assert mgr1 is mgr2

    def test_singleton_first_call_requires_registries(self):
        with pytest.raises(RuntimeError, match="not yet initialised"):
            get_plugin_manager()

    def test_user_plugin_tool_appears_in_tool_registry(
        self, tmp_path, monkeypatch,
    ):
        flowly_home = tmp_path / "home"
        plugins_dir = flowly_home / "plugins"
        plugins_dir.mkdir(parents=True)
        plugin = plugins_dir / "echo"
        plugin.mkdir()
        (plugin / "plugin.yaml").write_text(
            "name: echo\nversion: '1'\nkind: standalone\n"
        )
        (plugin / "__init__.py").write_text(textwrap.dedent("""\
            def register(ctx):
                async def handler(text: str) -> str:
                    return f"echo: {text}"
                ctx.register_tool(
                    name="echo",
                    schema={
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                    handler=handler,
                )
        """))
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        tools = ToolRegistry()
        hooks = HookRegistry()
        mgr = get_plugin_manager(tool_registry=tools, hook_registry=hooks)
        mgr.discover_and_load(enabled={"echo"}, disabled=set())

        # Tool should be live in the registry, dispatching to the plugin.
        assert tools.has("echo")

    @pytest.mark.asyncio
    async def test_dispatched_plugin_tool_runs_handler(
        self, tmp_path, monkeypatch,
    ):
        flowly_home = tmp_path / "home"
        plugins_dir = flowly_home / "plugins"
        plugins_dir.mkdir(parents=True)
        plugin = plugins_dir / "shouter"
        plugin.mkdir()
        (plugin / "plugin.yaml").write_text("name: shouter\n")
        (plugin / "__init__.py").write_text(textwrap.dedent("""\
            def register(ctx):
                def shout(text: str) -> str:
                    return text.upper()
                ctx.register_tool(
                    name="shout",
                    schema={
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                    handler=shout,
                )
        """))
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        tools = ToolRegistry()
        hooks = HookRegistry()
        mgr = get_plugin_manager(tool_registry=tools, hook_registry=hooks)
        mgr.discover_and_load(enabled={"shouter"}, disabled=set())

        result = await tools.execute("shout", {"text": "hello"})
        assert result == "HELLO"

    @pytest.mark.asyncio
    async def test_plugin_pre_tool_hook_can_block(
        self, tmp_path, monkeypatch,
    ):
        flowly_home = tmp_path / "home"
        plugins_dir = flowly_home / "plugins"
        plugins_dir.mkdir(parents=True)
        plugin = plugins_dir / "guard"
        plugin.mkdir()
        (plugin / "plugin.yaml").write_text("name: guard\n")
        (plugin / "__init__.py").write_text(textwrap.dedent("""\
            from flowly.agent.hooks import BlockAction
            def register(ctx):
                def block(hook_ctx):
                    if hook_ctx.tool_name == "shout":
                        return BlockAction("policy")
                ctx.register_hook("pre_tool_call", block)
                def shout(text):
                    return text.upper()
                ctx.register_tool(
                    name="shout",
                    schema={
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                    handler=shout,
                )
        """))
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        tools = ToolRegistry(hooks=HookRegistry())
        # Use the same hooks instance the manager will populate
        hooks = tools._hooks
        mgr = get_plugin_manager(tool_registry=tools, hook_registry=hooks)
        mgr.discover_and_load(enabled={"guard"}, disabled=set())

        result = await tools.execute("shout", {"text": "hi"})
        assert result == "[blocked: policy]"
