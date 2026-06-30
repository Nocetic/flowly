"""Tests for the plugin system: manifest, adapter, context, manager."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from flowly.agent.hooks import HookRegistry, ToolHookContext
from flowly.agent.tools.registry import ToolRegistry
from flowly.plugins import PluginManager, _reset_for_tests
from flowly.plugins.adapter import FunctionToolAdapter
from flowly.plugins.context import PluginContext, RESERVED_SLASH_COMMANDS
from flowly.plugins.manifest import PluginManifest, find_manifest, parse_manifest


# ── Helpers ─────────────────────────────────────────────────────


def _write_plugin(
    root: Path,
    name: str,
    *,
    manifest_text: str | None = None,
    init_text: str | None = None,
) -> Path:
    """Build a minimal plugin directory under *root* and return its path."""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    if manifest_text is None:
        manifest_text = textwrap.dedent(f"""\
            name: {name}
            version: 0.1.0
            description: A test plugin
            kind: standalone
        """)
    (plugin_dir / "plugin.yaml").write_text(manifest_text)
    if init_text is None:
        init_text = textwrap.dedent("""\
            def register(ctx):
                pass
        """)
    (plugin_dir / "__init__.py").write_text(init_text)
    return plugin_dir


@pytest.fixture(autouse=True)
def _reset_plugin_singleton():
    from flowly.agent.tools.web_providers.registry import (
        _reset_for_tests as _reset_web,
    )

    _reset_for_tests()
    _reset_web()
    yield
    _reset_for_tests()
    _reset_web()


# ── manifest.parse_manifest ────────────────────────────────────


class TestParseManifest:
    def test_minimal_yaml(self, tmp_path):
        plugin_dir = _write_plugin(tmp_path, "foo")
        manifest = parse_manifest(
            plugin_dir / "plugin.yaml", plugin_dir, source="user",
        )
        assert manifest is not None
        assert manifest.name == "foo"
        assert manifest.version == "0.1.0"
        assert manifest.kind == "standalone"
        assert manifest.source == "user"
        assert manifest.path == plugin_dir
        assert manifest.key == "foo"

    def test_unknown_kind_falls_back_to_standalone(self, tmp_path):
        plugin_dir = _write_plugin(
            tmp_path, "weird",
            manifest_text="name: weird\nversion: '1'\nkind: weirdkind\n",
        )
        manifest = parse_manifest(
            plugin_dir / "plugin.yaml", plugin_dir, source="user",
        )
        assert manifest is not None
        assert manifest.kind == "standalone"

    def test_json_manifest(self, tmp_path):
        plugin_dir = tmp_path / "json-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            '{"name": "json-plugin", "version": "2.0", "kind": "standalone"}'
        )
        manifest = parse_manifest(
            plugin_dir / "plugin.json", plugin_dir, source="user",
        )
        assert manifest is not None
        assert manifest.version == "2.0"

    def test_missing_name_falls_back_to_dir_name(self, tmp_path):
        plugin_dir = tmp_path / "implicit"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text("version: '1'\n")
        manifest = parse_manifest(
            plugin_dir / "plugin.yaml", plugin_dir, source="bundled",
        )
        assert manifest is not None
        assert manifest.name == "implicit"

    def test_unsupported_manifest_version_returns_none(self, tmp_path):
        plugin_dir = tmp_path / "future"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(
            "name: future\nmanifest_version: 99\n"
        )
        manifest = parse_manifest(
            plugin_dir / "plugin.yaml", plugin_dir, source="user",
        )
        assert manifest is None

    def test_malformed_yaml_returns_none(self, tmp_path):
        plugin_dir = tmp_path / "broken"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text("name: foo\n  bad:indent\n: : :")
        manifest = parse_manifest(
            plugin_dir / "plugin.yaml", plugin_dir, source="user",
        )
        assert manifest is None


class TestFindManifest:
    def test_prefers_yaml(self, tmp_path):
        d = tmp_path / "p"
        d.mkdir()
        (d / "plugin.yaml").write_text("name: p")
        (d / "plugin.json").write_text("{}")
        assert find_manifest(d).name == "plugin.yaml"

    def test_falls_back_to_yml(self, tmp_path):
        d = tmp_path / "p"
        d.mkdir()
        (d / "plugin.yml").write_text("name: p")
        assert find_manifest(d).name == "plugin.yml"

    def test_falls_back_to_json(self, tmp_path):
        d = tmp_path / "p"
        d.mkdir()
        (d / "plugin.json").write_text("{}")
        assert find_manifest(d).name == "plugin.json"

    def test_returns_none_when_missing(self, tmp_path):
        d = tmp_path / "p"
        d.mkdir()
        assert find_manifest(d) is None


# ── adapter.FunctionToolAdapter ────────────────────────────────


class TestFunctionToolAdapter:
    @pytest.mark.asyncio
    async def test_sync_handler(self):
        def handler(x: int) -> str:
            return f"got {x}"

        adapter = FunctionToolAdapter(
            name="t",
            schema={"description": "test", "parameters": {"type": "object"}},
            handler=handler,
        )
        assert adapter.name == "t"
        assert adapter.description == "test"
        result = await adapter.execute(x=5)
        assert result == "got 5"

    @pytest.mark.asyncio
    async def test_async_handler(self):
        async def handler() -> str:
            return "async"

        adapter = FunctionToolAdapter(
            name="t", schema={"parameters": {"type": "object"}}, handler=handler,
        )
        assert await adapter.execute() == "async"

    @pytest.mark.asyncio
    async def test_check_fn_blocks_when_false(self):
        adapter = FunctionToolAdapter(
            name="t",
            schema={"parameters": {"type": "object"}},
            handler=lambda: "should not run",
            check_fn=lambda: False,
        )
        result = await adapter.execute()
        assert "unavailable" in result

    @pytest.mark.asyncio
    async def test_check_fn_passes_when_true(self):
        adapter = FunctionToolAdapter(
            name="t",
            schema={"parameters": {"type": "object"}},
            handler=lambda: "ran",
            check_fn=lambda: True,
        )
        assert await adapter.execute() == "ran"

    @pytest.mark.asyncio
    async def test_handler_exception_returns_error_string(self):
        def boom():
            raise RuntimeError("fail")

        adapter = FunctionToolAdapter(
            name="t", schema={"parameters": {"type": "object"}}, handler=boom,
        )
        result = await adapter.execute()
        assert result.startswith("Error executing t:")

    def test_extracts_parameters_from_function_wrapper(self):
        adapter = FunctionToolAdapter(
            name="t",
            schema={
                "type": "function",
                "function": {
                    "parameters": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                    },
                },
            },
            handler=lambda **kw: None,
        )
        assert adapter.parameters["properties"]["x"]["type"] == "string"

    def test_extracts_parameters_when_schema_is_parameters(self):
        adapter = FunctionToolAdapter(
            name="t",
            schema={"type": "object", "properties": {"y": {"type": "integer"}}},
            handler=lambda **kw: None,
        )
        assert adapter.parameters["properties"]["y"]["type"] == "integer"

# ── PluginContext ──────────────────────────────────────────────


def _make_manager() -> PluginManager:
    return PluginManager(
        tool_registry=ToolRegistry(),
        hook_registry=HookRegistry(),
    )


class TestPluginContext:
    def test_register_tool_inserts_into_registry(self):
        mgr = _make_manager()
        manifest = PluginManifest(name="p", key="p")
        ctx = PluginContext(manifest, mgr)
        ctx.register_tool(
            name="my_tool",
            schema={"parameters": {"type": "object"}},
            handler=lambda: "ok",
        )
        assert mgr._tool_registry.has("my_tool")
        assert "my_tool" in mgr._plugin_tool_names["p"]

    def test_register_hook_subscribes_to_registry(self):
        mgr = _make_manager()
        ctx = PluginContext(PluginManifest(name="p", key="p"), mgr)
        ctx.register_hook("post_tool_call", lambda hook_ctx: None)
        assert "post_tool_call" in mgr._plugin_hook_names["p"]
        assert len(mgr._hook_registry._hooks["post_tool_call"]) == 1

    def test_register_command_normalises_name(self):
        mgr = _make_manager()
        ctx = PluginContext(PluginManifest(name="p", key="p"), mgr)
        ctx.register_command("/Foo Bar", lambda args: "ok")
        assert "foo-bar" in mgr._slash_commands

    def test_register_command_rejects_reserved(self, caplog):
        mgr = _make_manager()
        ctx = PluginContext(PluginManifest(name="p", key="p"), mgr)
        for reserved in RESERVED_SLASH_COMMANDS:
            ctx.register_command(reserved, lambda args: None)
            assert reserved not in mgr._slash_commands
        assert "reserved" in caplog.text.lower()

    def test_register_skill_qualifies_name(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("# skill")
        mgr = _make_manager()
        ctx = PluginContext(PluginManifest(name="myplug", key="myplug"), mgr)
        ctx.register_skill("greet", skill_md, description="Says hi")
        assert "myplug:greet" in mgr._plugin_skills
        assert mgr._plugin_skills["myplug:greet"]["bare_name"] == "greet"

    def test_register_skill_rejects_colon(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("#")
        mgr = _make_manager()
        ctx = PluginContext(PluginManifest(name="p", key="p"), mgr)
        with pytest.raises(ValueError, match="must not contain"):
            ctx.register_skill("bad:name", skill_md)


# ── PluginManager — discovery & loading ────────────────────────


class TestPluginManagerDiscovery:
    def test_loads_user_plugin_when_enabled(self, tmp_path, monkeypatch):
        # Point FLOWLY_HOME at a temp dir and drop a plugin under
        # plugins/ so the user-source scan finds it.
        flowly_home = tmp_path / "home"
        plugins_dir = flowly_home / "plugins"
        plugins_dir.mkdir(parents=True)
        _write_plugin(
            plugins_dir, "hello",
            init_text=textwrap.dedent("""\
                def register(ctx):
                    ctx.register_tool(
                        name="hello",
                        schema={"parameters": {"type": "object"}},
                        handler=lambda: "world",
                    )
            """),
        )
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"hello"}, disabled=set())

        info = {p["key"]: p for p in mgr.list_plugins()}
        assert info["hello"]["enabled"] is True
        assert mgr._tool_registry.has("hello")

    def test_skips_user_plugin_when_not_enabled(self, tmp_path, monkeypatch):
        flowly_home = tmp_path / "home"
        plugins_dir = flowly_home / "plugins"
        plugins_dir.mkdir(parents=True)
        _write_plugin(plugins_dir, "secret")
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        mgr = _make_manager()
        mgr.discover_and_load(enabled=set(), disabled=set())

        info = {p["key"]: p for p in mgr.list_plugins()}
        assert info["secret"]["enabled"] is False
        assert "not in plugins.enabled" in info["secret"]["error"]

    def test_disabled_overrides_enabled(self, tmp_path, monkeypatch):
        flowly_home = tmp_path / "home"
        plugins_dir = flowly_home / "plugins"
        plugins_dir.mkdir(parents=True)
        _write_plugin(plugins_dir, "thing")
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"thing"}, disabled={"thing"})

        info = {p["key"]: p for p in mgr.list_plugins()}
        assert info["thing"]["enabled"] is False
        assert info["thing"]["error"] == "disabled in config"

    def test_failing_register_records_error_and_keeps_others(self, tmp_path, monkeypatch):
        flowly_home = tmp_path / "home"
        plugins_dir = flowly_home / "plugins"
        plugins_dir.mkdir(parents=True)
        _write_plugin(
            plugins_dir, "bad",
            init_text="def register(ctx):\n    raise RuntimeError('boom')\n",
        )
        _write_plugin(
            plugins_dir, "good",
            init_text=textwrap.dedent("""\
                def register(ctx):
                    ctx.register_tool(
                        name="good_tool",
                        schema={"parameters": {"type": "object"}},
                        handler=lambda: "ok",
                    )
            """),
        )
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"bad", "good"}, disabled=set())

        info = {p["key"]: p for p in mgr.list_plugins()}
        assert info["bad"]["enabled"] is False
        assert "RuntimeError" in info["bad"]["error"]
        assert info["good"]["enabled"] is True
        assert mgr._tool_registry.has("good_tool")

    def test_kind_backend_loads_and_registers_web_provider(self, tmp_path, monkeypatch):
        from flowly.agent.tools.web_providers.registry import get_provider

        flowly_home = tmp_path / "home"
        plugins_dir = flowly_home / "plugins"
        plugins_dir.mkdir(parents=True)
        _write_plugin(
            plugins_dir, "webprov",
            manifest_text=(
                "name: webprov\nversion: '1'\nkind: backend\n"
                "provides_web_providers:\n  - dummy\n"
            ),
            init_text=textwrap.dedent("""\
                from flowly.agent.tools.web_providers.base import WebSearchProvider

                class _Dummy(WebSearchProvider):
                    @property
                    def name(self):
                        return "dummy"

                    def is_available(self):
                        return True

                    def search(self, query, limit=5):
                        return {"success": True, "data": {"web": []}}

                def register(ctx):
                    ctx.register_web_search_provider(_Dummy())
            """),
        )
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"webprov"}, disabled=set())

        info = {p["key"]: p for p in mgr.list_plugins()}
        assert info["webprov"]["enabled"] is True
        assert info["webprov"]["web_providers"] == ["dummy"]
        assert get_provider("dummy") is not None

    def test_kind_exclusive_is_skipped(self, tmp_path, monkeypatch):
        flowly_home = tmp_path / "home"
        plugins_dir = flowly_home / "plugins"
        plugins_dir.mkdir(parents=True)
        _write_plugin(
            plugins_dir, "memprov",
            manifest_text="name: memprov\nversion: '1'\nkind: exclusive\n",
        )
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"memprov"}, disabled=set())

        info = {p["key"]: p for p in mgr.list_plugins()}
        assert info["memprov"]["enabled"] is False
        assert "not supported" in info["memprov"]["error"]


# ── Manager-level slash + skill lookups ────────────────────────


class TestManagerLookups:
    def test_get_slash_handler_strips_leading_slash(self, tmp_path, monkeypatch):
        flowly_home = tmp_path / "home"
        plugins_dir = flowly_home / "plugins"
        plugins_dir.mkdir(parents=True)
        _write_plugin(
            plugins_dir, "slasher",
            init_text=textwrap.dedent("""\
                def register(ctx):
                    ctx.register_command(
                        "ping",
                        handler=lambda args: "pong",
                        description="Ping",
                    )
            """),
        )
        monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))

        mgr = _make_manager()
        mgr.discover_and_load(enabled={"slasher"}, disabled=set())

        assert mgr.get_slash_handler("ping") is not None
        assert mgr.get_slash_handler("/ping") is not None
        assert mgr.get_slash_handler("PING") is not None
        assert mgr.get_slash_handler("nope") is None
