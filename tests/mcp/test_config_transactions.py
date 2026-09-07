import json
from contextlib import contextmanager

import pytest

from flowly.config.loader import load_config, save_config
from flowly.config.transaction import config_write_lock
from flowly.integrations.config_io import _atomic_write_json, _load_raw_or_empty
from flowly.mcp.connections import MCPConnectionService
from flowly.mcp.setup import MCPConfigStore


def test_stale_settings_save_does_not_restore_revoked_tools_or_deleted_connections(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": {"demo": {"command": "demo", "tools": {"mode": "all"}}},
                                "channels": {"email": {"enabled": True}}}))
    stale = load_config(path)
    store = MCPConfigStore(path)
    entry, revision = store.entry("demo")
    entry["tools"] = {"mode": "none"}
    store.publish("demo", entry, revision, lambda: None)
    save_config(stale, path)
    assert json.loads(path.read_text())["mcpServers"]["demo"]["tools"]["mode"] == "none"
    assert json.loads(path.read_text())["channels"]["email"]["enabled"] is True
    _, revision = store.entry("demo")
    store.publish("demo", None, revision, lambda: None)
    save_config(stale, path)
    assert "demo" not in json.loads(path.read_text()).get("mcpServers", {})
    stale.mcp_servers["demo"].enabled = False
    with pytest.raises(ValueError, match="connections changed"):
        save_config(stale, path)


def test_raw_editor_conflict_cannot_undo_later_mcp_or_mail_save(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"channels":{"email":{"enabled":true}}}')
    stale = _load_raw_or_empty(path)
    store = MCPConfigStore(path)
    _, revision = store.entry("demo")
    store.publish("demo", {"command": "demo", "tools": {"mode": "none"}}, revision, lambda: None)
    stale["theme"] = "dark"
    with pytest.raises(ValueError, match="changed while editing"):
        _atomic_write_json(path, stale)
    saved = json.loads(path.read_text())
    assert saved["channels"]["email"]["enabled"] is True
    assert saved["mcpServers"]["demo"]["tools"]["mode"] == "none"


def test_all_new_and_legacy_writers_use_the_same_bounded_lease(tmp_path):
    path = tmp_path / "config.json"
    config = load_config(path)
    raw = _load_raw_or_empty(path)
    store = MCPConfigStore(path)
    _, revision = store.entry("demo")
    with config_write_lock(path):
        for write in (lambda: save_config(config, path), lambda: _atomic_write_json(path, raw),
                      lambda: store.publish("demo", {"command": "demo"}, revision, lambda: None)):
            with pytest.raises(TimeoutError):
                write()
    assert not path.exists()


def test_malformed_connection_cannot_break_owner_listing_or_use_other_profile(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": {"broken": {"command": 12, "url": {}, "args": 12, "tools": "invalid"}}}))
    monkeypatch.setattr("flowly.integrations.config_io._load_raw", lambda: {"mcpServers": {"other-profile": {"command": "other"}}})
    service = MCPConnectionService(path, lambda: None)
    rows = service.list()["servers"]
    assert not any(row["name"] == "other-profile" for row in rows)
    broken = next(row for row in rows if row["name"] == "broken")
    assert broken["runtimeState"] == "invalid"
    assert broken["permissions"]["mode"] == "none"


def test_recovery_rechecks_after_lock_instead_of_restoring_stale_permissions(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{broken')
    path.with_suffix('.json.bak').write_text(json.dumps({"mcpServers": {"demo": {"command": "demo", "tools": {"mode": "all"}}}}))
    latest = {"mcpServers": {"demo": {"command": "demo", "tools": {"mode": "none"}}}}
    @contextmanager
    def concurrent_repair(_path):
        path.write_text(json.dumps(latest))
        yield
    monkeypatch.setattr("flowly.config.loader.config_write_lock", concurrent_repair)
    assert load_config(path).mcp_servers["demo"].tools.mode == "none"
    assert json.loads(path.read_text()) == latest
