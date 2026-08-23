from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
import flowly.profile_host as profile_host_module
from flowly.gateway.server import GatewayServer
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError


@pytest.fixture
def profile_roots(tmp_path, monkeypatch: pytest.MonkeyPatch):
    default = tmp_path / ".flowly"
    root = default / "profiles"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    return default, root


def test_remote_gateway_requires_authentication() -> None:
    with pytest.raises(ValueError, match="requires gateway authentication"):
        GatewayServer(host="0.0.0.0", enable_profile_host=True)


@pytest.mark.asyncio
async def test_profile_directory_and_statuses_are_public_and_stable(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True, model="openai/gpt-5")
    host = ProfileHost()

    directory = await host.list()
    statuses = await host.statuses()

    assert uuid.UUID(directory["hostId"])
    assert {item["name"] for item in directory["profiles"]} == {"default", "writer"}
    assert all("path" not in item for item in directory["profiles"])
    assert all(uuid.UUID(item["botId"]) for item in directory["profiles"])
    writer = next(item for item in directory["profiles"] if item["name"] == "writer")
    assert writer["model"] == "openai/gpt-5"
    assert {item["profile"] for item in statuses["statuses"]} == {"default", "writer"}
    assert all(item["botId"] for item in statuses["statuses"])
    assert host.capabilities()["defaultProfileRpc"] == "direct"
    assert host.capabilities()["maxConcurrentRuntimes"] == 4


