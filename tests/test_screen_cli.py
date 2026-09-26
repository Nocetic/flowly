import json
from unittest.mock import Mock

import pytest
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


def test_cli_unsupported_platform_never_constructs_runtime(monkeypatch):
    monkeypatch.setattr(screen_cmd.ScreenRuntime, "capabilities", lambda: {"supported": False, "reason": "UNSUPPORTED_HOST", "missing": []})
    monkeypatch.setattr(screen_cmd, "get_flowly_home", lambda: pytest.fail("must not create local screen"))
    for command in ("start", "status", "stop"):
        result = CliRunner().invoke(screen_cmd.screen_app, [command])
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "UNSUPPORTED_HOST"
    assert CliRunner().invoke(screen_cmd.screen_app, ["capabilities"]).exit_code == 0
