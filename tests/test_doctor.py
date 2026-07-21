from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from flowly.cli import doctor as doctor_module
from flowly.cli.doctor import run_doctor
from flowly.config.loader import convert_to_camel
from flowly.config.schema import Config
from flowly.diagnostics.checks import _service_command_and_home, check_profile_isolation
from flowly.diagnostics.config import find_unknown_keys, read_config_snapshot
from flowly.diagnostics.models import DoctorCheck, DoctorContext, Status
from flowly.diagnostics.repairs import (
    repair_config_backup,
    repair_config_duplicates,
    repair_memory,
    repair_sessions,
)


def _write_config(home: Path) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(home / "workspace")
    config.providers.active = "openrouter"
    config.providers.openrouter.api_key = "sk-or-v1-test"
    raw = convert_to_camel(config.model_dump())
    path = home / "config.json"
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return config


def _seed_workspace(home: Path) -> None:
    workspace = home / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "skills").mkdir()
    (workspace / "personas").mkdir()
    for name in ("AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "HEARTBEAT.md"):
        (workspace / name).write_text(f"# {name}\n", encoding="utf-8")
    (workspace / "memory" / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")


def _tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    snapshot: dict[str, tuple[bytes, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
    return snapshot


def test_default_doctor_is_offline_and_filesystem_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "flowly-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    _write_config(home)
    _seed_workspace(home)
    before = _tree_snapshot(home)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("default doctor touched a mutating or external API")

    monkeypatch.setattr("flowly.config.loader.load_config", forbidden)
    monkeypatch.setattr("flowly.config.loader.save_config", forbidden)
    monkeypatch.setattr("flowly.account.auth.load_account_sync", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr("httpx.get", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)

    code = run_doctor(json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["readOnly"] is True
    assert payload["online"] is False
    assert _tree_snapshot(home) == before
    assert not (home / "config.json.bak").exists()
    assert not (home / "credentials" / ".keychain-broken").exists()


@pytest.mark.parametrize("content", ["[]", '"not-an-object"', "{broken"])
def test_malformed_config_never_crashes_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    (home / "config.json").write_text(content, encoding="utf-8")

    code = run_doctor(json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["status"] == "unhealthy"
    assert payload["summary"]["internal"] == 0


def test_check_exception_is_isolated_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    _write_config(home)

    def explode(_ctx):
        raise RuntimeError("secret-value-must-not-leak")

    def survive(ctx):
        ctx.ok("survived", "later check ran")

    monkeypatch.setattr(
        doctor_module,
        "CHECKS",
        [DoctorCheck("explode", "test", explode), DoctorCheck("survive", "test", survive)],
    )

    code = run_doctor(json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert [item["name"] for item in payload["results"]] == ["explode", "survived"]
    assert "secret-value-must-not-leak" not in json.dumps(payload)


def test_snapshot_detects_exact_and_known_alias_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        """
        {
          "gateway": {"port": 18790, "port": 18791},
          "providers": {
            "openrouter": {"api_key": "first", "apiKey": "second"}
          }
        }
        """,
        encoding="utf-8",
    )

    snapshot = read_config_snapshot(path)

    assert snapshot.raw is not None
    assert snapshot.raw["gateway"]["port"] == 18791
    assert snapshot.raw["providers"]["openrouter"]["apiKey"] == "second"
    assert "gateway.port + gateway.port" in snapshot.duplicates
    assert "providers.openrouter.api_key + providers.openrouter.apiKey" in snapshot.duplicates


def test_snapshot_preserves_opaque_and_named_map_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "server_one": {
                        "command": "python",
                        "env": {
                            "API_KEY": "first",
                            "apiKey": "second",
                            "foo_bar": "A",
                            "fooBar": "B",
                        },
                    },
                    "serverOne": {"command": "node"},
                }
            }
        ),
        encoding="utf-8",
    )

    snapshot = read_config_snapshot(path)

    assert snapshot.config is not None
    assert snapshot.duplicates == ()
    env = snapshot.raw["mcpServers"]["server_one"]["env"]
    assert env == {
        "API_KEY": "first",
        "apiKey": "second",
        "foo_bar": "A",
        "fooBar": "B",
    }
    assert set(snapshot.raw["mcpServers"]) == {"server_one", "serverOne"}


def test_unknown_key_scan_respects_dynamic_and_opaque_maps() -> None:
    raw = {
        "agents": {"agents": {"coder": {"name": "Coder", "modle": "typo"}}},
        "mcpServers": {
            "server_one": {
                "command": "python",
                "commnad": "typo",
                "env": {"API_KEY": "kept", "apiKey": "also-kept"},
            }
        },
    }

    unknown = find_unknown_keys(raw)

    assert "agents.agents.coder.modle" in unknown
    assert "mcpServers.server_one.commnad" in unknown
    assert not any(item.endswith("API_KEY") or item.endswith("apiKey") for item in unknown)


def test_schema_error_does_not_echo_secret_input(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"gateway":{"port":"secret-value-must-not-leak"}}', encoding="utf-8")

    snapshot = read_config_snapshot(path)

    assert snapshot.config is None
    assert "gateway.port" in snapshot.error
    assert "secret-value-must-not-leak" not in snapshot.error


def test_middle_orphan_tool_call_is_diagnosed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    _write_config(home)
    _seed_workspace(home)
    sessions = home / "sessions"
    sessions.mkdir()
    transcript = sessions / "cli_test.jsonl"
    records = [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "exec"}}],
        },
        {"role": "user", "content": "hello again"},
    ]
    transcript.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    code = run_doctor(json_output=True)
    payload = json.loads(capsys.readouterr().out)
    session_result = next(item for item in payload["results"] if item["name"] == "sessions")

    assert code == 1
    assert session_result["status"] == "error"
    assert "tool" in (session_result["message"] + session_result["detail"]).lower()


