"""State database routing for the shared Desktop/iOS feature RPC surface."""

from __future__ import annotations

from flowly.channels import feature_rpc
from flowly.memory.governance import GovernanceStore, STATUS_ACTIVE


def test_state_db_prefers_the_runtime_canonical_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    canonical = tmp_path / "memory_governance.sqlite3"
    legacy = tmp_path / "workspace" / ".flowly_state" / "memory_governance.sqlite3"
    legacy.parent.mkdir(parents=True)
    canonical.touch()
    legacy.touch()

    assert feature_rpc.state_db("memory_governance.sqlite3") == canonical


def test_state_db_falls_back_to_a_legacy_store_when_canonical_is_absent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    legacy = tmp_path / "workspace" / ".flowly_state" / "memory_governance.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.touch()

    assert feature_rpc.state_db("memory_governance.sqlite3") == legacy


def test_state_db_defaults_new_state_to_the_profile_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))

    assert feature_rpc.state_db("memory_governance.sqlite3") == (
        tmp_path / "memory_governance.sqlite3"
    )


def test_memory_rpc_ignores_an_empty_shadow_store(tmp_path, monkeypatch):
    """Regression: a stray workspace DB must not hide the live agent's memory."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    canonical = GovernanceStore(tmp_path / "memory_governance.sqlite3")
    item = canonical.add_item(
        kind="preference",
        text="prefers the canonical store",
        status=STATUS_ACTIVE,
        confidence=0.9,
    )
    canonical.close()

    shadow = GovernanceStore(
        tmp_path / "workspace" / ".flowly_state" / "memory_governance.sqlite3"
    )
    shadow.close()

    result = feature_rpc.memory_gov("list", {"status": "active"})

    assert [row["id"] for row in result["items"]] == [item.id]
