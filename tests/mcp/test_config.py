"""Tests for the ``mcpServers`` slice of the Flowly config.

We cover:

* camelCase → snake_case round-trip on read.
* snake_case → camelCase round-trip on save.
* Unknown server-level fields survive a save (so a user who pre-fills
  ``transport: sse`` doesn't lose it before Faz 2 reads that field).
* ``enabled: false`` parses correctly and is preserved.
* Empty config yields an empty ``mcp_servers`` dict — no MCP-related
  validation errors for users who never use MCP.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowly.config.loader import (
    convert_keys,
    convert_to_camel,
    load_config,
    save_config,
)
from flowly.config.schema import Config, MCPServerConfig


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


def test_empty_config_has_no_mcp_servers(isolated_home: Path):
    cfg = Config()
    assert cfg.mcp_servers == {}


def test_camel_case_round_trip(isolated_home: Path):
    payload = {
        "mcpServers": {
            "context7": {
                "enabled": True,
                "command": "npx",
                "args": ["-y", "@upstash/context7-mcp"],
                "timeout": 60,
                "connectTimeout": 30,
                "tools": {
                    "include": [],
                    "exclude": ["dangerous_tool"],
                    "resources": False,
                    "prompts": False,
                },
            },
        },
    }
    cfg = Config.model_validate(convert_keys(payload))
    assert "context7" in cfg.mcp_servers
    entry = cfg.mcp_servers["context7"]
    assert entry.command == "npx"
    assert entry.connect_timeout == 30.0
    assert entry.tools.exclude == ["dangerous_tool"]

    back = convert_to_camel(cfg.model_dump())
    assert "mcpServers" in back
    fake = back["mcpServers"]["context7"]
    assert fake["connectTimeout"] == 30.0
    assert fake["tools"]["exclude"] == ["dangerous_tool"]


def test_save_and_load_preserves_mcp_servers(isolated_home: Path):
    cfg = Config()
    cfg.mcp_servers = {
        "demo": MCPServerConfig(
            command="echo",
            args=["hello"],
            timeout=15.0,
            connect_timeout=5.0,
        ),
    }
    save_config(cfg)

    on_disk = json.loads((isolated_home / "config.json").read_text())
    assert on_disk["mcpServers"]["demo"]["connectTimeout"] == 5.0

    reloaded = load_config()
    assert reloaded.mcp_servers["demo"].connect_timeout == 5.0


def test_unknown_server_field_preserved_across_save(isolated_home: Path):
    """Hand-edited unknown keys must survive an agent-driven save_config.

    The save path deep-merges the Pydantic dump onto whatever JSON is
    already on disk, so fields the schema doesn't know about should
    persist. This matters for users pre-staging future-Faz fields.
    """
    path = isolated_home / "config.json"
    path.write_text(json.dumps({
        "mcpServers": {
            "preview": {
                "command": "echo",
                "futureKnob": "kept",
            },
        },
    }, indent=4))

    cfg = load_config()
    save_config(cfg)

    on_disk = json.loads(path.read_text())
    # Known field still there
    assert on_disk["mcpServers"]["preview"]["command"] == "echo"
    # Unknown field still there
    assert on_disk["mcpServers"]["preview"]["futureKnob"] == "kept"


def test_disabled_entry_round_trips(isolated_home: Path):
    cfg = Config()
    cfg.mcp_servers = {
        "off": MCPServerConfig(command="true", enabled=False),
    }
    save_config(cfg)
    assert load_config().mcp_servers["off"].enabled is False


def test_env_and_header_keys_survive_round_trip(isolated_home: Path):
    # Regression: convert_keys/convert_to_camel must NOT camel/snake-case the
    # KEYS inside env/headers maps. An UPPER_SNAKE env name like
    # GITHUB_PERSONAL_ACCESS_TOKEN would otherwise be mangled on save/load.
    cfg = Config()
    cfg.mcp_servers = {
        "gh": MCPServerConfig(
            command="npx",
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"},
        ),
        "remote": MCPServerConfig(
            url="https://x/mcp",
            headers={"X-Custom-Header": "v", "Authorization": "Bearer ${T}"},
        ),
    }
    save_config(cfg)

    on_disk = json.loads((isolated_home / "config.json").read_text())
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in on_disk["mcpServers"]["gh"]["env"]
    assert "X-Custom-Header" in on_disk["mcpServers"]["remote"]["headers"]

    reloaded = load_config()
    assert reloaded.mcp_servers["gh"].env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${GITHUB_PERSONAL_ACCESS_TOKEN}"
    assert reloaded.mcp_servers["remote"].headers["X-Custom-Header"] == "v"


def test_underscore_server_name_survives(isolated_home: Path):
    # Server NAMES (mcpServers keys) must also survive verbatim.
    cfg = Config()
    cfg.mcp_servers = {"my_local_server": MCPServerConfig(command="echo")}
    save_config(cfg)
    on_disk = json.loads((isolated_home / "config.json").read_text())
    assert "my_local_server" in on_disk["mcpServers"]
    assert "my_local_server" in load_config().mcp_servers


def test_reap_orphans_defaults_off_and_round_trips(isolated_home: Path):
    # Fix 2: orphan reaping must be opt-in (default False) so the default
    # path can never force-kill a sibling subprocess.
    assert MCPServerConfig(command="x").reap_orphans is False

    cfg = Config()
    cfg.mcp_servers = {"r": MCPServerConfig(command="x", reap_orphans=True)}
    save_config(cfg)
    assert load_config().mcp_servers["r"].reap_orphans is True
