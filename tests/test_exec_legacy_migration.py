"""One-time migration: when the exec-approvals store doesn't exist yet, seed
its security/ask from any legacy ``tools.exec`` values in config.json (written
by older builds whose wizard pointed at the wrong place). An existing store is
never overwritten.
"""

import json

import pytest

from flowly.exec.approvals import ExecApprovalStore


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(h))
    return h


def _write_config(home, exec_block):
    (home / "config.json").write_text(
        json.dumps({"tools": {"exec": exec_block}}), encoding="utf-8"
    )


def test_seeds_from_legacy_config(home):
    _write_config(home, {"enabled": True, "security": "allowlist", "ask": "always"})

    cfg = ExecApprovalStore().load()

    assert cfg.security == "allowlist"
    assert cfg.ask == "always"
    # And it's now persisted so the seed is stable.
    assert ExecApprovalStore().load().security == "allowlist"


def test_defaults_when_no_config(home):
    cfg = ExecApprovalStore().load()
    assert cfg.security == "full"
    assert cfg.ask == "off"


def test_defaults_when_config_has_no_exec_policy(home):
    _write_config(home, {"enabled": True})  # no security/ask
    cfg = ExecApprovalStore().load()
    assert cfg.security == "full"


def test_invalid_legacy_security_falls_back(home):
    _write_config(home, {"security": "bogus", "ask": "always"})
    cfg = ExecApprovalStore().load()
    assert cfg.security == "full"  # bogus ignored
    assert cfg.ask == "off"


def test_invalid_ask_falls_back_but_security_kept(home):
    _write_config(home, {"security": "deny", "ask": "nonsense"})
    cfg = ExecApprovalStore().load()
    assert cfg.security == "deny"
    assert cfg.ask == "off"  # invalid ask → safe default


def test_existing_store_is_not_overwritten(home):
    # Store already configured to "deny"...
    first = ExecApprovalStore()
    fcfg = first.load()
    fcfg.security = "deny"
    first.save()

    # ...and config.json says "allowlist". The store must win — migration only
    # fires when the store file is absent.
    _write_config(home, {"security": "allowlist", "ask": "always"})

    cfg = ExecApprovalStore().load()
    assert cfg.security == "deny"
