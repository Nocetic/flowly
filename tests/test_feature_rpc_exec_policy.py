"""Exec approval policy on the shared ``feature_rpc`` surface.

The standing shell/exec policy lives in its own store
(``~/.flowly/credentials/exec-approvals.json``), NOT config.json. Exposing it
through ``feature_rpc`` (rather than a direct-gateway-only handler) means
Desktop-direct AND iOS-over-relay reach the same shape, and — because the
running executor reloads the store on its next command — a policy change never
requires a gateway restart.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from flowly.channels import feature_rpc
from flowly.exec.approvals import ExecApprovalStore


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    return tmp_path


def _dispatch(method: str, params: dict | None = None):
    return asyncio.run(feature_rpc.dispatch(method, params or {}))


def test_exec_policy_is_in_the_shared_surface():
    # Both transports gate on FEATURE_METHODS; membership is what lights the
    # relay path up (the direct gateway dispatches this set first too).
    assert "exec.policy.get" in feature_rpc.FEATURE_METHODS
    assert "exec.policy.set" in feature_rpc.FEATURE_METHODS
    assert "exec.policy.allowlist.remove" in feature_rpc.FEATURE_METHODS


def test_policy_get_returns_defaults():
    result, restart = _dispatch("exec.policy.get")
    assert restart is False
    assert result["security"] == "full"
    assert result["ask"] == "off"
    assert result["allowlist"] == []


def test_policy_set_persists_and_never_restarts():
    result, restart = _dispatch(
        "exec.policy.set", {"security": "allowlist", "ask": "always"}
    )
    # The whole point: exec policy is applied live by the executor's store
    # reload, so this path must never ask the transport to bounce the gateway.
    assert restart is False
    assert result["security"] == "allowlist"
    assert result["ask"] == "always"
    # Persisted to the store file the executor actually reads.
    assert ExecApprovalStore().load().security == "allowlist"


def test_policy_set_partial_only_touches_given_field():
    _dispatch("exec.policy.set", {"security": "allowlist", "ask": "always"})
    result, _ = _dispatch("exec.policy.set", {"ask": "on-miss"})
    assert result["security"] == "allowlist"  # untouched
    assert result["ask"] == "on-miss"


def test_policy_set_rejects_invalid_security():
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("exec.policy.set", {"security": "bogus"})
    assert exc.value.code == "INVALID"


def test_policy_set_rejects_invalid_ask():
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("exec.policy.set", {"ask": "sometimes"})
    assert exc.value.code == "INVALID"


def test_policy_set_rejects_empty():
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("exec.policy.set", {})
    assert exc.value.code == "INVALID"


def test_allowlist_remove():
    seed = ExecApprovalStore()
    seed.load()
    seed.add_to_allowlist(pattern="/usr/bin/git", command="git *")

    result, restart = _dispatch(
        "exec.policy.allowlist.remove", {"pattern": "/usr/bin/git"}
    )
    assert restart is False
    assert result["removed"] is True
    assert result["allowlist"] == []
    assert ExecApprovalStore().load().allowlist == []


def test_allowlist_remove_missing_pattern_is_false():
    result, _ = _dispatch("exec.policy.allowlist.remove", {"pattern": "/nope"})
    assert result["removed"] is False


def test_allowlist_remove_requires_pattern():
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("exec.policy.allowlist.remove", {})
    assert exc.value.code == "INVALID"


def test_set_replaces_allowlist():
    # A settings screen manages the whole list; sending it via set replaces the
    # stored one (works local AND over the relay, unlike a local file write).
    result, restart = _dispatch(
        "exec.policy.set",
        {"security": "allowlist", "allowlist": [{"pattern": "/usr/bin/git"}, {"pattern": "/bin/ls"}]},
    )
    assert restart is False
    assert result["security"] == "allowlist"
    assert [e["pattern"] for e in result["allowlist"]] == ["/usr/bin/git", "/bin/ls"]
    assert [e.pattern for e in ExecApprovalStore().load().allowlist] == ["/usr/bin/git", "/bin/ls"]


def test_set_allowlist_accepts_bare_strings():
    result, _ = _dispatch("exec.policy.set", {"allowlist": ["/opt/tool"]})
    assert [e["pattern"] for e in result["allowlist"]] == ["/opt/tool"]


def test_set_allowlist_can_clear():
    _dispatch("exec.policy.set", {"allowlist": [{"pattern": "/x"}]})
    result, _ = _dispatch("exec.policy.set", {"allowlist": []})
    assert result["allowlist"] == []


def test_set_rejects_non_list_allowlist():
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("exec.policy.set", {"allowlist": "nope"})
    assert exc.value.code == "INVALID"


def test_set_rejects_allowlist_entry_without_pattern():
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        _dispatch("exec.policy.set", {"allowlist": [{"cmd": "x"}]})
    assert exc.value.code == "INVALID"
