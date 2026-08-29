from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

import flowly.profile as profiles
from flowly.cli.profile_cmd import profile_app
from flowly.profile_room_store import SQLiteRoomStore


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
        "AWS_SECRET_ACCESS_KEY=drop-custom-secret\n"
        "GITHUB_TOKEN=drop-custom-token\n"
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
    assert str(uuid.UUID(info.bot_id)) == info.bot_id
    assert info.to_public_dict()["botId"] == info.bot_id
    assert info.to_public_dict()["credentialPolicy"] == "isolated"
    assert "path" not in info.to_public_dict()


def test_legacy_profile_and_host_ids_are_backfilled_once(profile_roots) -> None:
    default, root = profile_roots
    (default / "config.json").write_text("{}", encoding="utf-8")
    legacy = root / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text("{}", encoding="utf-8")
    (legacy / "profile.json").write_text(
        json.dumps({"version": 1, "displayName": "Legacy"}),
        encoding="utf-8",
    )

    first = profiles.ensure_profile_bot_id("legacy")
    second = profiles.ensure_profile_bot_id("legacy")
    first_host = profiles.get_or_create_profile_host_id()
    second_host = profiles.get_or_create_profile_host_id()

    assert first.bot_id == second.bot_id == str(uuid.UUID(first.bot_id))
    assert first_host == second_host == str(uuid.UUID(first_host))
    assert first.to_public_dict()["displayName"] == "Legacy"
    assert "path" not in first.to_public_dict()


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


def test_profile_skill_counts_match_runtime_loader(profile_roots, tmp_path, monkeypatch) -> None:
    default, root = profile_roots
    from flowly.agent import skills as skills_module

    builtin = tmp_path / "builtin-skills"
    for parent, name in [
        (builtin, "bundled"),
        (default / "skills", "managed"),
        (default / "workspace" / "skills", "workspace"),
    ]:
        skill = parent / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    # A folder is not a skill unless the runtime loader can find SKILL.md.
    (default / "skills" / "not-a-skill").mkdir()

    named = root / "writer"
    duplicate = named / "skills" / "bundled"
    duplicate.mkdir(parents=True)
    (duplicate / "SKILL.md").write_text("# override\n", encoding="utf-8")
    local = named / "workspace" / "skills" / "writer-only"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("# writer\n", encoding="utf-8")

    monkeypatch.setattr(skills_module, "BUILTIN_SKILLS_DIR", builtin)

    listed = {item.name: item for item in profiles.list_profiles()}
    assert listed["default"].skill_count == 3
    assert listed["writer"].skill_count == 2
    assert listed["default"].to_public_dict()["skillCount"] == 3
    assert listed["default"].to_public_dict()["skillCountVerified"] is True


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
        "credentialPolicy": "isolated",
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


def test_profile_cli_reads_persona_from_owner_only_file(profile_roots, tmp_path: Path) -> None:
    _default, _root = profile_roots
    soul_file = tmp_path / "SOUL.md"
    soul_file.write_text("Keep private instructions out of argv.\n", encoding="utf-8")
    soul_file.chmod(0o600)
    runner = CliRunner()

    created = runner.invoke(profile_app, [
        "create", "private", "--local-only", "--soul-file", str(soul_file), "--json",
    ])
    assert created.exit_code == 0, created.output
    assert profiles.read_profile_settings("private")["soul"] == (
        "Keep private instructions out of argv.\n"
    )


def test_profile_cli_rejects_shared_persona_file(profile_roots, tmp_path: Path) -> None:
    _default, _root = profile_roots
    soul_file = tmp_path / "SOUL.md"
    soul_file.write_text("Do not accept shared instructions.\n", encoding="utf-8")
    soul_file.chmod(0o640)

    created = CliRunner().invoke(
        profile_app,
        ["create", "private", "--local-only", "--soul-file", str(soul_file), "--json"],
        terminal_width=180,
    )

    assert created.exit_code != 0
    assert "Persona file must use 0600 permissions" in created.output


def test_profile_mark_rejects_invalid_values(profile_roots) -> None:
    with pytest.raises(ValueError, match="one or two"):
        profiles.create_profile("writer", mark_text="LONG")
    with pytest.raises(ValueError, match="Unknown profile mark tone"):
        profiles.create_profile("writer", mark_tone="neon")
    with pytest.raises(ValueError, match="Unknown profile mark tone"):
        profiles.create_profile("writer", mark_tone="#fff")
    with pytest.raises(ValueError, match="Unknown model provider"):
        profiles.create_profile("writer", provider="unknown-provider")


def test_profile_mark_accepts_normalized_custom_hex_color(profile_roots) -> None:
    profiles.create_profile("custom-mark", mark_tone="#FfEeDd")

    info = profiles.describe_profile("custom-mark")

    assert info.mark_tone == "#ffeedd"
    assert info.to_dict()["markTone"] == "#ffeedd"


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


