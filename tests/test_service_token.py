"""The gateway's token lives in config.json only, and can be replaced in one step.

`flowly service install --host 0.0.0.0` used to put `--token <token>` on the
service's command line: readable by anyone on the machine (ps, the plist in
~/Library/LaunchAgents), and winning over config.json, so a token changed in
config came back on the next start. `flowly gateway --rotate-token` could not
help a service either: it starts a second gateway on the busy port.
"""

from __future__ import annotations

import io
import json
import plistlib
import subprocess

import pytest
import typer
from rich.console import Console

from flowly.cli import service_cmd
from flowly.gateway import remote_info
from flowly.gateway.tls import ensure_identity, short_fingerprint

LABEL = "ai.flowly.gateway"
PORT = 19877
OLD = "old-token-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"gateway": {"host": "0.0.0.0", "port": PORT, "token": OLD}}))
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    stream = io.StringIO()
    monkeypatch.setattr(service_cmd, "console", Console(file=stream, width=400, color_system=None))
    monkeypatch.setattr(service_cmd, "_provider_configured", lambda: True)
    monkeypatch.setattr(service_cmd, "_resolve_flowly_exec_argv", lambda: ["/opt/flowly/bin/flowly"])
    monkeypatch.setattr(service_cmd, "_get_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(service_cmd, "_detect_public_ip", lambda: "")
    monkeypatch.setattr(service_cmd, "_ensure_linger_linux", lambda: None)
    monkeypatch.setattr(remote_info, "detect_lan_ip", lambda: "203.0.113.7")
    commands: list[list[str]] = []
    monkeypatch.setattr(service_cmd, "_run_cmd", lambda args, check=True: commands.append(args)
                        or subprocess.CompletedProcess(args, 0, "", ""))
    return {"tmp": tmp_path, "home": home, "out": stream, "commands": commands}


def _config_token(env) -> str:
    return json.loads((env["home"] / "config.json").read_text())["gateway"]["token"]


def _install(**overrides):
    args = dict(label=LABEL, port=PORT, verbose=False, start=False, force=False, persona="",
                cwd="", host="0.0.0.0", remote=False, token="")
    args.update(overrides)
    service_cmd.service_install(**args)


def test_a_linux_install_keeps_the_token_off_the_command_line(env, monkeypatch):
    unit = env["tmp"] / "systemd" / f"{LABEL}.service"
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service_cmd, "_service_paths", lambda _label: (None, unit, None))
    _install(token="chosen-token-BBBB")
    assert "chosen-token-BBBB" not in unit.read_text()
    assert "--token" not in unit.read_text()
    assert "--host 0.0.0.0" in unit.read_text()
    assert _config_token(env) == "chosen-token-BBBB"
    # Shown once to the person installing it, with the key.
    out = env["out"].getvalue()
    assert "chosen-token-BBBB" in out
    assert short_fingerprint(ensure_identity(directory=env["home"] / "gateway" / "tls").pin) in out


def test_a_macos_install_keeps_the_token_off_the_plist(env, monkeypatch):
    plist = env["tmp"] / "LaunchAgents" / f"{LABEL}.plist"
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service_cmd, "_service_paths", lambda _label: (plist, None, None))
    monkeypatch.setattr(service_cmd, "_launchd_loaded", lambda *_a, **_k: False)
    monkeypatch.setattr(service_cmd, "_launchd_domain", lambda _label: "gui/501")
    _install()
    assert OLD not in plist.read_text()
    assert "--token" not in plistlib.loads(plist.read_bytes())["ProgramArguments"]
    assert _config_token(env) == OLD


