"""Installation review is authenticated, pinned and incapable of executing work."""

import os
import weakref
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.desktop_runtime import install_plan as plans
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.gateway import display
from tests.test_screen_display_gateway import display_gateway as gateway_fixture
from tests.test_screen_install_jobs import finished
from tests.test_screen_install_jobs import fixture as install_job_fixture

display_gateway = gateway_fixture
installation_job = install_job_fixture


def test_combined_plan_binds_identity_and_both_recipes(monkeypatch):
    identity = {"hostId": "host", "botId": "bot", "profileId": "default", "screenId": "browser"}
    system = {"planId": "a" * 64, "runtimeAccountRequired": True, "supported": True, "sudoAvailable": False}
    python = {"planId": "b" * 64, "upToDate": False, "supported": True}
    monkeypatch.setattr(plans, "install_plan", lambda: dict(system))
    monkeypatch.setattr(plans, "python_plan", lambda: dict(python))
    original = plans.installation_plan(identity)
    assert original["applySupported"] and original["reason"] is None
    assert original["runtimeAccountRequired"]
    python["upToDate"] = True
    assert plans.installation_plan(identity)["planId"] == original["planId"]
    for key in ("hostId", "botId", "profileId"):
        assert plans.installation_plan({**identity, key: "other"})["planId"] != original["planId"]
    system["planId"] = "c" * 64
    assert plans.installation_plan(identity)["planId"] != original["planId"]
    system["planId"] = "a" * 64
    python["planId"] = "d" * 64
    assert plans.installation_plan(identity)["planId"] != original["planId"]


@pytest.mark.parametrize("root,sudo,system_ok,python_ok,admissible", [
    (True, False, True, True, True),     # Root: the job provisions the runtime account.
    (False, True, True, True, True),     # Unprivileged with sudo: per-operation password.
    (False, False, True, True, False),   # No privileged path at all.
    (True, True, False, True, False),    # Unsupported distribution/tools.
    (True, True, True, False, False),    # Python environment cannot take the extra.
])
def test_apply_admission_is_live_and_outside_the_fingerprint(monkeypatch, root, sudo, system_ok, python_ok, admissible):
    identity = {"hostId": "host", "botId": "bot", "profileId": "default", "screenId": "browser"}
    system = {"planId": "a" * 64, "runtimeAccountRequired": root, "supported": system_ok, "sudoAvailable": sudo}
    python = {"planId": "b" * 64, "upToDate": False, "supported": python_ok}
    monkeypatch.setattr(plans, "install_plan", lambda: dict(system))
    monkeypatch.setattr(plans, "python_plan", lambda: dict(python))
    plan = plans.installation_plan(identity)
    assert plan["applySupported"] is admissible
    # Older Desktop parsers accept exactly this reason for a read-only plan.
    assert plan["reason"] == (None if admissible else "INSTALL_ADMISSION_UNAVAILABLE")
    monkeypatch.setattr(plans, "install_plan", lambda: {**system, "sudoAvailable": not sudo})
    assert plans.installation_plan(identity)["planId"] == plan["planId"]


@pytest.mark.parametrize("profile", ["default", "writer"])
async def test_install_plan_requires_exact_pin_without_runtime_creation(display_gateway, monkeypatch, profile):
    f = display_gateway
    session = display._resolve(f.gateway, {"profileId": profile})
    identity = dict(session.identity)
    session._runtime = None
    monkeypatch.setattr(ScreenRuntime, "__init__", lambda *a, **kw: pytest.fail("read-only install plan"))
    inspect = Mock(return_value={**identity, "installPlanVersion": 1, "applySupported": False})
    monkeypatch.setattr(display, "installation_plan", inspect)
    target = {"profileId": profile, "expectedHostId": identity["hostId"], "expectedBotId": identity["botId"]}
    for params in ({"profileId": profile}, {**target, "expectedBotId": "wrong"},
                   {**target, "password": "must-not-be-accepted"}, {**target, "generation": "unused"},
                   {**target, "packages": ["arbitrary"]}):
        assert "error" in await f.rpc("display.install.plan", params)
    inspect.assert_not_called()
    assert (await f.rpc("display.install.plan", target))["result"] == {**inspect.return_value, "selfRestart": False}
    inspect.assert_called_once_with(identity)
    assert session._runtime is None


async def test_install_plan_rechecks_identity_before_reply(display_gateway, monkeypatch):
    f = display_gateway
    session = display._resolve(f.gateway, {"profileId": "default"})
    target = {"profileId": "default", "expectedHostId": session.identity["hostId"], "expectedBotId": session.identity["botId"]}
    def changed(*args, **kwargs):
        raise ScreenError("PROFILE_IDENTITY_CHANGED")
    def inspect(identity):
        monkeypatch.setattr(display, "_resolve", changed)
        return {"applySupported": False}
    monkeypatch.setattr(display, "installation_plan", inspect)
    assert (await f.rpc("display.install.plan", target))["error"]["code"] == "PROFILE_IDENTITY_CHANGED"


@pytest.mark.parametrize("boundary", ["transport", "authentication"])
async def test_install_plan_rejects_unprotected_admission(display_gateway, monkeypatch, boundary):
    f = display_gateway
    inspect = Mock(side_effect=AssertionError("must not inspect"))
    monkeypatch.setattr(display, "installation_plan", inspect)
    if boundary == "transport":
        monkeypatch.setattr(display, "_protected_socket", lambda request: False)
    else:
        # A socket that never presented the token, e.g. an exempt loopback client.
        monkeypatch.setattr(f.gateway, "_display_authenticated", weakref.WeakSet())
    result = await f.rpc("display.install.plan", {"profileId": "default"})
    assert result["error"]["code"] == ("SECURE_TRANSPORT_REQUIRED" if boundary == "transport" else "UNAUTHORIZED")
    inspect.assert_not_called()


