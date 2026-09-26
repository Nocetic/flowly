"""Post-install verification observes identity, process age and installed components."""

from unittest.mock import Mock

import pytest

from flowly.desktop_runtime import install_jobs as jobs
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.installation_gate import environment_activity
from flowly.gateway import display
from tests.test_screen_display_gateway import display_gateway as gateway_fixture
from tests.test_screen_install_jobs import OP, PLAN, finished
from tests.test_screen_install_jobs import fixture as job_fixture

installation_job = job_fixture
display_gateway = gateway_fixture


@pytest.fixture
def installed(installation_job, monkeypatch):
    service, _ = installation_job
    service.submit(PLAN, OP)
    assert finished(service)["state"] == "succeeded"
    components = Mock(return_value=True)
    monkeypatch.setattr(jobs, "_installation_components_ready", components)
    return service, components


def test_verification_requires_restart_then_checks_components_without_mutation(installed, monkeypatch):
    service, components = installed
    before = service.path.read_bytes()
    result = service.verify(PLAN, OP)
    assert result["ready"] is False and result["reason"] == "INSTALL_RESTART_REQUIRED"
    components.assert_not_called()
    monkeypatch.setattr(jobs, "process_identity", lambda _: {"pid": 99, "start": "101", "boot": "boot"})
    assert service.verify(PLAN, OP) == {"installVerificationVersion": 1, **service.identity,
                                      "operationId": OP, "planId": PLAN, "ready": True, "reason": None}
    assert service.path.read_bytes() == before
    components.assert_called_once()
    jobs.install_system.assert_called_once()
    jobs.install_python.assert_called_once()


@pytest.mark.parametrize("failure", [False, RuntimeError("private dependency path")])
def test_missing_or_unimportable_components_cannot_verify_success(installed, monkeypatch, failure):
    service, components = installed
    monkeypatch.setattr(jobs, "process_identity", lambda _: {"pid": 99, "start": "101", "boot": "boot"})
    if failure is False:
        components.return_value = False
    else:
        components.side_effect = failure
    result = service.verify(PLAN, OP)
    assert result["ready"] is False and result["reason"] == "INSTALL_VERIFICATION_FAILED"
    assert "private" not in str(result)


def test_busy_environment_blocks_verification(installed):
    service, components = installed
    with environment_activity(exclusive=True):
        result = service.verify(PLAN, OP)
    assert result["ready"] is False and result["reason"] == "INSTALL_BUSY"
    components.assert_not_called()


def test_wrong_identity_or_plan_cannot_verify_another_installation(installed):
    service, components = installed
    with pytest.raises(ScreenError, match="OPERATION_CONFLICT"):
        service.verify("b" * 64, OP)
    other = jobs.ScreenInstallJobs({**service.identity, "botId": "other"}, Mock())
    with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
        other.verify(PLAN, OP)
    components.assert_not_called()


def test_missing_receipt_does_not_create_state(installation_job):
    service, _ = installation_job
    with pytest.raises(ScreenError, match="OPERATION_NOT_FOUND"):
        service.verify(PLAN, OP)
    assert not service.root.exists()
    jobs.install_system.assert_not_called()


def test_target_is_rechecked_after_readiness_observation(installed, monkeypatch):
    service, _ = installed
    monkeypatch.setattr(jobs, "process_identity", lambda _: {"pid": 99, "start": "101", "boot": "boot"})
    service.guard.reset_mock(side_effect=True)
    service.guard.side_effect = [None, ScreenError("PROFILE_IDENTITY_CHANGED")]
    with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
        service.verify(PLAN, OP)


