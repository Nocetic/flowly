"""Tests for the ``mcp.*`` feature-RPC surface.

These RPCs back the desktop "MCP" tab (and any other remote client) over both
the relay channel and the direct gateway. They are thin wrappers over
:mod:`flowly.integrations.mcp_io`; the contract pinned here:

* ``mcp.list`` returns configured servers + installable catalog entries, in the
  camelCase wire shape the desktop expects.
* ``mcp.upsert`` validates a manual server config and writes it back verbatim
  (camelCase, env keys untouched), and is restart-aware.
* ``mcp.set_enabled`` / ``mcp.remove`` mutate the on-disk ``mcpServers`` map and
  signal ``willRestart``.
* Bad input raises a structured :class:`FeatureRpcError` rather than crashing.

The connect-once probe (``mcp.test`` / ``mcp.oauth_start``) is exercised by the
shared :mod:`flowly.mcp.probe` tests; here we only assert dispatch wiring and
argument validation, never a live network connect.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from flowly.channels import feature_rpc


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


def _dispatch(method: str, params: dict | None = None):
    return asyncio.run(feature_rpc.dispatch(method, params or {}))


def _config(home: Path) -> dict:
    path = home / "config.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ── registration ────────────────────────────────────────────────────────────

def test_methods_registered():
    expected = {
        "mcp.list", "mcp.upsert", "mcp.set_enabled",
        "mcp.remove", "mcp.install", "mcp.test", "mcp.oauth_start",
    }
    assert expected <= feature_rpc.FEATURE_METHODS


# ── list ─────────────────────────────────────────────────────────────────────

def test_list_empty_returns_catalog_only(isolated_home):
    result, restart = _dispatch("mcp.list")
    assert restart is False
    assert "servers" in result
    # No configured servers yet, but catalog entries are installable rows.
    assert all(s["source"] in ("configured", "catalog") for s in result["servers"])
    assert all(s["status"] == "available" for s in result["servers"])


def test_list_shape_is_camelcase(isolated_home):
    _dispatch("mcp.upsert", {"name": "demo", "config": {"command": "echo"}})
    result, _ = _dispatch("mcp.list")
    demo = next(s for s in result["servers"] if s["name"] == "demo")
    # Wire keys are camelCase, not the dataclass snake_case.
    for key in ("toolFilter", "needsOauth", "needsSecrets", "secretFields"):
        assert key in demo
    assert demo["source"] == "configured"
    assert demo["status"] == "enabled"


# ── upsert ───────────────────────────────────────────────────────────────────

def test_upsert_writes_verbatim_camelcase(isolated_home):
    result, restart = _dispatch("mcp.upsert", {
        "name": "github",
        "config": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "tok"},
        },
        "restart": True,
    })
    assert result["ok"] is True
    assert restart is True  # restart-aware

    on_disk = _config(isolated_home)["mcpServers"]["github"]
    assert on_disk["command"] == "npx"
    assert on_disk["connectTimeout"] == 60.0  # snake field rendered camelCase
    # env map keys survive verbatim (not snake-cased).
    assert on_disk["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "tok"}


def test_upsert_http_server(isolated_home):
    result, _ = _dispatch("mcp.upsert", {
        "name": "remote",
        "config": {"url": "https://example.com/mcp", "headers": {"X-Key": "v"}},
    })
    assert result["ok"] is True
    on_disk = _config(isolated_home)["mcpServers"]["remote"]
    assert on_disk["url"] == "https://example.com/mcp"
    assert on_disk["headers"] == {"X-Key": "v"}


def test_upsert_requires_transport(isolated_home):
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("mcp.upsert", {"name": "bad", "config": {"env": {}}})
    assert exc.value.code == "INVALID"


def test_upsert_requires_name(isolated_home):
    with pytest.raises(feature_rpc.FeatureRpcError):
        _dispatch("mcp.upsert", {"name": "", "config": {"command": "echo"}})


def test_upsert_drops_unknown_field(isolated_home):
    # Stray fields are harmless — silently ignored, not written to disk.
    result, _ = _dispatch("mcp.upsert", {"name": "x", "config": {"command": "echo", "bogus": 1}})
    assert result["ok"] is True
    assert "bogus" not in _config(isolated_home)["mcpServers"]["x"]


# ── set_enabled / remove ─────────────────────────────────────────────────────

def test_set_enabled_toggles(isolated_home):
    _dispatch("mcp.upsert", {"name": "demo", "config": {"command": "echo"}})
    result, restart = _dispatch("mcp.set_enabled", {"name": "demo", "enabled": False})
    assert result == {"ok": True, "enabled": False, "willRestart": True}
    assert restart is True
    assert _config(isolated_home)["mcpServers"]["demo"]["enabled"] is False


def test_set_enabled_unknown_raises(isolated_home):
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("mcp.set_enabled", {"name": "nope", "enabled": True})
    assert exc.value.code == "NOT_FOUND"


def test_remove_deletes(isolated_home):
    _dispatch("mcp.upsert", {"name": "demo", "config": {"command": "echo"}})
    result, restart = _dispatch("mcp.remove", {"name": "demo"})
    assert result["ok"] is True
    assert restart is True
    assert "mcpServers" not in _config(isolated_home) or \
        "demo" not in _config(isolated_home).get("mcpServers", {})


def test_remove_unknown_raises(isolated_home):
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("mcp.remove", {"name": "nope"})
    assert exc.value.code == "NOT_FOUND"


# ── test / oauth_start validation (no live connect) ──────────────────────────

def test_mcp_test_requires_name_or_config(isolated_home):
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("mcp.test", {})
    assert exc.value.code == "INVALID"


def test_mcp_test_no_transport_fails_gracefully(isolated_home):
    # A config with neither command nor url can't connect — the probe returns
    # ok=False with an error string rather than raising.
    result, restart = _dispatch("mcp.test", {"config": {"timeout": 5}})
    assert restart is False
    assert result["ok"] is False
    assert result["error"]


def test_oauth_start_requires_oauth_server(isolated_home):
    _dispatch("mcp.upsert", {"name": "remote", "config": {"url": "https://x/mcp"}})
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("mcp.oauth_start", {"name": "remote"})
    assert exc.value.code == "INVALID"  # auth != oauth


def test_oauth_start_rejects_stdio(isolated_home):
    _dispatch("mcp.upsert", {"name": "local", "config": {"command": "echo", "auth": "oauth"}})
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("mcp.oauth_start", {"name": "local"})
    assert exc.value.code == "INVALID"  # stdio has no url


def test_oauth_start_unknown_raises(isolated_home):
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("mcp.oauth_start", {"name": "nope"})
    assert exc.value.code == "NOT_FOUND"
