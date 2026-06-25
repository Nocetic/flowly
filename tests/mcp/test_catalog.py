"""Tests for the MCP catalog + install (Faz 3b, M2/M3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowly.cli.mcp_cmd import mcp_app
from flowly.config.loader import save_config
from flowly.config.schema import Config
from flowly.mcp.catalog import load_catalog, build_server_config, get_entry


# ── manifest parsing ────────────────────────────────────────────────


def test_all_shipped_manifests_parse():
    cat = load_catalog()
    # The curated set we ship — all must parse cleanly.
    assert {"context7", "fetch", "time", "filesystem", "github",
            "linear", "playwright", "notion"} <= set(cat)


def test_entry_fields():
    e = get_entry("context7")
    assert e is not None
    assert e.auth_type == "none"
    assert e.transport_type == "stdio"
    assert e.transport["command"] == "npx"


def test_build_config_stdio_no_auth():
    cfg = build_server_config(get_entry("context7"))
    assert cfg["command"] == "npx"
    assert "env" not in cfg  # no auth → no env block


def test_build_config_api_key_adds_env_reference():
    cfg = build_server_config(get_entry("github"))
    # Secret referenced via ${VAR}, never inlined.
    assert cfg["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${GITHUB_PERSONAL_ACCESS_TOKEN}"


def test_build_config_interpolated_arg_not_duplicated_in_env():
    # filesystem puts ${MCP_FILESYSTEM_ROOT} in args → must NOT also appear in env.
    cfg = build_server_config(get_entry("filesystem"))
    assert any("${MCP_FILESYSTEM_ROOT}" in str(a) for a in cfg["args"])
    assert "env" not in cfg or "MCP_FILESYSTEM_ROOT" not in cfg.get("env", {})


def test_build_config_http_oauth():
    cfg = build_server_config(get_entry("linear"))
    assert cfg["url"] == "https://mcp.linear.app/mcp"
    assert cfg["auth"] == "oauth"


# ── install ─────────────────────────────────────────────────────────


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    save_config(Config())
    return tmp_path


def _config_json(home: Path) -> dict:
    return json.loads((home / "config.json").read_text())


def test_install_no_auth_no_probe(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, ["install", "context7", "--no-probe"])
    assert result.exit_code == 0, result.stdout
    entry = _config_json(isolated_home)["mcpServers"]["context7"]
    assert entry["command"] == "npx"
    assert entry["enabled"] is True


def test_install_api_key_writes_env_and_config(isolated_home: Path):
    runner = CliRunner()
    # Provide the token at the prompt.
    result = runner.invoke(
        mcp_app, ["install", "github", "--no-probe"], input="ghp_testtoken123\n",
    )
    assert result.exit_code == 0, result.stdout
    # Config references the env var, not the raw token.
    entry = _config_json(isolated_home)["mcpServers"]["github"]
    assert entry["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${GITHUB_PERSONAL_ACCESS_TOKEN}"
    # Secret landed in .env, NOT config.json.
    env_text = (isolated_home / ".env").read_text()
    assert "ghp_testtoken123" in env_text
    assert "ghp_testtoken123" not in (isolated_home / "config.json").read_text()


def test_install_oauth_entry_skips_probe(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, ["install", "linear"])
    assert result.exit_code == 0, result.stdout
    entry = _config_json(isolated_home)["mcpServers"]["linear"]
    assert entry["auth"] == "oauth"
    assert "login linear" in result.stdout


def test_install_unknown_entry_fails(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, ["install", "nope", "--no-probe"])
    assert result.exit_code != 0


def test_catalog_command_lists(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, ["catalog"])
    assert result.exit_code == 0
    assert "context7" in result.stdout
    assert "linear" in result.stdout


def test_env_save_value_roundtrip(isolated_home: Path, monkeypatch):
    from flowly.mcp.env_loader import save_env_value
    import os
    monkeypatch.delenv("MY_TEST_KEY", raising=False)
    save_env_value("MY_TEST_KEY", "secret-val")
    assert os.environ["MY_TEST_KEY"] == "secret-val"
    assert "MY_TEST_KEY=secret-val" in (isolated_home / ".env").read_text()
    import stat
    mode = stat.S_IMODE((isolated_home / ".env").stat().st_mode)
    assert mode == 0o600
    # Update in place (no duplicate line).
    save_env_value("MY_TEST_KEY", "new-val")
    assert (isolated_home / ".env").read_text().count("MY_TEST_KEY=") == 1