async def test_verify_rpc_uses_durable_receipt_and_rechecks_identity(display_gateway, installation_job, monkeypatch):
    f = display_gateway
    service, _ = installation_job
    session = display._resolve(f.gateway, {"profileId": "default"})
    session._runtime = None
    service.identity = dict(session.identity)
    service.submit(PLAN, OP)
    finished(service)
    request = {"profileId": "default", "expectedHostId": session.identity["hostId"],
               "expectedBotId": session.identity["botId"], "operationId": OP, "planId": PLAN}
    assert (await f.rpc("display.install.verify", request))["result"]["reason"] == "INSTALL_RESTART_REQUIRED"
    monkeypatch.setattr(jobs, "process_identity", lambda _: {"pid": 99, "start": "101", "boot": "boot"})
    monkeypatch.setattr(jobs, "_installation_components_ready", Mock(return_value=True))
    result = (await f.rpc("display.install.verify", request))["result"]
    assert result["ready"] is True and result["reason"] is None
    assert session._runtime is None
    jobs.install_python.assert_called_once()
    def changed():
        monkeypatch.setattr(display, "_resolve", Mock(side_effect=ScreenError("PROFILE_IDENTITY_CHANGED")))
        return True
    monkeypatch.setattr(jobs, "_installation_components_ready", changed)
    assert (await f.rpc("display.install.verify", request))["error"]["code"] == "PROFILE_IDENTITY_CHANGED"


@pytest.mark.parametrize("extra", [{"password": "private"}, {"generation": "unused"}, {"command": "arbitrary"}])
async def test_verify_rpc_rejects_non_observation_fields(display_gateway, monkeypatch, extra):
    factory = Mock(side_effect=AssertionError("must not verify"))
    monkeypatch.setattr(display, "ScreenInstallJobs", factory)
    request = {"profileId": "default", "expectedHostId": "host", "expectedBotId": "bot",
               "operationId": OP, "planId": PLAN, **extra}
    assert (await display_gateway.rpc("display.install.verify", request))["error"]["code"] == "INVALID_PARAMS"
    factory.assert_not_called()


@pytest.mark.parametrize("missing", ["profileId", "expectedHostId", "expectedBotId", "operationId", "planId"])
async def test_verify_rpc_requires_the_complete_target(display_gateway, monkeypatch, missing):
    factory = Mock(side_effect=AssertionError("must not verify"))
    monkeypatch.setattr(display, "ScreenInstallJobs", factory)
    request = {"profileId": "default", "expectedHostId": "host", "expectedBotId": "bot", "operationId": OP, "planId": PLAN}
    del request[missing]
    assert (await display_gateway.rpc("display.install.verify", request))["error"]["code"] == "INVALID_PARAMS"
    factory.assert_not_called()


@pytest.mark.parametrize("boundary", ["transport", "authentication"])
async def test_verify_rpc_rejects_unprotected_connections(display_gateway, monkeypatch, boundary):
    f = display_gateway
    if boundary == "transport":
        monkeypatch.setattr(display, "_protected_socket", lambda _: False)
    else:
        monkeypatch.setattr(f.gateway, "_require_auth", False)
    result = await f.rpc("display.install.verify", {})
    assert result["error"]["code"] == ("SECURE_TRANSPORT_REQUIRED" if boundary == "transport" else "UNAUTHORIZED")


@pytest.mark.parametrize("packages,capable,imports", [(False, True, False), (True, False, False), (True, True, True)])
def test_component_check_requires_versions_programs_and_actual_imports(monkeypatch, packages, capable, imports):
    from flowly.desktop_runtime import python_install_worker
    from flowly.desktop_runtime.launcher import ScreenRuntime
    monkeypatch.setattr(python_install_worker, "ready", lambda: packages)
    monkeypatch.setattr(ScreenRuntime, "capabilities", lambda: {"supported": capable})
    load = Mock()
    monkeypatch.setattr(jobs.importlib, "import_module", load)
    assert jobs._installation_components_ready() is imports
    assert [call.args[0] for call in load.call_args_list] == (
        ["playwright.async_api", "greenlet", "pyee", "typing_extensions"] if imports else [])


@pytest.mark.parametrize("state,reason", [("pending", "INSTALL_BUSY"), ("failed", "INSTALL_VERIFICATION_FAILED"),
                                       ("recovery_required", "INSTALL_RECOVERY_REQUIRED")])
def test_non_success_receipts_never_verify_ready(installed, monkeypatch, state, reason):
    service, components = installed
    receipt = service.status(OP)
    monkeypatch.setattr(service, "status", lambda _: {**receipt, "state": state})
    result = service.verify(PLAN, OP)
    assert result["ready"] is False and result["reason"] == reason
    components.assert_not_called()
