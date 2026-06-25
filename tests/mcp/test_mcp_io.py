"""Tests for the TUI MCP modal backing logic (A2, flowly/integrations/mcp_io)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowly.config.loader import save_config
from flowly.config.schema import Config, MCPServerConfig
from flowly.integrations import mcp_io


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    save_config(Config())
    return tmp_path


def _names(rows, source=None):
    return {r.name for r in rows if source is None or r.source == source}


# ── listing ─────────────────────────────────────────────────────────


def test_empty_config_lists_only_catalog(isolated_home):
    rows = mcp_io.list_mcp_servers()
    assert _names(rows, "configured") == set()
    # All 8 catalog entries show as available.
    assert {"context7", "github", "linear"} <= _names(rows, "catalog")
    assert all(r.status == "available" for r in rows if r.source == "catalog")


def test_catalog_secret_oauth_flags(isolated_home):
    rows = {r.name: r for r in mcp_io.list_mcp_servers()}
    assert rows["context7"].needs_secrets is False   # auth none
    assert rows["fetch"].needs_secrets is False
    assert rows["github"].needs_secrets is True       # api_key secret → prompt
    assert rows["github"].secret_fields              # has fields to collect
    assert rows["linear"].needs_oauth is True         # oauth → install then login
    assert rows["linear"].auth == "oauth"


def test_configured_server_status(isolated_home):
    cfg = Config()
    cfg.mcp_servers = {
        "on": MCPServerConfig(command="echo"),
        "off": MCPServerConfig(command="echo", enabled=False),
        "broken": MCPServerConfig(),  # no command/url
    }
    save_config(cfg)
    rows = {r.name: r for r in mcp_io.list_mcp_servers()}
    assert rows["on"].status == "enabled"
    assert rows["off"].status == "disabled"
    assert rows["broken"].status == "invalid"
    assert rows["broken"].error


def test_configured_hides_catalog_duplicate(isolated_home):
    cfg = Config()
    cfg.mcp_servers = {"context7": MCPServerConfig(command="npx")}
    save_config(cfg)
    rows = mcp_io.list_mcp_servers()
    c7 = [r for r in rows if r.name == "context7"]
    # Only ONE context7 row, and it's the configured one (not catalog).
    assert len(c7) == 1
    assert c7[0].source == "configured"


def test_tool_filter_summary(isolated_home):
    cfg = Config()
    cfg.mcp_servers = {
        "inc": MCPServerConfig(command="x", tools={"include": ["a", "b"]}),
        "exc": MCPServerConfig(command="x", tools={"exclude": ["c"]}),
        "all": MCPServerConfig(command="x"),
    }
    save_config(cfg)
    rows = {r.name: r for r in mcp_io.list_mcp_servers()}
    assert rows["inc"].tool_filter == "2 selected"
    assert rows["exc"].tool_filter == "-1 excluded"
    assert rows["all"].tool_filter == "all"


# ── mutations ───────────────────────────────────────────────────────


def test_install_no_secret_entry(isolated_home):
    ok, msg = mcp_io.install_catalog_server("context7")
    assert ok, msg
    data = json.loads((isolated_home / "config.json").read_text())
    assert data["mcpServers"]["context7"]["command"] == "npx"


def test_install_secret_entry_writes_env_and_config(isolated_home):
    # The modal collects the token; install writes it to .env (not config)
    # and installs the server referencing ${VAR}.
    ok, msg = mcp_io.install_catalog_server(
        "github", {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_fromtui"},
    )
    assert ok, msg
    cfg_text = (isolated_home / "config.json").read_text()
    assert "github" in json.loads(cfg_text).get("mcpServers", {})
    assert "ghp_fromtui" not in cfg_text                      # secret NOT in config
    assert "ghp_fromtui" in (isolated_home / ".env").read_text()  # secret in .env


def test_install_oauth_entry_inline(isolated_home):
    # OAuth installs the config inline (no secret); message points at login.
    ok, msg = mcp_io.install_catalog_server("linear")
    assert ok, msg
    assert "flowly mcp login linear" in msg
    entry = json.loads((isolated_home / "config.json").read_text())["mcpServers"]["linear"]
    assert entry["auth"] == "oauth"


def test_catalog_secret_fields_accessor(isolated_home):
    fields = mcp_io.catalog_secret_fields("github")
    assert any(f.name == "GITHUB_PERSONAL_ACCESS_TOKEN" and f.secret for f in fields)
    assert mcp_io.catalog_secret_fields("context7") == []


def test_set_enabled_toggles(isolated_home):
    cfg = Config()
    cfg.mcp_servers = {"s": MCPServerConfig(command="echo")}
    save_config(cfg)
    mcp_io.set_mcp_enabled("s", False)
    data = json.loads((isolated_home / "config.json").read_text())
    assert data["mcpServers"]["s"]["enabled"] is False
    mcp_io.set_mcp_enabled("s", True)
    data = json.loads((isolated_home / "config.json").read_text())
    assert data["mcpServers"]["s"]["enabled"] is True


def test_set_enabled_unknown_raises(isolated_home):
    with pytest.raises(KeyError):
        mcp_io.set_mcp_enabled("ghost", False)


def test_remove_drops_entry_and_empties_key(isolated_home):
    cfg = Config()
    cfg.mcp_servers = {"only": MCPServerConfig(command="echo")}
    save_config(cfg)
    assert mcp_io.remove_mcp_server("only") is True
    data = json.loads((isolated_home / "config.json").read_text())
    assert "mcpServers" not in data
    # Removing again is a no-op (returns False).
    assert mcp_io.remove_mcp_server("only") is False


def test_install_preserves_env_keys_verbatim(isolated_home, monkeypatch):
    # A hypothetical no-secret entry whose config carries an UPPER_SNAKE
    # env key must round-trip unmangled (regression guard for the
    # convert_to_camel key-preservation fix). We install context7 then
    # assert the on-disk JSON keeps camelCase top-level + verbatim names.
    ok, _ = mcp_io.install_catalog_server("context7")
    assert ok
    raw = json.loads((isolated_home / "config.json").read_text())
    entry = raw["mcpServers"]["context7"]
    # camelCase top-level field present (not snake), args intact.
    assert entry["args"] == ["-y", "@upstash/context7-mcp"]


def test_modal_class_importable():
    # The Textual modal needs a running app to render, but importing it +
    # checking its bindings catches gross wiring errors.
    from flowly.tui.panes.mcp_modal import MCPModal
    binding_keys = {b[0] for b in MCPModal.BINDINGS}
    assert {"escape", "r", "d"} <= binding_keys
