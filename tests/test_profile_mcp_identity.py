from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import flowly.profile_host as module
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError
from flowly.profile_host_contract import PROFILE_RPC_TIMEOUTS, validate_profile_rpc


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["host", "profile", "partial", "startup", "matching", "legacy"])
async def test_profile_rpc_identity_boundary(monkeypatch, tmp_path, case):
    import flowly.profile as profiles
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", tmp_path / "profiles")
    current = {"botId": "original"}
    monkeypatch.setattr(module, "_public_profile", lambda name: current)
    host = ProfileHost()
    runtime = SimpleNamespace(active_runs=set())

    async def startup(name):
        if case == "startup":
            current["botId"] = "replacement"
        return runtime

    host._ensure_runtime = AsyncMock(side_effect=startup)
    host._rpc = AsyncMock(return_value={"servers": []})
    params = {"name": "writer", "method": "sessions.list" if case == "legacy" else "mcp.connections.list", "params": {},
              "expectedHostId": host.host_id, "expectedBotId": "original"}
    if case == "host":
        params["expectedHostId"] = "other-host"
    if case == "profile":
        params["expectedBotId"] = "other-profile"
    if case == "partial":
        del params["expectedBotId"]
    if case == "legacy":
        del params["expectedHostId"]
        del params["expectedBotId"]
    if case in {"matching", "legacy"}:
        assert await host.dispatch("profiles.rpc", params) == {"servers": []}
        host._rpc.assert_awaited_once()
    else:
        with pytest.raises(ProfileHostError) as error:
            await host.dispatch("profiles.rpc", params)
        assert error.value.code == ("INVALID_PARAMS" if case == "partial" else "PROFILE_IDENTITY_CHANGED")
        host._rpc.assert_not_awaited()
    assert host.capabilities()["rpcIdentityGuard"] is True


def test_profile_mcp_surface_is_explicit_and_preserves_session_boundary():
    expected = {
        "mcp.capabilities", "mcp.connections.list", "mcp.connections.action",
        "mcp.setup.begin", "mcp.setup.status", "mcp.setup.pending", "mcp.setup.confirm",
        "mcp.setup.callback", "mcp.setup.cancel", "mcp.setup.cancel_request",
        "mcp.chat.pending", "mcp.chat.cancel",
    }
    assert {method for method in PROFILE_RPC_TIMEOUTS if method.startswith("mcp.")} == expected
    for method in expected:
        assert validate_profile_rpc(method, {}) == (method, {})
    for method in ["mcp.install", "mcp.upsert", "mcp.oauth_start", "mcp.access.create", "mcp.arbitrary"]:
        with pytest.raises(ProfileHostError, match="not available"):
            validate_profile_rpc(method, {})
    for method in ["mcp.chat.pending", "mcp.setup.begin"]:
        with pytest.raises(ProfileHostError) as error:
            validate_profile_rpc(method, {"sessionKey": "desktop:profile-room:private"})
        assert error.value.code == "REMOTE_SESSION_DENIED"


@pytest.mark.asyncio
async def test_mcp_cannot_use_legacy_name_only_routing(monkeypatch, tmp_path):
    import flowly.profile as profiles
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", tmp_path / "profiles")
    host = ProfileHost()
    host._target_rpc = AsyncMock()
    with pytest.raises(ProfileHostError) as error:
        await host.rpc("writer", "mcp.setup.begin", {})
    assert error.value.code == "INVALID_PARAMS"
    host._target_rpc.assert_not_awaited()