def test_fix_mode_creates_only_low_risk_profile_local_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("FLOWLY_HOME", str(home))

    code = run_doctor(fix=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["readOnly"] is False
    fixed = {item["name"] for item in payload["results"] if item["status"] == "fixed"}
    assert {"state_dir", "config_file", "workspace"} <= fixed
    snapshot = read_config_snapshot(home / "config.json")
    assert snapshot.config is not None
    assert snapshot.config.agents.defaults.workspace == str(home / "workspace")
    assert (home / "workspace" / "memory" / "MEMORY.md").is_file()


def test_named_duplicate_repair_preserves_opaque_maps_and_runtime_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        """
        {
          "providers": {
            "active": "openrouter",
            "openrouter": {
              "api_key": "first",
              "apiKey": "effective",
              "apiBase": "https://openrouter.ai/api/v1"
            }
          },
          "mcpServers": {
            "server_one": {
              "command": "python",
              "env": {
                "API_KEY": "first",
                "apiKey": "second",
                "foo_bar": "A",
                "fooBar": "B"
              }
            },
            "serverOne": {"command": "node"}
          }
        }
        """,
        encoding="utf-8",
    )
    path.chmod(0o600)

    outcome = repair_config_duplicates(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    env = raw["mcpServers"]["server_one"]["env"]

    assert raw["providers"]["openrouter"]["apiKey"] == "effective"
    assert "api_key" not in raw["providers"]["openrouter"]
    assert env == {
        "API_KEY": "first",
        "apiKey": "second",
        "foo_bar": "A",
        "fooBar": "B",
    }
    assert set(raw["mcpServers"]) == {"server_one", "serverOne"}
    assert read_config_snapshot(path).duplicates == ()
    assert len(outcome.changed_paths) == 2
    assert outcome.changed_paths[1].read_text(encoding="utf-8").find('"api_key"') >= 0


def test_safe_fix_does_not_touch_medium_risk_duplicate_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    path = home / "config.json"
    original = (
        '{"providers":{"openrouter":{"api_key":"first","apiKey":"second"}},'
        f'"agents":{{"defaults":{{"workspace":"{home / "workspace"}"}}}}}}'
    )
    path.write_text(original, encoding="utf-8")
    path.chmod(0o600)

    code = run_doctor(fix=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert path.read_text(encoding="utf-8") == original
    duplicate = next(item for item in payload["results"] if item["name"] == "duplicate_keys")
    assert duplicate["risk"] == "medium"


def test_duplicate_repair_rolls_back_when_post_write_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flowly.diagnostics import repairs
    from flowly.diagnostics.config import ConfigSnapshot

    path = tmp_path / "config.json"
    path.write_text(
        '{"providers":{"openrouter":{"api_key":"first","apiKey":"second"}}}',
        encoding="utf-8",
    )
    original = path.read_bytes()
    real_snapshot = repairs.read_config_snapshot
    calls = 0

    def fail_active_verification(candidate: Path):
        nonlocal calls
        calls += 1
        if calls == 3:
            return ConfigSnapshot(None, None, "injected verification failure")
        return real_snapshot(candidate)

    monkeypatch.setattr(repairs, "read_config_snapshot", fail_active_verification)

    with pytest.raises(ValueError, match="injected verification failure"):
        repairs.repair_config_duplicates(path)

    assert path.read_bytes() == original
    assert list(tmp_path.glob("config.json.doctor-backup-*"))


def test_session_salvage_trims_at_first_semantic_break_and_keeps_forensic_backup(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    path = sessions / "cli_test.jsonl"
    records = [
        {"role": "user", "content": "keep me"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "exec"}}],
        },
        {"role": "user", "content": "must be discarded with the broken turn"},
        {"role": "assistant", "content": "also discarded"},
    ]
    original = "".join(json.dumps(record) + "\n" for record in records)
    path.write_text(original, encoding="utf-8")

    outcome = repair_sessions(home)

    repaired = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert repaired == [{"role": "user", "content": "keep me"}]
    assert "removed 3 unsafe record(s)" in outcome.message
    backup = next(item for item in outcome.changed_paths if "doctor-backup" in item.name)
    assert backup.read_text(encoding="utf-8") == original