@pytest.mark.parametrize("profile", ["default", "writer"])
async def test_install_status_is_pinned_read_only_and_rejects_extra_fields(display_gateway, monkeypatch, profile):
    f = display_gateway
    session = display._resolve(f.gateway, {"profileId": profile})
    identity = dict(session.identity)
    session._runtime = None
    monkeypatch.setattr(ScreenRuntime, "__init__", lambda *a, **kw: pytest.fail("status must not create runtime"))
    jobs = Mock()
    jobs.status.return_value = {**identity, "operationId": "1" * 32, "state": "running"}
    factory = Mock(return_value=jobs)
    monkeypatch.setattr(display, "ScreenInstallJobs", factory)
    target = {"profileId": profile, "expectedHostId": identity["hostId"], "expectedBotId": identity["botId"],
              "operationId": "1" * 32}
    for params in ({"profileId": profile, "operationId": "1" * 32},
                   {**target, "expectedBotId": "other"}, {**target, "password": "private"},
                   {**target, "generation": "unused"}):
        assert "error" in await f.rpc("display.install.status", params)
    factory.assert_not_called()
    result = await f.rpc("display.install.status", target)
    assert result["result"] == jobs.status.return_value
    assert factory.call_args.args[0] == identity
    jobs.status.assert_called_once_with("1" * 32)
    assert session._runtime is None


async def test_install_status_rechecks_target_after_receipt_read(display_gateway, monkeypatch):
    f = display_gateway
    session = display._resolve(f.gateway, {"profileId": "default"})
    target = {"profileId": "default", "expectedHostId": session.identity["hostId"],
              "expectedBotId": session.identity["botId"], "operationId": "1" * 32}
    def read(_):
        monkeypatch.setattr(display, "_resolve", Mock(side_effect=ScreenError("PROFILE_IDENTITY_CHANGED")))
        return {"state": "succeeded"}
    monkeypatch.setattr(display, "ScreenInstallJobs", Mock(return_value=Mock(status=read)))
    result = await f.rpc("display.install.status", target)
    assert result["error"]["code"] == "PROFILE_IDENTITY_CHANGED"


@pytest.mark.parametrize("boundary", ["transport", "authentication"])
async def test_install_status_rejects_unprotected_read(display_gateway, monkeypatch, boundary):
    f = display_gateway
    factory = Mock(side_effect=AssertionError("must not read receipt"))
    monkeypatch.setattr(display, "ScreenInstallJobs", factory)
    if boundary == "transport":
        monkeypatch.setattr(display, "_protected_socket", lambda request: False)
    else:
        # A socket that never presented the token, e.g. an exempt loopback client.
        monkeypatch.setattr(f.gateway, "_display_authenticated", weakref.WeakSet())
    result = await f.rpc("display.install.status", {"profileId": "default"})
    assert result["error"]["code"] == ("SECURE_TRANSPORT_REQUIRED" if boundary == "transport" else "UNAUTHORIZED")
    factory.assert_not_called()


async def test_install_status_reads_durable_job_without_replaying_worker(display_gateway, installation_job):
    f = display_gateway
    service, plan = installation_job
    session = display._resolve(f.gateway, {"profileId": "default"})
    service.identity = dict(session.identity)
    operation = "1" * 32
    service.submit(plan["planId"], operation)
    expected = finished(service)
    target = {"profileId": "default", "expectedHostId": session.identity["hostId"],
              "expectedBotId": session.identity["botId"], "operationId": operation}
    for _ in range(2):
        assert (await f.rpc("display.install.status", target))["result"] == expected
    from flowly.desktop_runtime import install_jobs
    install_jobs.install_system.assert_called_once()
    install_jobs.install_python.assert_called_once()
    result = await f.rpc("display.install.status", {**target, "operationId": "invalid"})
    assert result["error"]["code"] == "INVALID_PARAMS"
    result = await f.rpc("display.install.status", {**target, "operationId": "2" * 32})
    assert result["error"]["code"] == "OPERATION_NOT_FOUND"

    other = display._resolve(f.gateway, {"profileId": "writer"})
    result = await f.rpc("display.install.status", {
        "profileId": "writer", "expectedHostId": other.identity["hostId"],
        "expectedBotId": other.identity["botId"], "operationId": operation,
    })
    assert result["error"]["code"] == "PROFILE_IDENTITY_CHANGED"


@pytest.mark.parametrize("owned", [True, False])
async def test_install_plan_says_whether_the_gateway_restarts_itself(display_gateway, monkeypatch, owned):
    # The same proof display.install.restart requires before scheduling one.
    f = display_gateway
    monkeypatch.setattr("flowly.gateway.display.installation_plan", Mock(return_value={"planId": "a" * 64}))
    monkeypatch.setattr("flowly.gateway.display.sys.platform", "linux")
    ownership = AsyncMock(return_value=owned)
    monkeypatch.setattr("flowly.integrations.service_ownership.owns_current_gateway", ownership)
    status = (await f.rpc("display.status", {"profileId": "default"}))["result"]
    target = {"profileId": "default", "expectedHostId": status["hostId"], "expectedBotId": status["botId"]}
    assert (await f.rpc("display.install.plan", target))["result"]["selfRestart"] is owned
    assert ownership.await_args.args[1] == os.getpid()
