"""Restarting the main agent from an app (a phone, say)."""

from __future__ import annotations

import os

import pytest

import flowly.integrations.service_control as service_control
from flowly.channels import feature_rpc


def _runner(outputs: dict[str, tuple[int, str]]):
    calls: list[list[str]] = []

    async def run(cmd: list[str]) -> tuple[int, str, str]:
        calls.append(cmd)
        rc, out = outputs.get(cmd[0], (1, ""))
        return rc, out, ""

    return run, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        ((0, f'{{\n\t"PID" = {os.getpid()};\n\t"Label" = "ai.flowly.gateway";\n}};'), True),
        ((0, '{\n\t"PID" = 1;\n\t"Label" = "ai.flowly.gateway";\n};'), False),
        ((0, '{\n\t"LastExitStatus" = 0;\n};'), False),
        ((113, 'Could not find service'), False),
    ],
)
async def test_only_the_macos_service_itself_can_restart(monkeypatch, listing, expected) -> None:
    run, _calls = _runner({"launchctl": listing})
    monkeypatch.setattr(service_control.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service_control, "_run", run)

    assert await service_control.service_runs_this_process() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("main_pid", "expected"),
    [(str(os.getpid()), True), ("4242", False), ("0", False), ("", False)],
)
async def test_only_the_systemd_service_itself_can_restart(monkeypatch, main_pid, expected) -> None:
    run, calls = _runner({"systemctl": (0, main_pid + "\n")})
    monkeypatch.setattr(service_control.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service_control.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(service_control, "_run", run)

    assert await service_control.service_runs_this_process() is expected
    assert calls[0][:4] == ["systemctl", "--user", "show", "-p"]


@pytest.mark.asyncio
async def test_anything_it_cannot_tell_reads_as_no(monkeypatch) -> None:
    async def broken(_cmd):
        raise OSError("no launchctl here")

    monkeypatch.setattr(service_control.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service_control, "_run", broken)

    assert await service_control.service_runs_this_process() is False


@pytest.mark.asyncio
async def test_the_service_restarts_after_saying_so(monkeypatch) -> None:
    async def yes() -> bool:
        return True

    monkeypatch.setattr(service_control, "service_runs_this_process", yes)

    result, needs_restart = await feature_rpc.dispatch("gateway.restart", {})

    assert result == {"ok": True, "willRestart": True}
    assert needs_restart is True


@pytest.mark.asyncio
async def test_a_gateway_started_by_hand_says_why_it_cannot(monkeypatch) -> None:
    async def no() -> bool:
        return False

    monkeypatch.setattr(service_control, "service_runs_this_process", no)

    with pytest.raises(feature_rpc.FeatureRpcError) as raised:
        await feature_rpc.dispatch("gateway.restart", {})
    assert raised.value.code == "RESTART_UNAVAILABLE"


def test_apps_can_see_the_main_agent_offers_restart() -> None:
    assert "gateway.restart" in feature_rpc.system_capabilities()["featureMethods"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kick_rc", "marked"), [(0, True), (1, False)])
async def test_a_restart_marks_the_goodbye_only_when_it_is_really_sent(monkeypatch, kick_rc, marked) -> None:
    seen_during_kick: list[bool] = []

    async def run(cmd: list[str]) -> tuple[int, str, str]:
        if cmd[:2] == ["launchctl", "kickstart"]:
            seen_during_kick.append(service_control.restart_requested())
            return kick_rc, "", ""
        return 0, "", ""

    async def back(*_args) -> bool:
        return True

    monkeypatch.setattr(service_control.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service_control, "_run", run)
    monkeypatch.setattr(service_control, "_wait_for_port", back)
    monkeypatch.setattr(service_control, "_restart_requested", False)

    await service_control.restart_gateway()

    # The process can be signalled before the command returns: it is marked first.
    assert seen_during_kick == [True]
    assert service_control.restart_requested() is marked


@pytest.mark.asyncio
async def test_a_service_that_is_not_installed_leaves_the_goodbye_a_stop(monkeypatch) -> None:
    run, _calls = _runner({"launchctl": (113, "")})
    monkeypatch.setattr(service_control.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service_control, "_run", run)
    monkeypatch.setattr(service_control, "_restart_requested", False)

    await service_control.restart_gateway()

    assert service_control.restart_requested() is False
