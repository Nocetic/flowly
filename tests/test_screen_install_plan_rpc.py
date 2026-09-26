"""Installation review is authenticated, pinned and incapable of executing work."""

from unittest.mock import Mock

import pytest

from flowly.desktop_runtime import install_plan as plans
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.gateway import display
from tests.test_screen_display_gateway import display_gateway as gateway_fixture

display_gateway = gateway_fixture


def test_combined_plan_binds_identity_and_both_recipes(monkeypatch):
    identity = {"hostId": "host", "botId": "bot", "profileId": "default", "screenId": "browser"}
    system = {"planId": "a" * 64, "runtimeAccountRequired": True}
    python = {"planId": "b" * 64, "upToDate": False}
    monkeypatch.setattr(plans, "install_plan", lambda: dict(system))
    monkeypatch.setattr(plans, "python_plan", lambda: dict(python))
    original = plans.installation_plan(identity)
    assert not original["applySupported"]
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
    assert (await f.rpc("display.install.plan", target))["result"] == inspect.return_value
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
        monkeypatch.setattr(f.gateway, "_require_auth", False)
    result = await f.rpc("display.install.plan", {"profileId": "default"})
    assert result["error"]["code"] == ("SECURE_TRANSPORT_REQUIRED" if boundary == "transport" else "UNAUTHORIZED")
    inspect.assert_not_called()