def test_session_salvage_does_not_skip_corrupt_middle_tool_result(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    path = sessions / "web_chat.jsonl"
    path.write_text(
        json.dumps({"role": "user", "content": "keep"})
        + "\n"
        + json.dumps(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "function": {"name": "exec"}}],
            }
        )
        + "\n"
        + "{corrupt-tool-result\n"
        + json.dumps({"role": "user", "content": "later but unsafe"})
        + "\n",
        encoding="utf-8",
    )

    repair_sessions(home)

    repaired = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert repaired == [{"role": "user", "content": "keep"}]


def test_session_salvage_refuses_when_no_safe_message_prefix(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    path = sessions / "broken.jsonl"
    original = b"{broken\n"
    path.write_bytes(original)

    with pytest.raises(ValueError, match="no safely salvageable"):
        repair_sessions(home)

    assert path.read_bytes() == original
    assert not list(sessions.glob("*.doctor-backup-*"))


def test_config_backup_repair_preserves_broken_original(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.json"
    config = Config()
    config.agents.defaults.workspace = str(home / "workspace")
    healthy = json.dumps(convert_to_camel(config.model_dump()), indent=2).encode()
    config_path.write_bytes(b"{broken-active")
    config_path.with_suffix(".json.bak").write_bytes(healthy)

    outcome = repair_config_backup(config_path)

    assert read_config_snapshot(config_path).config is not None
    forensic = next(path for path in outcome.changed_paths if "doctor-backup" in path.name)
    assert forensic.read_bytes() == b"{broken-active"


def _write_governance_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                privacy_level TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO memory_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("m1", "preference", "Likes careful repairs", "active", 0.9, "normal", "1"),
                ("m2", "fact", "secret-value-must-not-leak", "active", 1.0, "secret", "2"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_memory_regenerate_is_transactional_and_preserves_manual_content(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_config(home)
    memory_dir = home / "workspace" / "memory"
    memory_dir.mkdir(parents=True)
    memory_path = memory_dir / "MEMORY.md"
    original = (
        "Manual before\n\n"
        "<!-- FLOWLY-GENERATED-START -->\nold generated\n"
        "<!-- FLOWLY-GENERATED-END -->\n\n"
        "Manual after\n"
    )
    memory_path.write_text(original, encoding="utf-8")
    _write_governance_db(home / "memory_governance.sqlite3")

    outcome = repair_memory(home, home / "config.json")
    repaired = memory_path.read_text(encoding="utf-8")

    assert "Manual before" in repaired
    assert "Manual after" in repaired
    assert "Likes careful repairs" in repaired
    assert "old generated" not in repaired
    assert "secret-value-must-not-leak" not in repaired
    forensic = next(path for path in outcome.changed_paths if "doctor-backup" in path.name)
    assert forensic.read_text(encoding="utf-8") == original


def test_memory_regenerate_refuses_active_wal_without_touching_memory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_config(home)
    memory_dir = home / "workspace" / "memory"
    memory_dir.mkdir(parents=True)
    memory_path = memory_dir / "MEMORY.md"
    memory_path.write_text("manual\n", encoding="utf-8")
    db = home / "memory_governance.sqlite3"
    _write_governance_db(db)
    db.with_name(db.name + "-wal").write_bytes(b"active")

    with pytest.raises(RuntimeError, match="active WAL"):
        repair_memory(home, home / "config.json")

    assert memory_path.read_text(encoding="utf-8") == "manual\n"
    assert not list(memory_dir.glob("*.doctor-backup-*"))


def test_online_probe_uses_flowly_account_key_from_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    config = Config()
    config.agents.defaults.workspace = str(home / "workspace")
    config.providers.active = "flowly"
    config.providers.flowly.account_key = "flw_runtime_account_key"
    config_path = home / "config.json"
    config_path.write_text(
        json.dumps(convert_to_camel(config.model_dump())),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    calls: list[tuple[str, dict]] = []

    class Response:
        status_code = 200

        def json(self):
            return {"status": "ok", "auth_required": False, "capabilities": []}

    def fake_get(url: str, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("httpx.get", fake_get)

    code = run_doctor(online=True, categories={"online"}, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["readOnly"] is True
    assert payload["online"] is True
    provider_call = next(call for call in calls if "useflowlyapp.com" in call[0])
    assert provider_call[1]["headers"]["Authorization"] == "Bearer flw_runtime_account_key"
    assert "flw_runtime_account_key" not in json.dumps(payload)
    results = {item["name"]: item for item in payload["results"]}
    assert results["provider_online"]["status"] == "ok"


def test_online_category_requires_explicit_online_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))

    code = run_doctor(categories={"online"}, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert "requires --online" in payload["error"]


def test_custom_profile_warns_when_it_shares_default_workspace(tmp_path: Path) -> None:
    config = Config()
    ctx = DoctorContext(
        config_path=tmp_path / "profile" / "config.json",
        data_dir=tmp_path / "profile",
        config=config,
    )

    check_profile_isolation(ctx)

    assert ctx.results[-1].status == Status.WARN
    assert "default profile workspace" in ctx.results[-1].message


def test_safe_fix_does_not_seed_an_external_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    external = tmp_path / "external-project"
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    config = Config()
    config.agents.defaults.workspace = str(external)
    path = home / "config.json"
    path.write_text(json.dumps(convert_to_camel(config.model_dump())), encoding="utf-8")
    path.chmod(0o600)

    code = run_doctor(fix=True, json_output=True)
    capsys.readouterr()

    assert code == 1
    assert not external.exists()


def test_safe_fix_fails_closed_for_symbolic_state_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    home = tmp_path / "linked-home"
    home.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))

    code = run_doctor(fix=True, json_output=True)
    capsys.readouterr()

    assert code == 1
    assert list(target.iterdir()) == []


def test_safe_fix_secures_active_and_backup_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    _write_config(home)
    backup = home / "config.json.bak"
    backup.write_bytes((home / "config.json").read_bytes())
    (home / "config.json").chmod(0o644)
    backup.chmod(0o644)

    code = run_doctor(fix=True, categories={"config"}, json_output=True)
    capsys.readouterr()

    assert code == 0
    assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_windows_service_parser_reads_production_xml_and_vbs(tmp_path: Path) -> None:
    profile = tmp_path / "flowly-profile"
    vbs = tmp_path / "ai.flowly.gateway.vbs"
    vbs.write_text(
        'Set env = sh.Environment("PROCESS")\n'
        f'env.Item("FLOWLY_HOME") = "{profile}"\n'
        'sh.Run """C:\\Program Files\\Flowly\\flowly.exe"" gateway --port 19999", 0, True\n',
        encoding="utf-8",
    )
    xml = tmp_path / "ai.flowly.gateway.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="UTF-16"?>
        <Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <Actions><Exec><Command>wscript.exe</Command>
          <Arguments>""" + f'"{vbs}"' + """</Arguments></Exec></Actions>
        </Task>""",
        encoding="utf-16",
    )

    argv, parsed_home = _service_command_and_home(xml, "Windows")

    assert parsed_home == str(profile)
    assert "gateway" in argv
    assert "19999" in argv


def test_windows_service_parser_supports_startup_fallback(tmp_path: Path) -> None:
    vbs = tmp_path / "ai.flowly.gateway.vbs"
    vbs.write_text(
        'env.Item("FLOWLY_HOME") = "C:\\FlowlyHome"\n'
        'sh.Run "flowly.exe gateway", 0, True\n',
        encoding="utf-8",
    )
    startup = tmp_path / "ai.flowly.gateway.cmd"
    startup.write_text(
        f'@echo off\r\nstart "" wscript.exe "{vbs}"\r\n',
        encoding="utf-8",
    )

    argv, parsed_home = _service_command_and_home(startup, "Windows")

    assert parsed_home == "C:\\FlowlyHome"
    assert argv == ["flowly.exe", "gateway"]
