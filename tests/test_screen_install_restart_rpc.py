"""Installation restart: idle admission, systemd ownership, lease held until exit."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.desktop_runtime import install_jobs
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.installation_gate import environment_activity
from flowly.desktop_runtime.relay_rpc import RelayScreenRpc
from flowly.gateway import display
from tests.test_screen_display_gateway import display_gateway as gateway_fixture
from tests.test_screen_install_jobs import OP, PLAN, finished
from tests.test_screen_install_jobs import fixture as job_fixture

display_gateway = gateway_fixture
installation_job = job_fixture


def fresh_process(monkeypatch):
    monkeypatch.setattr(install_jobs, "process_identity", lambda _: {"pid": 43, "start": "200", "boot": "boot"})


def assert_exclusive_held():
    with pytest.raises(ScreenError, match="INSTALL_BUSY"):
        with environment_activity():
            pass


def assert_released():
    with environment_activity():
        pass


def test_reservation_holds_the_exclusive_lease_only_while_restart_is_needed(installation_job, monkeypatch):
    service, _ = installation_job
    service.submit(PLAN, OP)
    assert finished(service)["restartRequired"]
    needed, reservation = service.restart_reservation(PLAN, OP)
    assert needed
    assert_exclusive_held()
    reservation.__exit__(None, None, None)
    assert_released()
    fresh_process(monkeypatch)
    assert service.restart_reservation(PLAN, OP) == (False, None)
    assert_released()


def test_reservation_refuses_a_live_turn_and_unfinished_or_foreign_receipts(installation_job, monkeypatch):
    service, _ = installation_job
    with pytest.raises(ScreenError, match="OPERATION_NOT_FOUND"):
        service.restart_reservation(PLAN, OP)
    service.submit(PLAN, OP)
    finished(service)
    with pytest.raises(ScreenError, match="OPERATION_CONFLICT"):
        service.restart_reservation("f" * 64, OP)
    with environment_activity():  # An agent turn or live screen.
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            service.restart_reservation(PLAN, OP)
    assert_released()
    service.guard.side_effect = ScreenError("PROFILE_IDENTITY_CHANGED")
    with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
        service.restart_reservation(PLAN, OP)
    assert_released()


@pytest.mark.parametrize("worker,job,refusal", [
    ({"state": "failed", "error": "INSTALL_INDEX_FAILED"}, "failed", "INSTALL_VERIFICATION_FAILED"),
    ({"state": "recovery_required", "error": "INSTALL_PACKAGES_FAILED"}, "recovery_required", "INSTALL_RECOVERY_REQUIRED"),
])
def test_failed_or_uncertain_jobs_never_admit_a_restart(installation_job, worker, job, refusal):
    service, _ = installation_job
    install_jobs.install_system.return_value = worker
    service.submit(PLAN, OP)
    assert finished(service)["state"] == job
    with pytest.raises(ScreenError, match=refusal):
        service.restart_reservation(PLAN, OP)
    assert_released()


def rpc_target(f):
    session = display._resolve(f.gateway, {"profileId": "default"})
    return {"profileId": "default", "expectedHostId": session.identity["hostId"],
            "expectedBotId": session.identity["botId"], "operationId": OP, "planId": PLAN}, session


@pytest.fixture
def restart(display_gateway, installation_job, monkeypatch):
    request, session = rpc_target(display_gateway)
    service, _ = installation_job
    service.identity = dict(session.identity)
    service.submit(PLAN, OP)
    finished(service)
    display_gateway.gateway._maintenance_backlog = lambda: 0
    owns = AsyncMock(return_value=True)
    outcome = SimpleNamespace(ok=True, detail="")
    restart_gateway = AsyncMock(return_value=outcome)
    monkeypatch.setattr("flowly.integrations.service_ownership.owns_current_gateway", owns)
    monkeypatch.setattr("flowly.integrations.service_control.restart_gateway", restart_gateway)
    monkeypatch.setattr(display.sys, "platform", "linux")
    monkeypatch.setattr(display.asyncio, "sleep", AsyncMock())
    return SimpleNamespace(f=display_gateway, request=request, owns=owns, restart_gateway=restart_gateway, outcome=outcome)


async def test_scheduled_restart_replies_first_and_holds_the_lease_through_restart(restart):
    held = []
    restart.restart_gateway.side_effect = lambda **_: held.append(_lease_is_exclusive()) or restart.outcome
    reply = await restart.f.rpc("display.install.restart", restart.request)
    assert reply["result"]["restart"] == "scheduled"
    assert reply["result"]["operationId"] == OP and "password" not in str(reply)
    await restart.f.gateway._install_restart
    restart.owns.assert_awaited_once()
    assert restart.restart_gateway.await_args.kwargs["expected_pid"] > 0
    assert held == [True], "no turn or runtime may start between admission and restart"
    assert_released()  # Only because this test process survived the restart.


def _lease_is_exclusive():
    try:
        with environment_activity():
            return False
    except ScreenError:
        return True


async def test_unowned_service_is_left_to_the_host_owner(restart):
    restart.owns.return_value = False
    reply = await restart.f.rpc("display.install.restart", restart.request)
    assert reply["result"]["restart"] == "manual"
    restart.restart_gateway.assert_not_called()
    assert_released()


@pytest.mark.parametrize("backlog", [lambda: 1, None])
async def test_queued_messages_or_unknown_backlog_refuse_the_restart(restart, backlog):
    if backlog is None:
        del restart.f.gateway._maintenance_backlog
    else:
        restart.f.gateway._maintenance_backlog = backlog
    reply = await restart.f.rpc("display.install.restart", restart.request)
    assert reply["error"]["code"] == "INSTALL_BUSY"
    restart.owns.assert_not_called()
    assert_released()


async def test_restart_after_a_fresh_process_is_not_required(restart, monkeypatch):
    fresh_process(monkeypatch)
    reply = await restart.f.rpc("display.install.restart", restart.request)
    assert reply["result"]["restart"] == "not_required"
    restart.owns.assert_not_called()


async def test_failed_restart_releases_the_lease_in_the_surviving_process(restart):
    restart.outcome.ok = False
    reply = await restart.f.rpc("display.install.restart", restart.request)
    assert reply["result"]["restart"] == "scheduled"
    await restart.f.gateway._install_restart
    assert_released()


@pytest.mark.parametrize("extra", [{"generation": "unused"}, {"password": "unused"}, {"command": "arbitrary"}])
async def test_restart_accepts_only_the_pinned_closed_shape(restart, monkeypatch, extra):
    factory = Mock(side_effect=AssertionError("must not admit"))
    monkeypatch.setattr(display, "ScreenInstallJobs", factory)
    reply = await restart.f.rpc("display.install.restart", {**restart.request, **extra})
    assert reply["error"]["code"] == "INVALID_PARAMS"
    factory.assert_not_called()


async def test_relay_bridge_never_forwards_an_installation_password():
    sent = []

    async def send(frame):
        sent.append(frame)

    manager = RelayScreenRpc(gateway=object(), current=lambda: True, send=send)
    manager.enabled = True
    manager.connected("browser")
    await manager.submit("browser", {"type": "rpc", "id": "1", "method": "display.install.apply",
                                     "params": {"password": "sudo-secret", "planId": PLAN, "operationId": OP}})
    await asyncio.sleep(0)
    assert len(sent) == 1 and "sudo-secret" not in str(sent)
    assert sent[0]["message"]["error"]["code"] == "SECURE_TRANSPORT_REQUIRED"
    assert manager.clients["browser"].task is None