def test_symlink_profile_is_never_listed_or_cloned(profile_roots, tmp_path: Path) -> None:
    _default, root = profile_roots
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    assert "linked" not in {profile.name for profile in profiles.list_profiles()}
    assert profiles.profile_exists("linked") is False
    with pytest.raises(FileNotFoundError):
        profiles.create_profile("copy", clone_from="linked", clone_all=True)


def test_clone_all_rejects_links_inside_source(profile_roots, tmp_path: Path) -> None:
    _default, _root = profile_roots
    source = profiles.create_profile("source", local_runtime=True)
    secret = tmp_path / "outside-secret"
    secret.write_text("do not copy", encoding="utf-8")
    (source / "workspace" / "linked-secret").symlink_to(secret)

    with pytest.raises(ValueError, match="symbolic link"):
        profiles.create_profile("copy", clone_from="source", clone_all=True)
    assert not profiles.profile_exists("copy")


def test_runtime_lease_rejects_pid_reuse(profile_roots, monkeypatch) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("lease", local_runtime=True)
    lease = created / ".desktop-runtime.json"
    lease.write_text(json.dumps({
        "version": 2,
        "instanceId": "old-runtime",
        "pid": profiles.os.getpid(),
        "processIdentity": "old-process",
    }), encoding="utf-8")
    monkeypatch.setattr(profiles, "_process_identity", lambda _pid: "new-process")

    assert profiles.read_runtime_lease(created) is None
    assert not lease.exists()


