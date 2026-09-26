"""Desktop processes get profile-scoped locations and no agent credentials."""

import os
from pathlib import Path

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.supervisor import application_command, application_environment

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux screen subprocess environment")


def test_environment_excludes_secrets_and_scopes_all_application_storage(tmp_path, monkeypatch):
    for key in ("OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "HTTP_PROXY", "HTTPS_PROXY",
                "LD_PRELOAD", "PYTHONPATH", "NODE_OPTIONS", "XDG_DATA_DIRS", "CUSTOM_AGENT_TOKEN"):
        monkeypatch.setenv(key, "synthetic-private-value")
    monkeypatch.setenv("HOME", "/private/agent-home")
    monkeypatch.setenv("PATH", "/private/agent-bin")
    monkeypatch.setenv("DISPLAY", ":999")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/fixture-bus,guid=abc")
    directory, sockets = tmp_path / "profile", tmp_path / "sockets"
    env = application_environment(directory, sockets)
    assert set(env) == {"PATH", "LANG", "LC_ALL", "HOME", "XAUTHORITY", "XDG_RUNTIME_DIR",
                        "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "DBUS_SESSION_BUS_ADDRESS"}
    assert env["PATH"] == os.defpath
    assert "synthetic-private-value" not in str(env)
    assert "/private/agent-" not in str(env)
    assert env["XAUTHORITY"] == str(sockets / "Xauthority")
    assert env["XDG_RUNTIME_DIR"] == str(sockets)
    for name, child in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data")):
        assert Path(env[name]) == directory / child
        assert Path(env[name]).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("address", ["tcp:host=example.invalid", "unix:path=/tmp/bus;tcp:host=example.invalid",
    "unix:path=/tmp/bus\nprivate", "unix:path=" + "x" * 1024])
def test_nonlocal_or_malformed_bus_is_rejected(tmp_path, monkeypatch, address):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        application_environment(tmp_path / "profile", tmp_path / "sockets")


def test_profiles_do_not_share_home_or_application_state(tmp_path, monkeypatch):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    a = application_environment(tmp_path / "a", tmp_path / "socket-a")
    b = application_environment(tmp_path / "b", tmp_path / "socket-b")
    assert "DBUS_SESSION_BUS_ADDRESS" not in a
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        assert a[key] != b[key]


def test_custom_executable_is_resolved_before_sanitizing_path(tmp_path, monkeypatch):
    executable = tmp_path / "Xvnc"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert application_command(["Xvnc", "-nolisten", "tcp"]) == [str(executable), "-nolisten", "tcp"]
    with pytest.raises(ScreenError, match="DEPENDENCIES_MISSING"):
        application_command(["missing-screen-command"])