def test_a_baked_token_comes_off_every_kind_of_service(env, monkeypatch):
    tmp = env["tmp"]
    plist = tmp / "a.plist"
    plist.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": [
        "/opt/flowly", "gateway", "--host", "0.0.0.0", "--token", OLD, "--verbose"]}))
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service_cmd, "_service_paths", lambda _label: (plist, None, None))
    assert service_cmd._drop_baked_token(LABEL) is True
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [
        "/opt/flowly", "gateway", "--host", "0.0.0.0", "--verbose"]
    assert service_cmd._drop_baked_token(LABEL) is False

    unit = tmp / "a.service"
    unit.write_text(f"[Service]\nExecStart=/opt/flowly gateway --token={OLD} --host 0.0.0.0\nRestart=always\n")
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service_cmd, "_service_paths", lambda _label: (None, unit, None))
    assert service_cmd._drop_baked_token(LABEL) is True
    assert unit.read_text() == "[Service]\nExecStart=/opt/flowly gateway --host 0.0.0.0\nRestart=always\n"

    xml = tmp / "win" / f"{LABEL}.xml"
    xml.parent.mkdir()
    vbs = xml.parent / f"{LABEL}.vbs"
    command = subprocess.list2cmdline([r"C:\Program Files\flowly\flowly.exe", "gateway", "--token", "a b\"c", "--port", "1"])
    vbs.write_text('Do\r\n  sh.Run "' + command.replace('"', '""') + '", 0, True\r\nLoop\r\n', encoding="utf-8")
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Windows")
    monkeypatch.setattr(service_cmd, "_service_paths", lambda _label: (None, None, xml))
    assert service_cmd._drop_baked_token(LABEL) is True
    expected = subprocess.list2cmdline([r"C:\Program Files\flowly\flowly.exe", "gateway", "--port", "1"])
    assert vbs.read_bytes().decode("utf-8") == 'Do\r\n  sh.Run "' + expected.replace('"', '""') + '", 0, True\r\nLoop\r\n'


def _linux_service(env, monkeypatch, *, running: bool):
    unit = env["tmp"] / f"{LABEL}.service"
    unit.write_text(f"[Service]\nExecStart=/opt/flowly gateway --port {PORT} --host 0.0.0.0 --token {OLD}\n")
    monkeypatch.setattr(service_cmd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service_cmd, "_service_paths", lambda _label: (None, unit, None))
    monkeypatch.setattr(service_cmd, "_port_listener_pids", lambda _port: [4242] if running else [])
    return unit


def test_rotate_token_stops_the_service_changes_it_and_starts_it_with_only_the_new_token(env, monkeypatch):
    unit = _linux_service(env, monkeypatch, running=True)
    events = []
    monkeypatch.setattr(service_cmd, "service_stop", lambda label: events.append(("stop", _config_token(env))))
    monkeypatch.setattr(service_cmd, "service_start", lambda label: events.append(("start", _config_token(env), unit.read_text())))

    service_cmd.service_rotate_token(label=LABEL, new_key=False)

    token = _config_token(env)
    assert token != OLD and len(token) >= 43
    # Stopped while the old token was still in config: nothing running writes it back.
    assert events[0] == ("stop", OLD)
    assert events[1][0] == "start" and events[1][1] == token
    assert "--token" not in events[1][2] and OLD not in events[1][2]
    assert ["systemctl", "--user", "daemon-reload"] in env["commands"]
    out = env["out"].getvalue()
    assert token in out and "flowly://remote?" in out
    assert short_fingerprint(ensure_identity(directory=env["home"] / "gateway" / "tls").pin) in out


def test_rotate_token_leaves_a_stopped_service_stopped(env, monkeypatch):
    unit = _linux_service(env, monkeypatch, running=False)
    events = []
    monkeypatch.setattr(service_cmd, "service_stop", lambda label: events.append("stop"))
    monkeypatch.setattr(service_cmd, "service_start", lambda label: events.append("start"))
    service_cmd.service_rotate_token(label=LABEL, new_key=False)
    assert events == []
    assert _config_token(env) != OLD
    assert "--token" not in unit.read_text()


def test_rotate_token_fails_loudly_when_the_gateway_comes_back_with_another_token(env, monkeypatch):
    _linux_service(env, monkeypatch, running=True)
    monkeypatch.setattr(service_cmd, "_drop_baked_token", lambda _label: False)

    def start_with_old(label):  # a definition this could not clean, starting with its own token
        data = json.loads((env["home"] / "config.json").read_text())
        data["gateway"]["token"] = OLD
        (env["home"] / "config.json").write_text(json.dumps(data))

    monkeypatch.setattr(service_cmd, "service_stop", lambda label: None)
    monkeypatch.setattr(service_cmd, "service_start", start_with_old)
    with pytest.raises(typer.Exit):
        service_cmd.service_rotate_token(label=LABEL, new_key=False)
    assert "another token" in env["out"].getvalue()


def test_rotate_token_with_a_new_key_changes_the_pin(env, monkeypatch):
    _linux_service(env, monkeypatch, running=False)
    tls = env["home"] / "gateway" / "tls"
    before = ensure_identity(directory=tls).pin
    service_cmd.service_rotate_token(label=LABEL, new_key=True)
    after = ensure_identity(directory=tls).pin
    assert after != before
    assert short_fingerprint(after) in env["out"].getvalue()