def test_runtime_lease_publishes_owner_only_attach_endpoint(
    profile_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    monkeypatch.setenv("FLOWLY_HOME", str(created))
    instance_id = "runtime-attach-test"
    token = "t" * 48

    lease_path = profiles.claim_runtime_lease(instance_id)
    profiles.update_runtime_lease(instance_id, port=19_191, auth_token=token)
    lease = profiles.read_runtime_lease(created)

    assert lease is not None
    assert lease["port"] == 19_191
    assert lease["authToken"] == token
    assert lease_path.stat().st_mode & 0o777 == 0o600
    profiles.release_runtime_lease(instance_id)


def test_runtime_lease_rejects_invalid_attach_endpoint(
    profile_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    monkeypatch.setenv("FLOWLY_HOME", str(created))
    instance_id = "runtime-invalid-endpoint"
    profiles.claim_runtime_lease(instance_id)

    with pytest.raises(ValueError, match="endpoint is invalid"):
        profiles.update_runtime_lease(instance_id, port=True, auth_token="t" * 48)
    with pytest.raises(ValueError, match="endpoint is invalid"):
        profiles.update_runtime_lease(instance_id, port=19_191, auth_token="short")

    lease = profiles.read_runtime_lease(created)
    assert lease is not None
    assert lease["port"] == 0
    assert "authToken" not in lease
    profiles.release_runtime_lease(instance_id)


def test_corrupt_runtime_lease_fails_closed_before_profile_mutation(profile_roots) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    (created / ".desktop-runtime.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="ownership data is unreadable"):
        profiles.delete_profile("writer")

    assert created.exists()


def test_managed_runtime_command_requires_exact_profile_and_ephemeral_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = {
        101: ["python", "/venv/bin/flowly", "--profile", "writer", "serve", "--port", "0"],
        102: ["flowly", "--profile=other", "serve", "--port=0"],
        103: ["flowly", "--profile", "writer", "serve", "--port", "9119"],
        104: ["python", "notes.py", "flowly", "--profile", "writer", "serve", "--port", "0"],
    }
    monkeypatch.setattr(profiles, "_process_command", commands.get)

    assert profiles._is_managed_profile_runtime_command(101, "writer") is True
    assert profiles._is_managed_profile_runtime_command(102, "writer") is False
    assert profiles._is_managed_profile_runtime_command(103, "writer") is False
    assert profiles._is_managed_profile_runtime_command(104, "writer") is False


def test_desktop_reconciles_legacy_runtime_reparented_to_init(
    profile_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    lease_path = created / ".desktop-runtime.json"
    lease_path.write_text(
        json.dumps({"version": 1, "instanceId": "legacy", "pid": 101}),
        encoding="utf-8",
    )
    alive = {101, 102, 700}
    commands = {
        101: ["python", "/venv/bin/flowly", "--profile", "writer", "serve", "--port", "0"],
        102: ["uv", "tool", "run", "flowly", "--profile", "writer", "serve", "--port", "0"],
    }
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_PID", "700")
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_INSTANCE", "desktop-a")
    monkeypatch.setattr(profiles, "_pid_is_alive", lambda pid: pid in alive)
    monkeypatch.setattr(profiles, "_process_identity", lambda pid: f"identity-{pid}" if pid in alive else None)
    monkeypatch.setattr(profiles, "_process_command", commands.get)
    monkeypatch.setattr(profiles, "_process_parent_pid", lambda pid: {101: 102, 102: 1}.get(pid))
    monkeypatch.setattr(profiles.os, "kill", lambda pid, _signal: alive.discard(pid))

    assert profiles.reconcile_runtime_lease(created, profile_name="writer") is None
    assert not lease_path.exists()


def test_desktop_preserves_legacy_runtime_with_another_live_parent(
    profile_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    lease = {"version": 1, "instanceId": "legacy", "pid": 101}
    (created / ".desktop-runtime.json").write_text(json.dumps(lease), encoding="utf-8")
    commands = {
        101: ["python", "/venv/bin/flowly", "--profile", "writer", "serve", "--port", "0"],
        102: ["uv", "tool", "run", "flowly", "--profile", "writer", "serve", "--port", "0"],
        333: ["/Applications/Flowly.app/Contents/MacOS/Flowly"],
    }
    kills: list[tuple[int, int]] = []
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_PID", "700")
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_INSTANCE", "desktop-a")
    monkeypatch.setattr(profiles, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(profiles, "_process_identity", lambda pid: f"identity-{pid}")
    monkeypatch.setattr(profiles, "_process_command", commands.get)
    monkeypatch.setattr(
        profiles,
        "_process_parent_pid",
        lambda pid: {101: 102, 102: 333, 333: 1}.get(pid),
    )
    monkeypatch.setattr(profiles.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    assert profiles.reconcile_runtime_lease(created, profile_name="writer") == lease
    assert kills == []


@pytest.mark.parametrize("same_manager,manager_dead", [(True, False), (False, True)])
def test_desktop_reconciles_owned_or_dead_manager_v2_runtime(
    profile_roots,
    monkeypatch: pytest.MonkeyPatch,
    same_manager: bool,
    manager_dead: bool,
) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    old_manager = 700 if same_manager else 333
    lease_path = created / ".desktop-runtime.json"
    lease_path.write_text(
        json.dumps({
            "version": 2,
            "instanceId": "runtime-a",
            "pid": 101,
            "processIdentity": "identity-101",
            "managerPid": old_manager,
            "managerIdentity": "old-manager" if manager_dead else f"identity-{old_manager}",
            "managerInstance": "desktop-a" if same_manager else "desktop-old",
        }),
        encoding="utf-8",
    )
    alive = {101, 333, 700}
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_PID", "700")
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_INSTANCE", "desktop-a")
    monkeypatch.setattr(profiles, "_pid_is_alive", lambda pid: pid in alive)
    monkeypatch.setattr(profiles, "_process_identity", lambda pid: f"identity-{pid}" if pid in alive else None)
    monkeypatch.setattr(profiles.os, "kill", lambda pid, _signal: alive.discard(pid))

    assert profiles.reconcile_runtime_lease(created, profile_name="writer") is None
    assert not lease_path.exists()


def test_desktop_preserves_v2_runtime_owned_by_another_live_manager(
    profile_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    lease = {
        "version": 2,
        "instanceId": "runtime-a",
        "pid": 101,
        "processIdentity": "identity-101",
        "managerPid": 333,
        "managerIdentity": "identity-333",
        "managerInstance": "desktop-old",
    }
    (created / ".desktop-runtime.json").write_text(json.dumps(lease), encoding="utf-8")
    kills: list[tuple[int, int]] = []
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_PID", "700")
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_INSTANCE", "desktop-a")
    monkeypatch.setattr(profiles, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(profiles, "_process_identity", lambda pid: f"identity-{pid}")
    monkeypatch.setattr(profiles.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    assert profiles.reconcile_runtime_lease(created, profile_name="writer") == lease
    assert kills == []


def test_cli_without_desktop_identity_never_reaps_a_live_runtime(
    profile_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    lease = {"version": 1, "instanceId": "legacy", "pid": 101}
    (created / ".desktop-runtime.json").write_text(json.dumps(lease), encoding="utf-8")
    kills: list[tuple[int, int]] = []
    monkeypatch.delenv("FLOWLY_DESKTOP_MANAGER_PID", raising=False)
    monkeypatch.delenv("FLOWLY_DESKTOP_MANAGER_INSTANCE", raising=False)
    monkeypatch.setattr(profiles, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(profiles.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    assert profiles.reconcile_runtime_lease(created, profile_name="writer") == lease
    assert kills == []


def test_desktop_manager_identity_is_passed_across_the_sandbox_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_PID", "700")
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_INSTANCE", "desktop-a")
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_IDENTITY", "ps:desktop-start")
    monkeypatch.setattr(profiles, "_pid_is_alive", lambda pid: pid == 700)
    monkeypatch.setattr(profiles, "_process_identity", lambda _pid: None)

    assert profiles._desktop_manager_context() == (700, "desktop-a", "ps:desktop-start")


def test_desktop_manager_identity_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_PID", "700")
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_INSTANCE", "desktop-a")
    monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_IDENTITY", "ps:old-start")
    monkeypatch.setattr(profiles, "_pid_is_alive", lambda pid: pid == 700)
    monkeypatch.setattr(profiles, "_process_identity", lambda _pid: "ps:new-start")

    assert profiles._desktop_manager_context() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX orphan lifecycle integration")
def test_real_legacy_orphan_is_reaped_before_desktop_reclaims_profile(
    profile_roots,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    pid_file = tmp_path / "child.pid"
    entrypoint = tmp_path / "flowly"
    entrypoint.write_text(
        "import os,sys,time\n"
        "pid=os.fork()\n"
        "if pid:\n"
        " open(sys.argv[-1], 'w').write(str(pid))\n"
        " os._exit(0)\n"
        "os.setsid()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    launcher = subprocess.Popen([
        sys.executable,
        str(entrypoint),
        "--profile",
        "writer",
        "serve",
        "--port",
        "0",
        str(pid_file),
    ])
    launcher.wait(timeout=5)
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        while profiles._process_parent_pid(child_pid) not in (0, 1):
            if time.monotonic() >= deadline:
                pytest.fail("test runtime was not reparented to init")
            time.sleep(0.02)
        lease_path = created / ".desktop-runtime.json"
        lease_path.write_text(
            json.dumps({"version": 1, "instanceId": "legacy", "pid": child_pid}),
            encoding="utf-8",
        )
        monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_PID", str(os.getpid()))
        monkeypatch.setenv("FLOWLY_DESKTOP_MANAGER_INSTANCE", "integration-desktop")

        assert profiles.reconcile_runtime_lease(created, profile_name="writer") is None
        assert not lease_path.exists()
        assert profiles._pid_is_alive(child_pid) is False
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_profile_import_rejects_path_traversal(profile_roots, tmp_path: Path) -> None:
    _default, _root = profile_roots
    archive = tmp_path / "malicious.tar.gz"
    payload = b"escaped"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("alpha/../../escaped.txt")
        member.size = len(payload)
        bundle.addfile(member, io.BytesIO(payload))

    with pytest.raises(ValueError, match="unsafe entry"):
        profiles.import_profile(str(archive), name="imported")
    assert not (tmp_path / "escaped.txt").exists()
    assert not profiles.profile_exists("imported")


def test_profile_import_extracts_atomically(profile_roots, tmp_path: Path) -> None:
    _default, root = profile_roots
    archive = tmp_path / "valid.tar.gz"
    payload = b'{"agents":{"defaults":{"model":"test/model"}}}'
    with tarfile.open(archive, "w:gz") as bundle:
        directory = tarfile.TarInfo("exported")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        bundle.addfile(directory)
        member = tarfile.TarInfo("exported/config.json")
        member.size = len(payload)
        member.mode = 0o600
        bundle.addfile(member, io.BytesIO(payload))

    imported = profiles.import_profile(str(archive), name="imported")
    assert imported == root / "imported"
    assert (imported / "config.json").read_bytes() == payload
    assert (imported.stat().st_mode & 0o777) == 0o700
    assert ((imported / "config.json").stat().st_mode & 0o777) == 0o600
    assert not list(_default.parent.glob(".flowly-import-*"))


def test_profile_creation_enforces_installation_limit(profile_roots) -> None:
    for index in range(1, profiles.MAX_NAMED_PROFILES + 1):
        profiles.create_profile(f"worker-{index}", local_runtime=True)

    with pytest.raises(profiles.ProfileLimitError, match="at most 15 bots"):
        profiles.create_profile("overflow", local_runtime=True)

    assert len(profiles.list_profiles()) == profiles.MAX_NAMED_PROFILES + 1


def test_profile_import_enforces_installation_limit_without_partial_publish(
    profile_roots, tmp_path: Path,
) -> None:
    archive = tmp_path / "portable.tar.gz"
    payload = b'{}'
    with tarfile.open(archive, "w:gz") as bundle:
        directory = tarfile.TarInfo("portable")
        directory.type = tarfile.DIRTYPE
        bundle.addfile(directory)
        member = tarfile.TarInfo("portable/config.json")
        member.size = len(payload)
        bundle.addfile(member, io.BytesIO(payload))
    for index in range(1, profiles.MAX_NAMED_PROFILES + 1):
        profiles.create_profile(f"worker-{index}", local_runtime=True)

    with pytest.raises(profiles.ProfileLimitError, match="at most 15 bots"):
        profiles.import_profile(str(archive), name="overflow")

    assert not profiles.profile_exists("overflow")
    assert not list(tmp_path.glob(".flowly-import-*"))


def test_profile_export_round_trips_without_runtime_identity(
    profile_roots, tmp_path: Path
) -> None:
    _default, _root = profile_roots
    source = profiles.create_profile("writer", local_runtime=True)
    (source / "sessions" / "chat.jsonl").write_text("private history", encoding="utf-8")
    (source / ".desktop-runtime.json").write_text(
        json.dumps({"instanceId": "stale", "pid": 999_999_999}), encoding="utf-8"
    )

    archive = profiles.export_profile("writer", str(tmp_path / "writer-backup"))

    assert archive == tmp_path / "writer-backup.tar.gz"
    assert (archive.stat().st_mode & 0o777) == 0o600
    with tarfile.open(archive, "r:gz") as bundle:
        names = bundle.getnames()
        assert "writer/sessions/chat.jsonl" in names
        assert "writer/.desktop-runtime.json" not in names
        assert all((member.mode & 0o077) == 0 for member in bundle.getmembers())

    imported = profiles.import_profile(str(archive), name="writer-copy")
    assert (imported / "sessions" / "chat.jsonl").read_text(encoding="utf-8") == (
        "private history"
    )
    assert not (imported / ".desktop-runtime.json").exists()
    source_id = json.loads((source / "profile.json").read_text(encoding="utf-8"))["botId"]
    imported_id = json.loads((imported / "profile.json").read_text(encoding="utf-8"))["botId"]
    assert imported_id != source_id


def test_sensitive_export_snapshots_sqlite_rooms_without_wal_sidecars(
    profile_roots, tmp_path: Path,
) -> None:
    default, _root = profile_roots
    room = {
        "id": "55fc1b75-0b89-47e6-8974-504eef89249c",
        "title": "Private council",
        "members": ["default", "writer"],
        "mode": "panel",
        "messages": [],
        "watermarks": {"default": 0, "writer": 0},
        "createdAt": "2026-08-27T00:00:00.000Z",
        "updatedAt": "2026-08-27T00:00:00.000Z",
    }
    database = default / "profile-rooms.sqlite3"
    store = SQLiteRoomStore(database)
    assert store.initialize_verified({room["id"]: room}) is True

    archive = profiles.export_profile("default", str(tmp_path / "default-backup"))

    with tarfile.open(archive, "r:gz") as bundle:
        names = set(bundle.getnames())
        snapshot = bundle.extractfile("default/profile-rooms.sqlite3").read()
    assert "default/profile-rooms.sqlite3-wal" not in names
    assert "default/profile-rooms.sqlite3-shm" not in names
    restored_database = tmp_path / "restored-rooms.sqlite3"
    restored_database.write_bytes(snapshot)
    loaded = SQLiteRoomStore(restored_database).load()
    assert loaded.rooms == [room]


def test_profile_restore_preserves_unique_identity_and_rejects_collision(
    profile_roots, tmp_path: Path,
) -> None:
    source = profiles.create_profile("writer", local_runtime=True)
    source_id = json.loads((source / "profile.json").read_text(encoding="utf-8"))["botId"]
    archive = profiles.export_profile("writer", str(tmp_path / "writer-backup"))

    with pytest.raises(profiles.ProfileIdentityConflictError, match="already exists"):
        profiles.import_profile(
            str(archive), name="writer-restore", identity="restore",
        )
    assert not profiles.profile_exists("writer-restore")

    profiles.delete_profile("writer")
    restored = profiles.import_profile(
        str(archive), name="writer-restore", identity="restore",
    )
    restored_id = json.loads((restored / "profile.json").read_text(encoding="utf-8"))["botId"]
    assert restored_id == source_id


def test_profile_template_excludes_private_state_and_redacts_credentials(
    profile_roots, tmp_path: Path,
) -> None:
    source = profiles.create_profile("writer", local_runtime=True)
    source_config = {
        "channels": {
            "telegram": {"enabled": True, "token": "telegram-secret"},
        },
        "gateway": {"host": "0.0.0.0", "token": "gateway-secret"},
        "providers": {
            "sample": {
                "apiKey": "provider-secret",
                "accountKey": "account-secret",
                "model": "test/model",
            },
        },
    }
    (source / "config.json").write_text(json.dumps(source_config), encoding="utf-8")
    (source / ".env").write_text("OPENAI_API_KEY=private\n", encoding="utf-8")
    (source / "credentials" / "provider.json").write_text(
        '{"token":"private"}', encoding="utf-8",
    )
    (source / "mcp-tokens").mkdir()
    (source / "mcp-tokens" / "server.json").write_text(
        '{"access_token":"private"}', encoding="utf-8",
    )
    (source / "gmail-credentials.json").write_text(
        '{"refresh_token":"private"}', encoding="utf-8",
    )
    (source / "sessions" / "chat.jsonl").write_text("private chat", encoding="utf-8")
    (source / "profile-rooms.json").write_text("private legacy group", encoding="utf-8")
    (source / "profile-rooms.sqlite3").write_bytes(b"private sqlite group")
    (source / "profile-rooms.sqlite3-wal").write_bytes(b"private group wal")
    (source / "profile-rooms.sqlite3-shm").write_bytes(b"private group shm")
    (source / "workspace" / "memory" / "facts.md").write_text(
        "private memory", encoding="utf-8",
    )
    (source / "workspace" / "USER.md").write_text("private user", encoding="utf-8")
    (source / "workspace" / "TOOLS.md").write_text(
        "CUSTOM_API_KEY=private-token-value-1234567890\nReusable instructions",
        encoding="utf-8",
    )

    archive = profiles.export_profile_template("writer", str(tmp_path / "writer"))

    with tarfile.open(archive, "r:gz") as bundle:
        names = set(bundle.getnames())
        config = json.load(bundle.extractfile("writer/config.json"))
        metadata = json.load(bundle.extractfile("writer/profile.json"))
        tools_text = bundle.extractfile("writer/workspace/TOOLS.md").read().decode()
    assert "writer/.env" not in names
    assert not any(name.startswith("writer/credentials") for name in names)
    assert not any(name.startswith("writer/mcp-tokens") for name in names)
    assert "writer/gmail-credentials.json" not in names
    assert not any(name.startswith("writer/sessions") for name in names)
    assert not any("profile-rooms" in name for name in names)
    assert not any(name.startswith("writer/workspace/memory") for name in names)
    assert "writer/workspace/USER.md" not in names
    assert config["channels"]["telegram"] == {"enabled": False, "token": ""}
    assert config["gateway"] == {"host": "127.0.0.1", "token": ""}
    assert config["providers"]["sample"]["apiKey"] == ""
    assert config["providers"]["sample"]["accountKey"] == ""
    assert "botId" not in metadata
    assert metadata["localRuntime"] is True
    assert tools_text == "CUSTOM_API_KEY=\nReusable instructions"

    assert (source / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=private\n"
    assert json.loads((source / "config.json").read_text(encoding="utf-8")) == source_config


def test_encrypted_profile_backup_round_trips_private_state_and_identity(
    profile_roots, tmp_path: Path,
) -> None:
    source = profiles.create_profile("writer", local_runtime=True)
    source_id = json.loads((source / "profile.json").read_text(encoding="utf-8"))["botId"]
    (source / "sessions" / "chat.jsonl").write_text("private history", encoding="utf-8")
    password = "correct horse battery staple"

    backup = profiles.export_profile_backup("writer", str(tmp_path / "writer"), password)

    assert backup == tmp_path / "writer.flowly-backup"
    assert backup.read_bytes().startswith(profiles._BACKUP_MAGIC)
    assert b"private history" not in backup.read_bytes()
    assert (backup.stat().st_mode & 0o777) == 0o600
    profiles.delete_profile("writer")
    restored = profiles.import_profile(
        str(backup), name="writer", identity="restore", password=password,
    )
    assert (restored / "sessions" / "chat.jsonl").read_text(encoding="utf-8") == (
        "private history"
    )
    restored_id = json.loads((restored / "profile.json").read_text(encoding="utf-8"))["botId"]
    assert restored_id == source_id


def test_encrypted_profile_backup_rejects_output_inside_profile(
    profile_roots,
) -> None:
    source = profiles.create_profile("writer", local_runtime=True)

    with pytest.raises(ValueError, match="outside the profile directory"):
        profiles.export_profile_backup(
            "writer", str(source / "unsafe"), "correct horse battery staple",
        )
    assert not (source / "unsafe.flowly-backup").exists()


def test_encrypted_profile_backup_rejects_wrong_password_and_tampering(
    profile_roots, tmp_path: Path,
) -> None:
    profiles.create_profile("writer", local_runtime=True)
    password = "correct horse battery staple"
    backup = profiles.export_profile_backup("writer", str(tmp_path / "writer"), password)

    with pytest.raises(ValueError, match="incorrect or the backup was modified"):
        profiles.import_profile(
            str(backup), name="wrong-password", password="this password is wrong",
        )
    assert not profiles.profile_exists("wrong-password")

    damaged = bytearray(backup.read_bytes())
    damaged[len(profiles._BACKUP_MAGIC) + profiles._BACKUP_SALT_BYTES + 1] ^= 0x01
    tampered = tmp_path / "tampered.flowly-backup"
    tampered.write_bytes(damaged)
    with pytest.raises(ValueError, match="incorrect or the backup was modified"):
        profiles.import_profile(str(tampered), name="tampered", password=password)
    assert not profiles.profile_exists("tampered")
    assert not list(tmp_path.glob(".flowly-restore-*"))


def test_profile_import_race_never_deletes_another_publishers_profile(
    profile_roots, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _default, root = profile_roots
    archive = tmp_path / "valid.tar.gz"
    payload = b'{}'
    with tarfile.open(archive, "w:gz") as bundle:
        directory = tarfile.TarInfo("exported")
        directory.type = tarfile.DIRTYPE
        bundle.addfile(directory)
        member = tarfile.TarInfo("exported/config.json")
        member.size = len(payload)
        bundle.addfile(member, io.BytesIO(payload))

    original_validate = profiles._assert_tree_no_symlinks

    def publish_competitor(path: Path, *args, **kwargs) -> None:
        original_validate(path, *args, **kwargs)
        competing = root / "imported"
        competing.mkdir()
        (competing / "sentinel.txt").write_text("owned elsewhere", encoding="utf-8")

    monkeypatch.setattr(profiles, "_assert_tree_no_symlinks", publish_competitor)
    with pytest.raises(FileExistsError, match="already exists"):
        profiles.import_profile(str(archive), name="imported")
    assert (root / "imported" / "sentinel.txt").read_text(encoding="utf-8") == (
        "owned elsewhere"
    )


def test_default_profile_export_excludes_named_profiles_and_active_pointer(
    profile_roots, tmp_path: Path
) -> None:
    default, _root = profile_roots
    (default / "config.json").write_text("{}", encoding="utf-8")
    profiles.create_profile("worker", local_runtime=True)
    profiles.set_active_profile("worker")

    archive = profiles.export_profile("default", str(tmp_path / "default.tgz"))

    with tarfile.open(archive, "r:gz") as bundle:
        names = bundle.getnames()
    assert "default/config.json" in names
    assert all(not name.startswith("default/profiles") for name in names)
    assert "default/active_profile" not in names


def test_profile_export_rejects_live_runtime_and_symbolic_links(
    profile_roots, tmp_path: Path
) -> None:
    _default, _root = profile_roots
    source = profiles.create_profile("active", local_runtime=True)
    lease = source / ".desktop-runtime.json"
    lease.write_text(
        json.dumps({"instanceId": "runtime", "pid": profiles.os.getpid()}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Stop it before export"):
        profiles.export_profile("active", str(tmp_path / "active.tar.gz"))

    lease.unlink()
    (source / "workspace" / "outside-link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symbolic link"):
        profiles.export_profile("active", str(tmp_path / "unsafe.tar.gz"))
    assert not (tmp_path / "unsafe.tar.gz").exists()
    assert not list(tmp_path.glob(".unsafe.tar.gz.*.tmp"))


def test_profile_cli_exports_and_imports_archive(profile_roots, tmp_path: Path) -> None:
    profiles.create_profile("portable", local_runtime=True)
    archive = tmp_path / "portable.tar.gz"
    runner = CliRunner()

    exported = runner.invoke(
        profile_app, ["export", "portable", "--output", str(archive), "--json"]
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.output)["archive"] == str(archive)

    imported = runner.invoke(
        profile_app, ["import", str(archive), "--name", "restored", "--json"]
    )
    assert imported.exit_code == 0, imported.output
    assert json.loads(imported.output)["profile"]["name"] == "restored"


def test_local_profile_import_strips_transport_identity(profile_roots, tmp_path: Path) -> None:
    archive = tmp_path / "external.tar.gz"
    config = json.dumps({
        "channels": {
            "telegram": {"enabled": True, "token": "secret"},
            "web": {"enabled": True, "relayUrl": "wss://relay.example"},
        },
        "gateway": {"host": "0.0.0.0", "token": "gateway-secret"},
        "agents": {"defaults": {"workspace": "/foreign/path", "model": "test/model"}},
    }).encode()
    environment = b"OPENAI_API_KEY=keep\nFLOWLY_SERVER_ID=drop\n"
    with tarfile.open(archive, "w:gz") as bundle:
        directory = tarfile.TarInfo("external")
        directory.type = tarfile.DIRTYPE
        bundle.addfile(directory)
        for filename, payload in (("config.json", config), (".env", environment)):
            member = tarfile.TarInfo(f"external/{filename}")
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))

    imported = profiles.import_profile(
        str(archive), name="safe-copy", local_runtime=True,
    )

    imported_config = json.loads((imported / "config.json").read_text(encoding="utf-8"))
    assert imported_config["channels"]["telegram"]["enabled"] is False
    assert imported_config["channels"]["web"] == {"enabled": False}
    assert imported_config["gateway"]["host"] == "127.0.0.1"
    assert imported_config["gateway"]["token"] == ""
    assert imported_config["agents"]["defaults"]["workspace"] == str(
        imported / "workspace"
    )
    assert (imported / ".env").read_text(encoding="utf-8") == (
        "OPENAI_API_KEY=keep\n"
    )
    assert json.loads((imported / "profile.json").read_text(encoding="utf-8"))[
        "localRuntime"
    ] is True


def test_mark_seeds_are_allocated_in_creation_order(profile_roots) -> None:
    """Seeds are what let a roster spread colour and silhouette evenly, so the
    runtime — not any one client — owns the counter."""
    profiles.create_profile("alpha", local_runtime=True)
    profiles.create_profile("beta", local_runtime=True)
    profiles.create_profile("gamma", local_runtime=True)

    seeds = {
        name: profiles.describe_profile(name).mark_seed
        for name in ("alpha", "beta", "gamma")
    }
    assert sorted(seeds.values()) == [0, 1, 2]
    assert seeds["alpha"] < seeds["beta"] < seeds["gamma"]
    assert profiles.describe_profile("alpha").to_dict()["markSeed"] == 0


def test_deleting_a_profile_never_renumbers_the_survivors(profile_roots) -> None:
    """A seed is identity for life. Compacting the range on delete would
    repaint every bot created after the deleted one."""
    profiles.create_profile("alpha", local_runtime=True)
    profiles.create_profile("beta", local_runtime=True)
    profiles.create_profile("gamma", local_runtime=True)
    before = profiles.describe_profile("gamma").mark_seed

    profiles.delete_profile("beta")

    assert profiles.describe_profile("gamma").mark_seed == before
    profiles.create_profile("delta", local_runtime=True)
    # The gap left by beta stays a gap; delta continues past the maximum.
    assert profiles.describe_profile("delta").mark_seed == before + 1


def test_a_pinned_seed_is_honoured_and_survives_unrelated_edits(profile_roots) -> None:
    profiles.create_profile("pinned", local_runtime=True, mark_seed=41)
    assert profiles.describe_profile("pinned").mark_seed == 41

    profiles.update_profile_metadata("pinned", display_name="Renamed")
    info = profiles.describe_profile("pinned")
    assert info.display_name == "Renamed"
    assert info.mark_seed == 41, "an edit that never mentions the seed must not clear it"

    profiles.update_profile_metadata("pinned", mark_seed=7)
    assert profiles.describe_profile("pinned").mark_seed == 7


def test_rejects_seeds_that_are_not_whole_numbers(profile_roots) -> None:
    for bad in (-1, "abc", 1.5, True, 10**9):
        with pytest.raises(ValueError):
            profiles.create_profile(f"bad-{abs(hash(str(bad))) % 9999}", local_runtime=True, mark_seed=bad)


def test_a_corrupt_seed_degrades_instead_of_breaking_the_roster(profile_roots) -> None:
    """Reads must tolerate what writes reject: a hand-edited metadata file
    should cost one bot its spread, not make every bot unlistable."""
    profiles.create_profile("research", local_runtime=True)
    metadata_path = profiles.describe_profile("research").path / profiles._PROFILE_METADATA_FILE
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["markSeed"] = "not-a-number"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    info = profiles.describe_profile("research")
    assert info.mark_seed is None
    assert info.to_dict()["markSeed"] is None
    assert any(profile.name == "research" for profile in profiles.list_profiles())


def test_backfill_seeds_seedless_profiles_oldest_first(profile_roots) -> None:
    profiles.create_profile("older", local_runtime=True)
    profiles.create_profile("newer", local_runtime=True)

    # Simulate profiles created before marks were generated.
    for name, created_at in (("older", "2024-01-01T00:00:00Z"), ("newer", "2025-06-01T00:00:00Z")):
        path = profiles.describe_profile(name).path / profiles._PROFILE_METADATA_FILE
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata.pop("markSeed", None)
        metadata["createdAt"] = created_at
        path.write_text(json.dumps(metadata), encoding="utf-8")
    assert profiles.describe_profile("older").mark_seed is None

    assigned = profiles.backfill_mark_seeds()

    assert assigned["older"] < assigned["newer"]
    assert profiles.describe_profile("older").mark_seed == assigned["older"]
    assert profiles.describe_profile("newer").mark_seed == assigned["newer"]
    # Running it again is a no-op: already-seeded profiles are left alone.
    assert profiles.backfill_mark_seeds() == {}


def test_backfill_does_not_collide_with_already_seeded_profiles(profile_roots) -> None:
    profiles.create_profile("kept", local_runtime=True, mark_seed=5)
    profiles.create_profile("legacy", local_runtime=True)
    path = profiles.describe_profile("legacy").path / profiles._PROFILE_METADATA_FILE
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata.pop("markSeed", None)
    path.write_text(json.dumps(metadata), encoding="utf-8")

    assigned = profiles.backfill_mark_seeds()

    assert assigned["legacy"] == 6
    assert profiles.describe_profile("kept").mark_seed == 5


def test_backfill_marks_cli_reports_what_it_assigned(profile_roots) -> None:
    profiles.create_profile("legacy", local_runtime=True)
    path = profiles.describe_profile("legacy").path / profiles._PROFILE_METADATA_FILE
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata.pop("markSeed", None)
    path.write_text(json.dumps(metadata), encoding="utf-8")

    result = CliRunner().invoke(profile_app, ["backfill-marks", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["assigned"]["legacy"] == 0
    assert profiles.describe_profile("legacy").mark_seed == 0

    # Idempotent: a second run has nothing left to do.
    again = json.loads(CliRunner().invoke(profile_app, ["backfill-marks", "--json"]).output)
    assert again["assigned"] == {}


def test_create_cli_accepts_a_pinned_seed(profile_roots) -> None:
    result = CliRunner().invoke(
        profile_app, ["create", "pinned", "--local-only", "--mark-seed", "12", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["profile"]["markSeed"] == 12
