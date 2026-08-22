from __future__ import annotations

import json
from pathlib import Path

import pytest

import flowly.profile as profiles


@pytest.fixture()
def profile_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    default = tmp_path / ".flowly"
    root = default / "profiles"
    default.mkdir()
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    monkeypatch.delenv("FLOWLY_HOME", raising=False)
    return default, root


def test_local_runtime_clone_keeps_provider_but_drops_transport_identity(profile_roots) -> None:
    default, root = profile_roots
    (default / "config.json").write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {"enabled": True, "token": "telegram-secret"},
                    "web": {
                        "enabled": True,
                        "relayUrl": "wss://relay.example",
                        "serverId": "srv_shared",
                        "authToken": "relay-secret",
                    },
                },
                "gateway": {"host": "0.0.0.0", "port": 19999, "token": "gateway-secret"},
                "providers": {
                    "active": "flowly_hosted",
                    "flowlyHosted": {
                        "enabled": True,
                        "accountKey": "flw_account",
                        "serverId": "srv_legacy",
                        "authToken": "legacy-secret",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (default / ".env").write_text(
        "OPENROUTER_API_KEY=keep-me\n"
        "FLOWLY_SERVER_ID=drop-me\n"
        "MOLTBOT_PROXY_JWT_SECRET=drop-me-too\n",
        encoding="utf-8",
    )

    created = profiles.create_profile(
        "research",
        clone_from="default",
        display_name="Research",
        description="Deep research profile",
        local_runtime=True,
    )

    assert created == root / "research"
    config = json.loads((created / "config.json").read_text(encoding="utf-8"))
    assert config["channels"]["telegram"]["enabled"] is False
    assert config["channels"]["web"] == {"enabled": False}
    assert config["gateway"] == {"host": "127.0.0.1", "port": 19999, "token": ""}
    assert config["providers"]["flowlyHosted"]["accountKey"] == "flw_account"
    assert "serverId" not in config["providers"]["flowlyHosted"]
    assert "authToken" not in config["providers"]["flowlyHosted"]
    assert (created / ".env").read_text(encoding="utf-8") == "OPENROUTER_API_KEY=keep-me\n"

    info = profiles.describe_profile("research")
    assert info.display_name == "Research"
    assert info.description == "Deep research profile"
    assert info.created_at.endswith("Z")
    assert info.to_dict()["path"] == str(created)


def test_profile_creation_is_atomic_when_clone_config_is_invalid(profile_roots) -> None:
    default, root = profile_roots
    (default / "config.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid config"):
        profiles.create_profile("broken", clone_from="default", local_runtime=True)

    assert not (root / "broken").exists()
    assert not list(default.parent.glob(".flowly-profile-broken.*"))


def test_clone_all_does_not_copy_machine_or_runtime_identity(profile_roots) -> None:
    default, root = profile_roots
    (default / "sessions").mkdir()
    (default / "sessions" / "chat.jsonl").write_text("history", encoding="utf-8")
    (default / ".machine-id").write_text("machine-a", encoding="utf-8")
    (default / ".desktop-runtime.json").write_text("{}", encoding="utf-8")

    created = profiles.create_profile("archive", clone_from="default", clone_all=True)

    assert (created / "sessions" / "chat.jsonl").read_text(encoding="utf-8") == "history"
    assert not (created / ".machine-id").exists()
    assert not (created / ".desktop-runtime.json").exists()


def test_profile_metadata_updates_without_touching_profile_data(profile_roots) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    sentinel = created / "sessions" / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")

    updated = profiles.update_profile_metadata(
        "writer",
        display_name="Writer Room",
        description="Long-form writing",
    )

    assert updated.display_name == "Writer Room"
    assert updated.description == "Long-form writing"
    assert updated.updated_at.endswith("Z")
    assert sentinel.read_text(encoding="utf-8") == "untouched"


def test_running_profile_cannot_be_deleted(profile_roots) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("active", local_runtime=True)
    (created / ".desktop-runtime.json").write_text(
        json.dumps({"instanceId": "runtime-1", "pid": profiles.os.getpid(), "port": 12345}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Stop it before deletion"):
        profiles.delete_profile("active")

    assert created.exists()


def test_stale_runtime_lease_is_pruned_before_delete(profile_roots, monkeypatch) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("stale", local_runtime=True)
    lease = created / ".desktop-runtime.json"
    lease.write_text(json.dumps({"instanceId": "old", "pid": 99999999}), encoding="utf-8")
    monkeypatch.setattr(profiles, "_pid_is_alive", lambda _pid: False)

    profiles.delete_profile("stale")

    assert not created.exists()