@pytest.mark.asyncio
async def test_remote_chat_proxy_assigns_identity_and_authority(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    profiles.create_profile("reviewer", local_runtime=True)
    host = ProfileHost()
    runtime = SimpleNamespace(last_used_at=0.0, active_runs=set())
    host._ensure_runtime = AsyncMock(return_value=runtime)  # type: ignore[method-assign]
    host._rpc = AsyncMock(return_value={"runId": "run-1"})  # type: ignore[method-assign]

    result = await host.rpc(
        "writer",
        "chat.send",
        {
            "sessionKey": "ios:thread-1",
            "message": "Ask @reviewer",
            "profileDirectory": ["spoofed"],
            "profileMentions": ["reviewer", "writer", "missing"],
            "profileMessageContext": {"sourceProfile": "spoofed", "hop": 3},
            "allowedTools": ["exec"],
            "disabledTools": ["message_profile"],
            "turnOrigin": "routine",
        },
    )

    assert result == {"runId": "run-1"}
    sent = host._rpc.await_args.args[2]
    assert sent["profileDirectory"] == ["default", "reviewer", "writer"]
    assert sent["profileMentions"] == ["reviewer"]
    assert sent["turnOrigin"] == "user"
    assert "profileMessageContext" not in sent
    assert "allowedTools" not in sent
    assert "disabledTools" not in sent
    assert runtime.active_runs == {"run-1"}


@pytest.mark.asyncio
async def test_delete_requires_fresh_confirmation_bound_to_bot(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    prepared = await host.prepare_delete("writer")

    with pytest.raises(ProfileHostError) as invalid:
        await host.commit_delete("writer", "not-the-confirmation")
    assert invalid.value.code == "DELETE_CONFIRMATION_INVALID"
    assert profiles.profile_exists("writer")

    result = await host.commit_delete("writer", prepared["confirmation"])
    assert result["ok"] is True
    assert result["botId"] == prepared["profile"]["botId"]
    assert not profiles.profile_exists("writer")


@pytest.mark.asyncio
async def test_profile_mutations_reject_ambiguous_parameter_types(profile_roots) -> None:
    host = ProfileHost()
    with pytest.raises(ProfileHostError) as bad_name:
        await host.create({"name": 123})
    assert bad_name.value.code == "INVALID_PARAMS"

    profiles.create_profile("writer", local_runtime=True)
    with pytest.raises(ProfileHostError, match="No bot settings"):
        await host.configure({"name": "writer"})
    with pytest.raises(ProfileHostError) as bad_model:
        await host.configure({"name": "writer", "model": ["not", "a", "string"]})
    assert bad_model.value.code == "INVALID_PARAMS"


@pytest.mark.asyncio
async def test_profile_runtime_capacity_is_bounded(profile_roots) -> None:
    host = ProfileHost()
    for name in ("one", "two", "three", "four", "five"):
        profiles.create_profile(name, local_runtime=True)
    host._runtimes = {name: object() for name in ("one", "two", "three", "four")}  # type: ignore[assignment]

    with pytest.raises(ProfileHostError) as capacity:
        await host._ensure_runtime("five")
    assert capacity.value.code == "PROFILE_CAPACITY"
    assert capacity.value.retryable is True


@pytest.mark.asyncio
async def test_external_runtime_is_visible_but_not_stopped(profile_roots, monkeypatch) -> None:
    profiles.create_profile("writer", local_runtime=True)
    monkeypatch.setattr(
        profile_host_module,
        "reconcile_runtime_lease",
        lambda *_args, **_kwargs: {"instanceId": "other-manager"},
    )
    host = ProfileHost()

    assert host.status("writer")["state"] == "external"
    assert host.status("writer")["owned"] is False
    with pytest.raises(ProfileHostError) as conflict:
        await host.stop("writer")
    assert conflict.value.code == "PROFILE_OWNERSHIP_CONFLICT"
    with pytest.raises(ProfileHostError) as connect_conflict:
        await host.connect("writer")
    assert connect_conflict.value.code == "PROFILE_OWNERSHIP_CONFLICT"


@pytest.mark.asyncio
async def test_named_bot_can_broker_to_default_through_primary_gateway(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)

    async def on_chat(
        _session_key,
        _message,
        _run_id,
        stream_callback,
        _media,
        _voice_mode,
        _iteration_callback,
        _render_capabilities,
        _metadata,
    ):
        await stream_callback("Primary answer")
        return "Primary answer", {}

    server = GatewayServer(
        host="127.0.0.1",
        on_chat_message=on_chat,
        enable_profile_host=True,
    )
    host = server._profile_host
    assert host is not None

    result = await host._broker("writer", {
        "sourceProfile": "writer",
        "sourceSessionKey": "ios:writer-thread",
        "targetProfile": "default",
        "message": "Please review this",
        "correlationId": "correlation-1",
        "hop": 1,
    })

    assert result == {
        "ok": True,
        "targetProfile": "default",
        "runId": result["runId"],
        "response": "Primary answer",
        "correlationId": "correlation-1",
        "hop": 1,
    }
    assert server._profile_host_client_id not in server._ws_clients
    await server.stop()


@pytest.mark.asyncio
async def test_profile_rpc_dispatch_preserves_structured_error() -> None:
    server = GatewayServer(host="127.0.0.1")
    fake_host = SimpleNamespace(
        dispatch=AsyncMock(side_effect=ProfileHostError(
            "PROFILE_CAPACITY",
            "Four bots are already running. Stop one and try again.",
            retryable=True,
        ))
    )
    server._profile_host = fake_host
    ws = SimpleNamespace(closed=False, messages=[])

    async def send_json(payload):
        ws.messages.append(payload)

    ws.send_json = send_json
    await server._handle_ws_rpc(ws, "ios-client", {
        "type": "rpc",
        "id": "rpc-1",
        "method": "profiles.rpc",
        "params": {"name": "writer", "method": "chat.history", "params": {}},
    })

    assert ws.messages == [{
        "type": "rpc",
        "id": "rpc-1",
        "error": {
            "code": "PROFILE_CAPACITY",
            "message": "Four bots are already running. Stop one and try again.",
            "retryable": True,
        },
    }]


@pytest.mark.asyncio
async def test_wrapped_default_profile_rpc_uses_primary_gateway(profile_roots) -> None:
    primary = AsyncMock(return_value={"messages": []})
    host = ProfileHost(primary_rpc=primary)

    result = await host.dispatch("profiles.rpc", {
        "name": "default",
        "method": "chat.history",
        "params": {"sessionKey": "ios:default-thread"},
        "timeoutMs": 5_000,
    })

    assert result == {"messages": []}
    primary.assert_awaited_once_with(
        "chat.history", {"sessionKey": "ios:default-thread"}, 5.0
    )
    assert host.capabilities()["wrappedDefaultProfileRpc"] is True


@pytest.mark.asyncio
async def test_profile_events_include_host_and_bot_identity(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    host = ProfileHost(on_event=on_event)
    await host._emit("writer", "connection", {"state": "starting"})

    assert events[0]["hostId"] == host.host_id
    assert uuid.UUID(events[0]["botId"])
    assert events[0]["profile"] == "writer"
    assert events[0]["type"] == "connection"


@pytest.mark.asyncio
async def test_internal_broker_serializes_turns_per_target(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    profiles.create_profile("reviewer", local_runtime=True)
    host = ProfileHost()
    active = 0
    max_active = 0

    async def run_turn(**_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return {"ok": True}

    host._run_broker_turn = run_turn  # type: ignore[method-assign]
    params = {
        "sourceProfile": "writer",
        "sourceSessionKey": "ios:one",
        "targetProfile": "reviewer",
        "message": "Review",
        "correlationId": "correlation-1",
        "hop": 1,
    }
    await asyncio.gather(host._broker("writer", params), host._broker("writer", params))

    assert max_active == 1
