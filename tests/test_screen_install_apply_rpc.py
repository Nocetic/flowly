"""Direct installation admission uses pinned identity and durable job receipts."""

import asyncio
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime import install_jobs
from flowly.desktop_runtime.installation_gate import environment_activity
from flowly.gateway import display
from tests.test_screen_display_gateway import display_gateway as gateway_fixture
from tests.test_screen_install_jobs import fixture as job_fixture

display_gateway = gateway_fixture
installation_job = job_fixture


def target(f, profile="default"):
    session = display._resolve(f.gateway, {"profileId": profile})
    return {"profileId": profile, "expectedHostId": session.identity["hostId"],
            "expectedBotId": session.identity["botId"], "operationId": "1" * 32,
            "planId": "a" * 64}, session


@pytest.mark.parametrize("profile", ["default", "writer"])
async def test_apply_is_durable_pinned_and_secret_free(display_gateway, installation_job, profile):
    f = display_gateway
    request, session = target(f, profile)
    service, _ = installation_job
    service.identity = dict(session.identity)
    session._runtime = None
    reply = await f.rpc("display.install.apply", {**request, "password": "synthetic-password"})
    assert reply["result"]["operationId"] == request["operationId"]
    for _ in range(100):
        result = service.status(request["operationId"])
        if result["state"] not in {"pending", "running"}:
            break
        await asyncio.sleep(.01)
    assert result["state"] == "succeeded"
    assert (await f.rpc("display.install.apply", request))["result"] == result
    install_jobs.install_system.assert_called_once_with("b" * 64, request["operationId"], password="synthetic-password")
    assert b"synthetic-password" not in service.path.read_bytes()
    assert "password" not in str(result)
    assert session._runtime is None


@pytest.mark.parametrize("extra", [{"packages": ["arbitrary"]}, {"command": "arbitrary"}, {"generation": "unused"}])
async def test_apply_rejects_arbitrary_commands_before_job_admission(display_gateway, monkeypatch, extra):
    request, _ = target(display_gateway)
    factory = Mock(side_effect=AssertionError("must not admit"))
    monkeypatch.setattr(display, "ScreenInstallJobs", factory)
    result = await display_gateway.rpc("display.install.apply", {**request, **extra})
    assert result["error"]["code"] == "INVALID_PARAMS"
    factory.assert_not_called()


async def test_apply_cannot_overlap_live_screen(display_gateway, installation_job):
    request, _ = target(display_gateway)
    service, _ = installation_job
    with environment_activity():
        result = await display_gateway.rpc("display.install.apply", request)
    assert result["error"]["code"] == "INSTALL_BUSY"
    assert not service.path.exists()
    install_jobs.install_system.assert_not_called()


@pytest.mark.parametrize("boundary", ["transport", "authentication", "identity"])
async def test_apply_rejects_untrusted_admission(display_gateway, monkeypatch, boundary):
    f = display_gateway
    request, _ = target(f)
    factory = Mock(side_effect=AssertionError("must not admit"))
    monkeypatch.setattr(display, "ScreenInstallJobs", factory)
    if boundary == "transport":
        monkeypatch.setattr(display, "_protected_socket", lambda _: False)
    elif boundary == "authentication":
        monkeypatch.setattr(f.gateway, "_require_auth", False)
    else:
        request["expectedBotId"] = "wrong"
    result = await f.rpc("display.install.apply", request)
    assert result["error"]["code"] == {"transport": "SECURE_TRANSPORT_REQUIRED",
        "authentication": "UNAUTHORIZED", "identity": "PROFILE_IDENTITY_CHANGED"}[boundary]
    factory.assert_not_called()


@pytest.mark.parametrize("key", ["profileId", "expectedHostId", "expectedBotId", "planId", "operationId"])
async def test_apply_requires_complete_target_and_operation(display_gateway, monkeypatch, key):
    request, _ = target(display_gateway)
    request.pop(key)
    factory = Mock(side_effect=AssertionError("must not admit"))
    monkeypatch.setattr(display, "ScreenInstallJobs", factory)
    result = await display_gateway.rpc("display.install.apply", request)
    assert result["error"]["code"] == "INVALID_PARAMS"
    factory.assert_not_called()


@pytest.mark.parametrize("password", [123, "bad\npassword", "x" * 4097])
async def test_apply_rejects_invalid_secret_without_receipt(display_gateway, installation_job, password):
    request, _ = target(display_gateway)
    service, _ = installation_job
    result = await display_gateway.rpc("display.install.apply", {**request, "password": password})
    assert result["error"]["code"] == "INVALID_PARAMS"
    assert not service.path.exists()
    install_jobs.install_system.assert_not_called()


async def test_accepted_installation_survives_websocket_disconnect(display_gateway, installation_job):
    import threading

    f = display_gateway
    request, _ = target(f)
    entered, release = threading.Event(), threading.Event()
    def system(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"state": "succeeded", "error": None}
    install_jobs.install_system.side_effect = system
    try:
        accepted = await f.rpc("display.install.apply", request)
        assert accepted["result"]["state"] in {"pending", "running"}
        assert await asyncio.to_thread(entered.wait, 2)
        await f.socket.close()
    finally:
        release.set()
    async with f.client.post(f.server.make_url("/api/auth/ws-ticket"),
                             headers={"Authorization": "Bearer display-test-owner"}) as response:
        ticket = (await response.json())["ticket"]
    async with f.client.ws_connect(f.server.make_url("/ws?ticket=" + ticket)) as ws:
        for _ in range(100):
            await ws.send_json({"type": "rpc", "id": "status", "method": "display.install.status",
                                "params": {k: v for k, v in request.items() if k != "planId"}})
            result = (await ws.receive_json(timeout=5))["result"]
            if result["state"] not in {"pending", "running"}:
                break
            await asyncio.sleep(.01)
        assert result["state"] == "succeeded"
    install_jobs.install_system.assert_called_once()
    install_jobs.install_python.assert_called_once()
