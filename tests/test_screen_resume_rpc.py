"""Authenticated direct resume RPC never infers its target from an active chat."""

import asyncio
import os

import pytest

from flowly.gateway.display import _resolve
from tests.test_screen_display_gateway import control_params, observe
from tests.test_screen_display_gateway import display_gateway as gateway_fixture

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix display fixture")
display_gateway = gateway_fixture


@pytest.mark.parametrize("profile", ["default", "writer"])
async def test_pinned_resume_rpc_requires_valid_viewer_and_explicit_released_checkpoint(display_gateway, profile, monkeypatch):
    f = display_gateway
    owner = {"pid": 42, "start": "1", "boot": "test"}
    monkeypatch.setattr("flowly.desktop_runtime.handoff.process_identity", lambda _: owner)
    monkeypatch.setattr("flowly.desktop_runtime.handoff.is_alive", lambda _: True)
    service = _resolve(f.gateway, {"profileId": profile})
    authority = service.runtime.authority
    generation = authority.snapshot()["generation"]
    authority.claim_run("original-run", generation)
    identity = {key: service.identity[key] for key in ("hostId", "botId", "profileId")}
    execution = service.handoffs.start(identity, "original-session", "original-run", generation)
    viewer, _ = await observe(f, profile)
    held = authority.acquire("human", generation, 1)
    paused = service.handoffs.checkpoint(execution)
    request = {**control_params(viewer, held["epoch"], "resume"), "handoffId": paused["handoffId"]}
    blocked = await f.rpc("display.run.resume", request)
    assert blocked["error"]["code"] == "HUMAN_HAS_CONTROL"
    authority.release("human", generation, held["epoch"])
    request["epoch"] = authority.snapshot()["epoch"]
    for override, code in [
        ({"expectedBotId": "replacement"}, "PROFILE_IDENTITY_CHANGED"),
        ({"viewerToken": "not-a-viewer"}, "VIEWER_EXPIRED"),
        ({"generation": "old"}, "STALE_GENERATION"),
        ({"handoffId": "another-handoff"}, "HANDOFF_CHANGED"),
        ({"originRunId": "different-run"}, "INVALID_PARAMS"),
        ({"originSessionKey": "currently-selected-chat"}, "INVALID_PARAMS"),
    ]:
        denied = await f.rpc("display.run.resume", {**request, **override})
        assert denied["error"]["code"] == code
        assert service.handoffs.status()["state"] == "waiting_resume"
    approved = await f.rpc("display.run.resume", request)
    assert approved["result"]["requested"] is True
    assert approved["result"]["profileId"] == profile
    assert (await f.rpc("display.run.resume", request))["result"] == approved["result"]
    public = (await f.rpc("display.status", {"profileId": profile}))["result"]
    assert public["resumeSupported"] is True
    assert public["handoff"]["originSessionKey"] == "original-session"
    assert public["handoff"]["originRunId"] == "original-run"
    assert public["handoff"]["state"] == "resume_requested"
    assert "executionId" not in public["handoff"] and "owner" not in public["handoff"]
    assert authority.snapshot()["control"] == "agent"
    await asyncio.to_thread(service.handoffs.finish, execution, cancelled=True)
    authority.finish_run("original-run", generation)
