"""Authenticated lifecycle admission for the primary and named profile."""

import asyncio
import os
from unittest.mock import ANY, Mock

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.gateway.display import _resolve
from tests.test_screen_display_gateway import display_gateway as gateway_fixture

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix display fixture")
display_gateway = gateway_fixture


async def terminal(f, target, operation):
    async with asyncio.timeout(3):
        while True:
            response = await f.rpc("display.operation.status", {**target, "operationId": operation})
            result = response["result"]
            if result["state"] not in {"pending", "running"}:
                return result
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("profile", ["default", "writer"])
async def test_profile_lifecycle_requires_identity_and_preserves_control(display_gateway, monkeypatch, profile):
    f = display_gateway
    monkeypatch.setattr("flowly.desktop_runtime.lifecycle.process_identity", lambda _: {"pid": 42, "start": "1", "boot": "boot"})
    monkeypatch.setattr("flowly.desktop_runtime.lifecycle.is_alive", lambda _: True)
    session = _resolve(f.gateway, {"profileId": profile})
    runtime = session.runtime
    runtime.directory = session.directory
    state = dict(runtime.status())
    runtime.status = lambda: dict(state)
    def stop(generation, *, identity_guard):
        identity_guard()
        runtime.authority.stop(generation)
        state["phase"] = "stopped"
    def start(*, expected_generation, identity_guard):
        identity_guard()
        if state["generation"] != expected_generation:
            raise ScreenError("STALE_GENERATION")
        runtime.authority.start("new-generation")
        state.update(phase="running", generation="new-generation")
        return dict(state)
    runtime.stop, runtime.start = Mock(side_effect=stop), Mock(side_effect=start)
    status = (await f.rpc("display.status", {"profileId": profile}))["result"]
    assert status["lifecycle"] and status["lifecycleVersion"] == 1
    target = {"profileId": profile, "expectedHostId": status["hostId"], "expectedBotId": status["botId"]}
    request = {**target, "generation": status["generation"], "operationId": "stop"}
    for changed in ({"expectedBotId": "other"}, {"expectedHostId": "other"}):
        response = await f.rpc("display.stop", {**request, **changed})
        assert response["error"]["code"] == "PROFILE_IDENTITY_CHANGED"
    assert (await f.rpc("display.start", {**request, "command": "arbitrary"}))["error"]["code"] == "INVALID_PARAMS"
    assert (await f.rpc("display.operation.status", request))["error"]["code"] == "INVALID_PARAMS"
    runtime.stop.assert_not_called()
    held = runtime.authority.acquire("holder", status["generation"], 1)
    await f.rpc("display.stop", request)
    assert (await terminal(f, target, "stop"))["error"] == "HUMAN_HAS_CONTROL"
    assert runtime.authority.snapshot()["control"] == "human"
    runtime.authority.release("holder", status["generation"], held["epoch"])
    await f.rpc("display.stop", {**request, "operationId": "released-stop"})
    assert (await terminal(f, target, "released-stop"))["state"] == "succeeded"
    start_request = {**request, "operationId": "start"}
    await f.rpc("display.start", start_request)
    started = await terminal(f, target, "start")
    assert started["state"] == "succeeded" and started["resultGeneration"] == "new-generation"
    assert (await f.rpc("display.start", start_request))["result"] == started
    runtime.start.assert_called_once_with(expected_generation=status["generation"], identity_guard=ANY)
    other = _resolve(f.gateway, {"profileId": "writer" if profile == "default" else "default"})
    assert other.runtime.status()["generation"] != "new-generation"
    assert "owner" not in started and "directory" not in started


async def test_status_reports_stop_fence_before_process_shutdown(display_gateway):
    f = display_gateway
    session = _resolve(f.gateway, {"profileId": "default"})
    session.runtime.authority.stop("generation-default")
    status = (await f.rpc("display.status", {}))["result"]
    assert status["phase"] == "stopping"
    assert status["input"] is False
    assert status["control"]["running"] is False
