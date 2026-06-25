"""Tests for the codex-parity migration features.

Covers MCP-server translation, permission profile, plugin block rendering,
and unmanaged-plugin-table stripping added to ``flowly.codex.tool_migration``.
"""

from __future__ import annotations

import tomllib

from flowly.codex import tool_migration as tm
from flowly.config.schema import Config, MCPServerConfig

# ---------------------------------------------------------------------------
# MCP server translation
# ---------------------------------------------------------------------------


class TestTranslateMcpServer:
    def test_stdio_server(self):
        cfg = MCPServerConfig(command="npx", args=["-y", "pkg"], env={"K": "v"}, timeout=90.0)
        out, skipped = tm._translate_mcp_server("github", cfg)
        assert out["command"] == "npx"
        assert out["args"] == ["-y", "pkg"]
        assert out["env"] == {"K": "v"}
        assert out["tool_timeout_sec"] == 90.0

    def test_http_server(self):
        cfg = MCPServerConfig(url="https://x.com", headers={"Authorization": "Bearer z"})
        out, _ = tm._translate_mcp_server("remote", cfg)
        assert out["url"] == "https://x.com"
        assert out["http_headers"] == {"Authorization": "Bearer z"}

    def test_neither_command_nor_url_untranslatable(self):
        cfg = MCPServerConfig()
        out, skipped = tm._translate_mcp_server("empty", cfg)
        assert out is None
        assert skipped

    def test_disabled_flag_emitted(self):
        cfg = MCPServerConfig(command="x", enabled=False)
        out, _ = tm._translate_mcp_server("s", cfg)
        assert out["enabled"] is False


# ---------------------------------------------------------------------------
# Permission mapping
# ---------------------------------------------------------------------------


class TestSandboxMapping:
    def test_known_levels(self):
        assert tm._sandbox_to_permission("read-only") == ":read-only"
        assert tm._sandbox_to_permission("workspace-write") == ":workspace"
        assert tm._sandbox_to_permission("full-access") == ":danger-full-access"

    def test_unknown_defaults_workspace(self):
        assert tm._sandbox_to_permission("") == ":workspace"
        assert tm._sandbox_to_permission(None) == ":workspace"


# ---------------------------------------------------------------------------
# Managed-block rendering — valid TOML with all the pieces
# ---------------------------------------------------------------------------


class TestRenderBlock:
    def _render(self):
        servers = {
            "github": {"command": "npx", "args": ["-y", "p"], "env": {"T": "x"}},
            "remote": {"url": "https://x.com", "http_headers": {"A": "b"}},
        }
        plugins = [{"name": "linear", "marketplace": "openai-curated", "enabled": True}]
        return tm.render_managed_block(
            python_bin="/py", env={"PYTHONPATH": "/x"},
            servers=servers, plugins=plugins, default_permissions=":workspace",
        )

    def test_block_is_valid_toml(self):
        block = self._render()
        parsed = tomllib.loads(
            block.replace(tm._MARKER, "# m").replace(tm._END_MARKER, "# e")
        )
        assert parsed["default_permissions"] == ":workspace"
        assert set(parsed["mcp_servers"]) == {"flowly-tools", "github", "remote"}
        assert "linear@openai-curated" in parsed["plugins"]
        assert parsed["plugins"]["linear@openai-curated"]["enabled"] is True

    def test_permission_precedes_first_table(self):
        block = self._render()
        assert block.index("default_permissions") < block.index("[mcp_servers")

    def test_callback_always_present(self):
        block = tm.render_managed_block(python_bin="/py", env={})
        assert "[mcp_servers.flowly-tools]" in block


# ---------------------------------------------------------------------------
# Unmanaged plugin table stripping
# ---------------------------------------------------------------------------


class TestStripUnmanagedPlugins:
    def test_strips_plugin_tables_keeps_others(self):
        text = (
            'model = "gpt-5.5"\n\n'
            '[plugins."github@openai-curated"]\nenabled = true\n\n'
            '[projects."/x"]\ntrust_level = "trusted"\n'
        )
        out = tm._strip_unmanaged_plugin_tables(text)
        assert "[plugins." not in out
        assert 'model = "gpt-5.5"' in out
        assert '[projects."/x"]' in out


# ---------------------------------------------------------------------------
# End-to-end migrate with a config carrying MCP servers
# ---------------------------------------------------------------------------


class TestMigrateWithServers:
    def test_migrate_writes_user_mcp_servers(self, tmp_path):
        cfg = Config()
        cfg.mcp_servers = {
            "github": MCPServerConfig(command="npx", args=["-y", "gh"]),
        }
        target = tm.migrate_flowly_tools_to_codex(
            codex_home=str(tmp_path), python_bin="/py",
            config=cfg, default_permissions=":workspace", discover_plugins=False,
        )
        text = target.read_text()
        parsed = tomllib.loads(
            text.replace(tm._MARKER, "# m").replace(tm._END_MARKER, "# e")
        )
        assert "github" in parsed["mcp_servers"]
        assert "flowly-tools" in parsed["mcp_servers"]
        assert parsed["default_permissions"] == ":workspace"
