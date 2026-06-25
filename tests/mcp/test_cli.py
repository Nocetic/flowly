"""Tests for the ``flowly mcp`` typer command group.

Covered:

* ``flowly mcp add`` with ``--no-probe`` writes a complete entry.
* ``flowly mcp list`` prints the table and reflects status changes.
* ``flowly mcp enable`` / ``disable`` toggle the ``enabled`` field.
* ``flowly mcp remove`` actually deletes the entry from
  ``config.json`` — important because the deep-merge in
  :func:`flowly.config.loader.save_config` cannot express deletions, so
  the CLI uses a direct-write helper instead.
* Add → list → remove leaves an empty ``mcpServers`` key removed (so
  the file stays clean for users who never used MCP).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowly.cli.mcp_cmd import mcp_app
from flowly.config.loader import save_config
from flowly.config.schema import Config


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    save_config(Config())
    return tmp_path


def _config_json(home: Path) -> dict:
    return json.loads((home / "config.json").read_text())


def test_add_no_probe_writes_entry(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, [
        "add", "myserver",
        "--command", "echo",
        "--arg", "hello",
        "--no-probe",
    ])
    assert result.exit_code == 0, result.stdout
    data = _config_json(isolated_home)
    entry = data["mcpServers"]["myserver"]
    assert entry["command"] == "echo"
    assert entry["args"] == ["hello"]
    assert entry["enabled"] is True


def test_add_http_with_header(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, [
        "add", "remote",
        "--url", "https://example.com/mcp",
        "--header", "Authorization: Bearer ${TEST_TOKEN}",
        "--no-probe",
    ])
    assert result.exit_code == 0, result.stdout
    entry = _config_json(isolated_home)["mcpServers"]["remote"]
    assert entry["url"] == "https://example.com/mcp"
    assert entry["headers"]["Authorization"] == "Bearer ${TEST_TOKEN}"


def test_add_rejects_both_command_and_url(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, [
        "add", "bad",
        "--command", "x",
        "--url", "https://example.com/mcp",
        "--no-probe",
    ])
    assert result.exit_code != 0


def test_add_rejects_neither_command_nor_url(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, ["add", "bad", "--no-probe"])
    assert result.exit_code != 0


def test_disable_flips_enabled(isolated_home: Path):
    runner = CliRunner()
    runner.invoke(mcp_app, ["add", "x", "--command", "echo", "--no-probe"])
    result = runner.invoke(mcp_app, ["disable", "x"])
    assert result.exit_code == 0, result.stdout
    assert _config_json(isolated_home)["mcpServers"]["x"]["enabled"] is False
    result = runner.invoke(mcp_app, ["enable", "x"])
    assert result.exit_code == 0
    assert _config_json(isolated_home)["mcpServers"]["x"]["enabled"] is True


def test_remove_deletes_entry_from_disk(isolated_home: Path):
    runner = CliRunner()
    runner.invoke(mcp_app, ["add", "alpha", "--command", "echo", "--no-probe"])
    runner.invoke(mcp_app, ["add", "beta", "--command", "echo", "--no-probe"])

    result = runner.invoke(mcp_app, ["remove", "alpha", "--yes"])
    assert result.exit_code == 0, result.stdout
    data = _config_json(isolated_home)
    assert "alpha" not in data["mcpServers"]
    assert "beta" in data["mcpServers"]


def test_remove_last_entry_drops_mcp_servers_key(isolated_home: Path):
    runner = CliRunner()
    runner.invoke(mcp_app, ["add", "only", "--command", "echo", "--no-probe"])
    runner.invoke(mcp_app, ["remove", "only", "--yes"])
    data = _config_json(isolated_home)
    assert "mcpServers" not in data


def test_list_runs_with_no_servers(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, ["list"])
    assert result.exit_code == 0
    assert "No MCP servers configured" in result.stdout


def test_list_shows_added_servers(isolated_home: Path):
    runner = CliRunner()
    runner.invoke(mcp_app, ["add", "shown", "--command", "echo", "--no-probe"])
    result = runner.invoke(mcp_app, ["list"])
    assert "shown" in result.stdout
    assert "stdio" in result.stdout


def test_remove_unknown_server_fails(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, ["remove", "ghost", "--yes"])
    assert result.exit_code != 0


# ── OAuth (Faz 2b) ──────────────────────────────────────────────────


def test_add_oauth_writes_auth_field(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, [
        "add", "secure",
        "--url", "https://secure.example.com/mcp",
        "--auth", "oauth",
        "--no-probe",
    ])
    assert result.exit_code == 0, result.stdout
    entry = _config_json(isolated_home)["mcpServers"]["secure"]
    assert entry["auth"] == "oauth"
    assert entry["url"] == "https://secure.example.com/mcp"


def test_add_oauth_requires_url(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, [
        "add", "bad", "--command", "echo", "--auth", "oauth", "--no-probe",
    ])
    assert result.exit_code != 0


def test_add_rejects_unknown_auth(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, [
        "add", "bad", "--url", "https://x.example/mcp", "--auth", "saml", "--no-probe",
    ])
    assert result.exit_code != 0


def test_login_on_non_oauth_server_fails(isolated_home: Path):
    runner = CliRunner()
    runner.invoke(mcp_app, ["add", "plain", "--url", "https://x.example/mcp", "--no-probe"])
    result = runner.invoke(mcp_app, ["login", "plain"])
    assert result.exit_code != 0
    assert "not configured for OAuth" in result.stdout


def test_login_on_stdio_server_fails(isolated_home: Path):
    runner = CliRunner()
    runner.invoke(mcp_app, ["add", "local", "--command", "echo", "--no-probe"])
    result = runner.invoke(mcp_app, ["login", "local"])
    assert result.exit_code != 0


def test_login_unknown_server_fails(isolated_home: Path):
    runner = CliRunner()
    result = runner.invoke(mcp_app, ["login", "ghost"])
    assert result.exit_code != 0
