import json
from unittest.mock import Mock

import pytest
from filelock import Timeout
from typer.testing import CliRunner

from flowly.cli import screen_cmd
from flowly.desktop_runtime.authority import ScreenError


@pytest.fixture
def runtime(monkeypatch):
    instance = Mock()
    instance.status.return_value = {"phase": "running", "generation": "generation", "error": None,
                                    "socket": "/private/internal.sock", "cdpPort": 12345}
    instance.authority.snapshot.return_value = {"control": "agent"}
    monkeypatch.setattr(screen_cmd, "_runtime", lambda: instance)
    monkeypatch.setattr(screen_cmd, "current_profile_name", lambda: "research")
    return instance


def test_cli_start_targets_selected_profile_and_hides_private_endpoints(runtime):
    result = CliRunner().invoke(screen_cmd.screen_app, ["start"])
    assert result.exit_code == 0
    runtime.start.assert_called_once()
    assert json.loads(result.stdout)["profileId"] == "research"
    assert "internal.sock" not in result.stdout and "12345" not in result.stdout


def test_cli_status_does_not_start_or_stop_runtime(runtime):
    result = CliRunner().invoke(screen_cmd.screen_app, ["status"])
    assert result.exit_code == 0
    runtime.start.assert_not_called()
    runtime.stop.assert_not_called()


def test_cli_stop_uses_exact_current_generation(runtime):
    result = CliRunner().invoke(screen_cmd.screen_app, ["stop"])
    assert result.exit_code == 0
    runtime.stop.assert_called_once_with("generation")


def test_cli_stop_cannot_override_human_ownership(runtime):
    runtime.stop.side_effect = ScreenError("HUMAN_HAS_CONTROL")
    result = CliRunner().invoke(screen_cmd.screen_app, ["stop"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "HUMAN_HAS_CONTROL"}


@pytest.mark.parametrize("action", ["start", "stop"])
def test_cli_lock_timeout_returns_public_busy_error(runtime, action):
    getattr(runtime, action).side_effect = Timeout("/private/account/startup.lock")
    result = CliRunner().invoke(screen_cmd.screen_app, [action])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "LIFECYCLE_BUSY"}


def test_cli_unsupported_platform_never_constructs_runtime(monkeypatch):
    monkeypatch.setattr(screen_cmd.ScreenRuntime, "capabilities", lambda: {"supported": False, "reason": "UNSUPPORTED_HOST", "missing": []})
    monkeypatch.setattr(screen_cmd, "get_flowly_home", lambda: pytest.fail("must not create local screen"))
    for command in ("start", "status", "stop"):
        result = CliRunner().invoke(screen_cmd.screen_app, [command])
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "UNSUPPORTED_HOST"
    assert CliRunner().invoke(screen_cmd.screen_app, ["capabilities"]).exit_code == 0


@pytest.mark.parametrize("ready", [True, False])
def test_cli_doctor_checks_selected_profile_without_runtime(tmp_path, monkeypatch, ready):
    home = tmp_path / "not-created"
    monkeypatch.setattr(screen_cmd, "get_flowly_home", lambda: home)
    monkeypatch.setattr(screen_cmd, "current_profile_name", lambda: "writer")
    monkeypatch.setattr(screen_cmd, "_runtime", lambda: pytest.fail("read-only doctor"))
    doctor = Mock(return_value={"doctorVersion": 1, "ready": ready})
    monkeypatch.setattr(screen_cmd.ScreenRuntime, "doctor", doctor)
    result = CliRunner().invoke(screen_cmd.screen_app, ["doctor"])
    assert result.exit_code == (0 if ready else 1)
    doctor.assert_called_once_with(home / "desktop")
    assert json.loads(result.stdout) == {"profileId": "writer", "doctorVersion": 1, "ready": ready}
    assert not home.exists()


def test_cli_install_plan_and_status_never_prompt_or_install(monkeypatch):
    monkeypatch.setattr(screen_cmd, "install_plan", lambda: {"supported": True, "planId": "a" * 64})
    status = Mock(return_value={"state": "running"})
    monkeypatch.setattr(screen_cmd, "install_status", status)
    monkeypatch.setattr(screen_cmd, "install_system", lambda *a, **kw: pytest.fail("read-only"))
    monkeypatch.setattr(screen_cmd.getpass, "getpass", lambda *a: pytest.fail("no prompt"))
    assert CliRunner().invoke(screen_cmd.screen_app, ["install-plan"]).exit_code == 0
    result = CliRunner().invoke(screen_cmd.screen_app, ["install-status", "--operation-id", "b" * 32])
    assert json.loads(result.stdout) == {"state": "running"}
    status.assert_called_once_with("b" * 32)


