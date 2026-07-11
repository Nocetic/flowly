"""Codex session policy (approval / sandbox) on the shared ``feature_rpc``
surface, plus its live-reload contract.

Unlike exec policy (its own store), codex_session policy lives in config.json —
but writing config alone is not enough: the warm Codex subprocess captured the
sandbox at spawn and the approval policy only reaches Codex via
~/.codex/config.toml. So ``codex.policy.set`` writes config and then live-reloads
through the host callback; a successful reload means no gateway restart, a
missing/failed callback falls back to one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from flowly.channels import feature_rpc


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_codex_cb():
    # The reload callback is a module global; keep tests independent.
    yield
    feature_rpc.set_codex_reload_callback(None)


def _dispatch(method: str, params: dict | None = None):
    return asyncio.run(feature_rpc.dispatch(method, params or {}))


def test_codex_policy_is_in_the_shared_surface():
    assert "codex.policy.get" in feature_rpc.FEATURE_METHODS
    assert "codex.policy.set" in feature_rpc.FEATURE_METHODS


def test_policy_get_returns_defaults():
    result, restart = _dispatch("codex.policy.get")
    assert restart is False
    # Schema defaults.
    assert result["approvalPolicy"] == "on-request"
    assert result["sandbox"] == "workspace-write"
    assert result["enabled"] is False


def test_policy_set_without_callback_persists_and_asks_for_restart():
    # No host callback wired → the change is written but the transport must
    # restart to apply it.
    result, restart = _dispatch(
        "codex.policy.set", {"approvalPolicy": "never", "sandbox": "read-only"}
    )
    assert result["willRestart"] is True
    assert restart is True  # dispatch echoes willRestart for restart-aware methods
    # Persisted.
    got, _ = _dispatch("codex.policy.get")
    assert got["approvalPolicy"] == "never"
    assert got["sandbox"] == "read-only"


def test_policy_set_with_live_reload_does_not_restart():
    calls = {"n": 0}

    async def fake_reload():
        calls["n"] += 1
        return {"ok": True, "registered": True, "sandbox": "read-only",
                "approvalPolicy": "never"}

    feature_rpc.set_codex_reload_callback(fake_reload)
    result, restart = _dispatch(
        "codex.policy.set", {"approvalPolicy": "never", "sandbox": "read-only"}
    )
    assert calls["n"] == 1
    assert result["willRestart"] is False
    assert restart is False
    # Status from the reload is surfaced to the caller.
    assert result["registered"] is True
    assert result["sandbox"] == "read-only"


def test_policy_set_reload_failure_falls_back_to_restart():
    async def boom():
        raise RuntimeError("subprocess would not die")

    feature_rpc.set_codex_reload_callback(boom)
    result, restart = _dispatch("codex.policy.set", {"approvalPolicy": "never"})
    # Config was still written; a restart applies it cleanly.
    assert result["willRestart"] is True
    assert restart is True
    got, _ = _dispatch("codex.policy.get")
    assert got["approvalPolicy"] == "never"


def test_policy_set_rejects_invalid_approval():
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("codex.policy.set", {"approvalPolicy": "yolo"})
    assert exc.value.code == "INVALID"


def test_policy_set_rejects_invalid_sandbox():
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("codex.policy.set", {"sandbox": "root"})
    assert exc.value.code == "INVALID"


def test_policy_set_rejects_empty():
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("codex.policy.set", {})
    assert exc.value.code == "INVALID"


def test_policy_set_partial_leaves_other_field():
    _dispatch("codex.policy.set", {"approvalPolicy": "never", "sandbox": "read-only"})
    _dispatch("codex.policy.set", {"sandbox": "full-access"})
    got, _ = _dispatch("codex.policy.get")
    assert got["approvalPolicy"] == "never"  # untouched
    assert got["sandbox"] == "full-access"
