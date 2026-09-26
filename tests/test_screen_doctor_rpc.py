"""Authenticated, pinned read-only diagnostics, separate from status revisions."""

from unittest.mock import Mock

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.gateway import display
from tests.test_screen_display_gateway import display_gateway as gateway_fixture

display_gateway = gateway_fixture


@pytest.mark.parametrize("profile", ["default", "writer"])
async def test_doctor_is_pinned_and_does_not_construct_runtime(display_gateway, monkeypatch, profile):
    f = display_gateway
    session = display._resolve(f.gateway, {"profileId": profile})
    identity = dict(session.identity)
    session._runtime = None
    monkeypatch.setattr(ScreenRuntime, "__init__", lambda *a, **kw: pytest.fail("read-only doctor"))
    report = {"doctorVersion": 1, "ready": False, "reason": "INSUFFICIENT_RESOURCES",
              "missing": [], "resources": {"ready": False, "reason": "INSUFFICIENT_RESOURCES"}}
    doctor = Mock(return_value=report)
    monkeypatch.setattr(ScreenRuntime, "doctor", doctor)
    target = {"profileId": profile, "expectedHostId": identity["hostId"], "expectedBotId": identity["botId"]}
    for params in ({"profileId": profile}, {**target, "expectedBotId": "wrong"}, {**target, "generation": "arbitrary"}):
        assert "error" in await f.rpc("display.doctor", params)
    doctor.assert_not_called()
    result = await f.rpc("display.doctor", target)
    assert result["result"] == {**identity, **report}
    doctor.assert_called_once_with(session.directory)
    assert session._runtime is None


async def test_doctor_rechecks_identity_before_publishing(display_gateway, monkeypatch):
    f = display_gateway
    session = display._resolve(f.gateway, {"profileId": "default"})
    target = {"profileId": "default", "expectedHostId": session.identity["hostId"], "expectedBotId": session.identity["botId"]}
    def changed(*args, **kwargs):
        raise ScreenError("PROFILE_IDENTITY_CHANGED")
    def inspect(directory):
        monkeypatch.setattr(display, "_resolve", changed)
        return {"ready": True}
    monkeypatch.setattr(ScreenRuntime, "doctor", inspect)
    assert (await f.rpc("display.doctor", target))["error"]["code"] == "PROFILE_IDENTITY_CHANGED"
