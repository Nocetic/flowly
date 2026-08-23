from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import flowly.profile as profiles
from flowly.cli.profile_cmd import profile_app


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
                "agents": {"defaults": {"workspace": "~/.flowly/workspace", "model": "old/model"}},
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
        mark_text="RS",
        mark_tone="violet",
        local_runtime=True,
    )

    assert created == root / "research"
    config = json.loads((created / "config.json").read_text(encoding="utf-8"))
    assert config["channels"]["telegram"]["enabled"] is False
    assert config["channels"]["web"] == {"enabled": False}
    assert config["gateway"] == {"host": "127.0.0.1", "port": 19999, "token": ""}
    assert config["agents"]["defaults"]["workspace"] == str(created / "workspace")
    assert config["providers"]["flowlyHosted"]["accountKey"] == "flw_account"
    assert "serverId" not in config["providers"]["flowlyHosted"]
    assert "authToken" not in config["providers"]["flowlyHosted"]
    assert (created / ".env").read_text(encoding="utf-8") == "OPENROUTER_API_KEY=keep-me\n"

    info = profiles.describe_profile("research")
    assert info.display_name == "Research"
    assert info.description == "Deep research profile"
    assert info.model
    assert info.to_dict()["model"] == info.model
    assert info.mark_text == "RS"
    assert info.mark_tone == "violet"
    assert info.to_dict()["markText"] == "RS"
    assert info.to_dict()["markTone"] == "violet"
    assert info.created_at.endswith("Z")
    assert info.to_dict()["path"] == str(created)


def test_profile_creation_is_atomic_when_clone_config_is_invalid(profile_roots) -> None:
    default, root = profile_roots
    (default / "config.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid config"):
        profiles.create_profile("broken", clone_from="default", local_runtime=True)

    assert not (root / "broken").exists()
    assert not list(default.parent.glob(".flowly-profile-broken.*"))


def test_current_profile_name_follows_process_home_not_sticky_selection(
    profile_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    default, root = profile_roots
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    assert profiles.current_profile_name() == "default"

    monkeypatch.setenv("FLOWLY_HOME", str(root / "research"))
    assert profiles.current_profile_name() == "research"

    monkeypatch.setenv("FLOWLY_HOME", str(root / "bad profile"))
    assert profiles.current_profile_name() == "default"


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
        mark_text="WR",
        mark_tone="amber",
    )

    assert updated.display_name == "Writer Room"
    assert updated.description == "Long-form writing"
    assert updated.mark_text == "WR"
    assert updated.mark_tone == "amber"
    assert updated.updated_at.endswith("Z")
    assert sentinel.read_text(encoding="utf-8") == "untouched"


def test_fresh_local_profile_gets_isolated_workspace_config(profile_roots) -> None:
    _default, root = profile_roots

    created = profiles.create_profile("fresh", local_runtime=True)

    config = json.loads((created / "config.json").read_text(encoding="utf-8"))
    assert config["agents"]["defaults"]["workspace"] == str(root / "fresh" / "workspace")


def test_profile_model_and_soul_are_isolated_and_editable(profile_roots) -> None:
    default, _root = profile_roots
    (default / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"model": "base/model"}}}),
        encoding="utf-8",
    )

    created = profiles.create_profile(
        "analyst",
        clone_from="default",
        local_runtime=True,
        provider="openrouter",
        model="profile/model",
        soul="# Analyst\n\nVerify every claim.\n",
    )

    assert profiles.read_profile_settings("analyst") == {
        "name": "analyst",
        "provider": "openrouter",
        "model": "profile/model",
        "soul": "# Analyst\n\nVerify every claim.\n",
        "workspace": str(created / "workspace"),
    }
    updated = profiles.update_profile_settings(
        "analyst",
        provider="anthropic",
        model="profile/model-v2",
        soul="",
    )
    assert updated["provider"] == "anthropic"
    assert updated["model"] == "profile/model-v2"
    assert updated["soul"] == ""
    assert json.loads((default / "config.json").read_text(encoding="utf-8"))["agents"]["defaults"]["model"] == "base/model"
    assert not (default / "workspace" / "SOUL.md").exists()


def test_running_profile_settings_cannot_change(profile_roots) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("busy", local_runtime=True)
    (created / ".desktop-runtime.json").write_text(
        json.dumps({"instanceId": "runtime-1", "pid": profiles.os.getpid(), "port": 12345}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Stop it before changing settings"):
        profiles.update_profile_settings("busy", model="new/model")


def test_profile_cli_round_trips_isolated_settings(profile_roots) -> None:
    default, _root = profile_roots
    (default / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"model": "base/model"}}}),
        encoding="utf-8",
    )
    runner = CliRunner()

    created = runner.invoke(profile_app, [
        "create", "planner", "--clone-from", "default", "--local-only",
        "--display-name", "Planner", "--provider", "openrouter", "--model", "planner/model",
        "--mark-text", "PL", "--mark-tone", "sky",
        "--soul", "Plan before acting.\n", "--json",
    ])
    assert created.exit_code == 0, created.output

    settings = runner.invoke(profile_app, ["settings", "planner", "--json"])
    assert settings.exit_code == 0, settings.output
    payload = json.loads(settings.output)
    assert payload["settings"]["provider"] == "openrouter"
    assert payload["settings"]["model"] == "planner/model"
    assert payload["settings"]["soul"] == "Plan before acting.\n"
    described = runner.invoke(profile_app, ["describe", "planner", "--json"])
    descriptor = json.loads(described.output)["profile"]
    assert descriptor["markText"] == "PL"
    assert descriptor["markTone"] == "sky"


def test_profile_mark_rejects_invalid_values(profile_roots) -> None:
    with pytest.raises(ValueError, match="one or two"):
        profiles.create_profile("writer", mark_text="LONG")
    with pytest.raises(ValueError, match="Unknown profile mark tone"):
        profiles.create_profile("writer", mark_tone="neon")
    with pytest.raises(ValueError, match="Unknown model provider"):
        profiles.create_profile("writer", provider="unknown-provider")


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