def test_cli_install_refuses_stale_plan_before_prompt(monkeypatch):
    monkeypatch.setattr(screen_cmd, "install_plan", lambda: {"supported": True, "planId": "a" * 64})
    monkeypatch.setattr(screen_cmd.getpass, "getpass", lambda *a: pytest.fail("stale plan"))
    result = CliRunner().invoke(screen_cmd.screen_app, ["install-system", "--plan-id", "c" * 64, "--operation-id", "b" * 32, "--ask-sudo"])
    assert result.exit_code == 1 and json.loads(result.stdout)["error"] == "INSTALL_PLAN_CHANGED"


def test_cli_secret_prompt_requires_terminal(monkeypatch):
    monkeypatch.setattr(screen_cmd, "install_plan", lambda: {"supported": True, "planId": "a" * 64})
    monkeypatch.setattr(screen_cmd.getpass, "getpass", lambda *a: pytest.fail("must not echo a secret"))
    result = CliRunner().invoke(screen_cmd.screen_app, ["install-system", "--plan-id", "a" * 64, "--operation-id", "b" * 32, "--ask-sudo"])
    assert result.exit_code == 1 and json.loads(result.stdout)["error"] == "INSTALL_TERMINAL_REQUIRED"


def test_cli_explicit_system_install_passes_only_plan_and_operation(monkeypatch):
    monkeypatch.setattr(screen_cmd, "install_plan", lambda: {"supported": True, "planId": "a" * 64})
    install = Mock(return_value={"state": "succeeded"})
    monkeypatch.setattr(screen_cmd, "install_system", install)
    result = CliRunner().invoke(screen_cmd.screen_app, ["install-system", "--plan-id", "a" * 64, "--operation-id", "b" * 32])
    assert result.exit_code == 0
    install.assert_called_once_with("a" * 64, "b" * 32, password=None)


def test_cli_never_falls_back_to_echoing_sudo_password(monkeypatch):
    import warnings
    monkeypatch.setattr(screen_cmd, "install_plan", lambda: {"supported": True, "planId": "a" * 64})
    def unavailable(*args):
        warnings.warn("terminal echo cannot be disabled", screen_cmd.getpass.GetPassWarning)
        pytest.fail("must not continue to echoed input")
    monkeypatch.setattr(screen_cmd.getpass, "getpass", unavailable)
    # CliRunner replaces sys.stdin; emulate a terminal inside the plan callback.
    def plan():
        monkeypatch.setattr(screen_cmd.sys.stdin, "isatty", lambda: True)
        return {"supported": True, "planId": "a" * 64}
    monkeypatch.setattr(screen_cmd, "install_plan", plan)
    result = CliRunner().invoke(screen_cmd.screen_app, ["install-system", "--plan-id", "a" * 64, "--operation-id", "b" * 32, "--ask-sudo"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "INSTALL_TERMINAL_REQUIRED"}


def test_python_plan_and_receipt_are_readonly(monkeypatch):
    monkeypatch.setattr(screen_cmd, "python_plan", lambda: {"scope": "agent-python"})
    read = Mock(return_value={"state": "running"})
    monkeypatch.setattr(screen_cmd, "python_status", read)
    monkeypatch.setattr(screen_cmd, "install_python", lambda *a: pytest.fail("read-only"))
    assert json.loads(CliRunner().invoke(screen_cmd.screen_app, ["install-python-plan"]).stdout) == {"scope": "agent-python"}
    result = CliRunner().invoke(screen_cmd.screen_app, ["install-python-status", "--operation-id", "b" * 32])
    assert json.loads(result.stdout) == {"state": "running"}
    read.assert_called_once_with("b" * 32)


@pytest.mark.parametrize("state,exit_code", [("succeeded", 0), ("recovery_required", 1)])
def test_python_install_requires_explicit_plan_and_operation(monkeypatch, state, exit_code):
    install = Mock(return_value={"state": state, "restartRequired": True})
    monkeypatch.setattr(screen_cmd, "install_python", install)
    monkeypatch.setattr(screen_cmd.getpass, "getpass", lambda *a: pytest.fail("never sudo"))
    result = CliRunner().invoke(screen_cmd.screen_app, ["install-python", "--plan-id", "a" * 64, "--operation-id", "b" * 32])
    assert result.exit_code == exit_code
    install.assert_called_once_with("a" * 64, "b" * 32)
    assert json.loads(result.stdout)["restartRequired"]
