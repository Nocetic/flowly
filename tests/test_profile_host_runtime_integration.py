from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp
import pytest

import flowly.profile as profiles
from flowly.gateway.server import GatewayServer
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="managed process-group lifecycle is POSIX-specific")
async def test_profile_host_starts_proxies_and_stops_real_isolated_gateway(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    default = home / ".flowly"
    root = default / "profiles"
    home.mkdir()
    default.mkdir()
    (default / "workspace").mkdir()
    default_credentials = default / "credentials" / "gmail.json"
    default_credentials.parent.mkdir()
    default_credentials.write_text(json.dumps({
        "mode": "flowly_broker", "issuer": "https://useflowlyapp.com",
        "grant_id": "a" * 32, "grant_secret": "b" * 43,
        "email": "primary@example.test", "disconnect_pending": True,
    }), encoding="utf-8")
    default_credentials_before = default_credentials.read_bytes()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "test-profile-host-key")
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    created = profiles.create_profile(
        "writer",
        local_runtime=True,
        provider="openai",
        model="openai/gpt-4o-mini",
    )
    config_path = created / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("providers", {}).setdefault("openai", {})["apiKey"] = (
        "test-profile-host-key"
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")
    host = ProfileHost()

    try:
        connected = await host.connect("writer")
        assert connected["status"]["state"] == "connected"

        result = await host.rpc("writer", "sessions.list", {})
        assert result == {"sessions": []}

        # Exercise the actual child process's feature handlers and allowlist,
        # rather than a permissive mock of profiles.rpc.
        history = await host.rpc("writer", "subagents.list", {"eventVersion": 2})
        assert history["schemaVersion"] == 2
        assert history["tasks"] == []
        for method in ("subagents.get", "subagents.result"):
            with pytest.raises(ProfileHostError) as missing:
                await host.rpc("writer", method, {"runId": "missing"})
            assert missing.value.code == "NOT_FOUND"

        writer = next(item for item in (await host.list())["profiles"] if item["name"] == "writer")
        identity = {"expected_host_id": host.host_id, "expected_bot_id": writer["botId"]}
        capability = await host.rpc("writer", "mcp.capabilities", {}, **identity)
        assert capability["connectionSetup"] is True
        assert capability["explicitPermissions"] is True
        connections = await host.rpc("writer", "mcp.connections.list", {}, **identity)
        assert isinstance(connections["servers"], list)
        pending = await host.rpc("writer", "mcp.setup.pending", {}, **identity)
        assert pending == {"operations": []}
        gmail_capability = await host.rpc("writer", "gmail.capabilities", {}, **identity)
        assert set(gmail_capability["methods"]) == {
            "gmail.capabilities", "gmail.status", "gmail.setup.begin", "gmail.setup.pending",
            "gmail.setup.status", "gmail.setup.cancel", "gmail.disconnect",
        }
        gmail = await host.rpc("writer", "gmail.status", {}, **identity)
        assert gmail == {"status": "not_configured", "connected": False}
        assert default_credentials.read_bytes() == default_credentials_before
        external_access = await host.rpc("writer", "mcp.access.list", {}, **identity)
        assert external_access["credentials"] == []
        assert external_access["endpointPath"] == "/mcp"

        sibling = profiles.create_profile("reader", local_runtime=True)
        sibling_before = (sibling / "config.json").read_bytes()
        sibling_credentials = sibling / "credentials" / "gmail.json"
        sibling_credentials.parent.mkdir(exist_ok=True)
        sibling_credentials.write_text(json.dumps({
            "mode": "flowly_broker", "issuer": "https://useflowlyapp.com",
            "grant_id": "c" * 32, "grant_secret": "d" * 43,
            "email": "reader@example.test", "disconnect_pending": True,
        }), encoding="utf-8")
        sibling_credentials_before = sibling_credentials.read_bytes()
        assert await host.rpc("writer", "gmail.status", {}, **identity) == {
            "status": "not_configured", "connected": False,
        }
        primary_config = default / "config.json"
        primary_before = primary_config.read_bytes() if primary_config.exists() else None
        writer_before = json.loads(config_path.read_text())
        peer = Path(__file__).parent / "mcp" / "profile_setup_peer.py"
        request = {"name": "ios_fixture", "requestId": "ios-profile-fixture-request-001",
                   "config": {"command": sys.executable, "args": [str(peer.resolve())]}}
        started = await host.rpc("writer", "mcp.setup.begin", request, **identity)
        operation_id = started["id"]

        async def await_phase(phase):
            async with asyncio.timeout(30):
                while True:
                    snapshot = await host.rpc("writer", "mcp.setup.status", {"id": operation_id}, **identity)
                    assert snapshot["phase"] not in {"failed", "cancelled", "expired"}, snapshot.get("error")
                    if snapshot["phase"] == phase:
                        return snapshot
                    await asyncio.sleep(0.05)

        review = await await_phase("review")
        assert set(review["tools"]) == {"read_fixture", "other_fixture"}
        assert "ios_fixture" not in json.loads(config_path.read_text()).get("mcpServers", {})
        await host.rpc("writer", "mcp.setup.confirm", {
            "id": operation_id, "permissions": {"mode": "selected", "include": ["read_fixture"]},
        }, **identity)
        complete = await await_phase("complete")
        assert complete["saved"] is True
        assert complete["runtime"]["connected"] is True
        saved = json.loads(config_path.read_text())
        assert saved["mcpServers"]["ios_fixture"]["tools"]["include"] == ["read_fixture"]
        assert {key: value for key, value in saved.items() if key != "mcpServers"} == {
            key: value for key, value in writer_before.items() if key != "mcpServers"
        }
        assert (sibling / "config.json").read_bytes() == sibling_before
        assert sibling_credentials.read_bytes() == sibling_credentials_before
        assert default_credentials.read_bytes() == default_credentials_before
        assert (primary_config.read_bytes() if primary_config.exists() else None) == primary_before

        saved_before_cancel = config_path.read_bytes()
        # A second draft must remain disposable and cannot erase the first one.
        second = {**request, "name": "cancelled_fixture", "requestId": "ios-profile-fixture-request-002"}
        started = await host.rpc("writer", "mcp.setup.begin", second, **identity)
        operation_id = started["id"]
        await await_phase("review")
        cancelled = await host.rpc("writer", "mcp.setup.cancel", {"id": operation_id}, **identity)
        assert cancelled["phase"] == "cancelled"
        assert config_path.read_bytes() == saved_before_cancel
        assert (sibling / "config.json").read_bytes() == sibling_before
        assert sibling_credentials.read_bytes() == sibling_credentials_before
        assert default_credentials.read_bytes() == default_credentials_before
        assert (primary_config.read_bytes() if primary_config.exists() else None) == primary_before

        stopped = await host.stop("writer")
        assert stopped["status"]["state"] == "stopped"
        assert profiles.read_runtime_lease(root / "writer") is None
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="managed process-group lifecycle is POSIX-specific")
async def test_second_manager_cooperatively_stops_desktop_owned_profile_runtime(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    default = home / ".flowly"
    root = default / "profiles"
    home.mkdir()
    default.mkdir()
    (default / "workspace").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    created = profiles.create_profile(
        "writer",
        local_runtime=True,
        provider="openai",
        model="openai/gpt-4o-mini",
    )
    config_path = created / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("providers", {}).setdefault("openai", {})["apiKey"] = "test-key"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    desktop_owner = ProfileHost()
    remote_manager = ProfileHost()

    try:
        owner_status = await desktop_owner.connect("writer")
        assert owner_status["status"]["owned"] is True

        remote_status = await remote_manager.connect("writer")
        assert remote_status["status"]["state"] == "connected"
        assert remote_status["status"]["owned"] is False
        assert await remote_manager.rpc("writer", "sessions.list", {}) == {"sessions": []}
        owned_lease = profiles.read_runtime_lease(root / "writer")
        assert owned_lease is not None
        assert "cooperative-stop-v1" in owned_lease["capabilities"]

        stopped = await remote_manager.stop("writer")
        assert stopped["status"]["state"] == "stopped"
        assert profiles.read_runtime_lease(root / "writer") is None
        with pytest.raises(ProfileHostError) as owner_closed:
            await desktop_owner.rpc("writer", "sessions.list", {})
        assert owner_closed.value.code in {"PROFILE_OFFLINE", "PROFILE_STOPPED"}
    finally:
        await remote_manager.shutdown()
        await desktop_owner.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="managed process-group lifecycle is POSIX-specific")
@pytest.mark.parametrize("session_prefix", ["ios", "android"])
async def test_authenticated_gateway_exposes_profile_lifecycle_and_proxy_rpc(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    session_prefix: str,
) -> None:
    home = tmp_path / "home"
    default = home / ".flowly"
    root = default / "profiles"
    home.mkdir()
    default.mkdir()
    (default / "workspace").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    created = profiles.create_profile(
        "writer",
        local_runtime=True,
        provider="openai",
        model="openai/gpt-4o-mini",
    )
    config_path = created / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("providers", {}).setdefault("openai", {})["apiKey"] = "test-key"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (created / "media").mkdir(exist_ok=True)
    (created / "media" / "generated.png").write_bytes(b"abcdefgh")

    async def on_chat(*_args):
        return "", {}

    server = GatewayServer(
        host="127.0.0.1",
        port=0,
        auth_token="remote-profile-secret",
        require_loopback_auth=True,
        advertise_control=False,
        enable_profile_host=True,
        on_chat_message=on_chat,
    )
    await server.start()
    try:
        origin = f"http://127.0.0.1:{server.port}"
        async with aiohttp.ClientSession() as session:
            ticket_response = await session.post(
                f"{origin}/api/auth/ws-ticket",
                headers={"Authorization": "Bearer remote-profile-secret"},
            )
            assert ticket_response.status == 200
            ticket = (await ticket_response.json())["ticket"]
            async with session.ws_connect(f"{origin}/ws?ticket={ticket}") as ws:
                request_number = 0
                events: list[dict] = []

                async def rpc(method: str, params: dict | None = None):
                    nonlocal request_number
                    request_number += 1
                    request_id = f"request-{request_number}"
                    await ws.send_json({
                        "type": "rpc",
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    })
                    while True:
                        frame = await ws.receive_json(timeout=100)
                        if frame.get("type") == "rpc" and frame.get("id") == request_id:
                            assert "error" not in frame, frame.get("error")
                            return frame.get("result")
                        if frame.get("type") == "event":
                            events.append(frame)

                capabilities = await rpc("profiles.capabilities")
                assert "profiles.rpc" in capabilities["methods"]
                assert "config.get" not in capabilities["profileRpcMethods"]
                assert "tools.access.get" in capabilities["profileRpcMethods"]
                assert "tools.access.set" in capabilities["profileRpcMethods"]
                assert "exec.policy.set" in capabilities["profileRpcMethods"]
                assert "codex.policy.set" in capabilities["profileRpcMethods"]
                system = await rpc("system.capabilities")
                assert system["profileHost"]["hostId"] == capabilities["hostId"]

                directory = await rpc("profiles.list")
                assert {item["name"] for item in directory["profiles"]} == {
                    "default",
                    "writer",
                }
                assert all("path" not in item for item in directory["profiles"])

                created_remote = await rpc("profiles.create", {
                    "name": "review-bot",
                    "cloneFrom": "writer",
                    "displayName": "Review Bot",
                    "description": "Reviews drafts",
                })
                assert created_remote["profile"]["displayName"] == "Review Bot"
                configured = await rpc("profiles.configure", {
                    "name": "review-bot",
                    "description": "Reviews final drafts",
                    "markTone": "violet",
                })
                assert configured["profile"]["description"] == "Reviews final drafts"
                settings = await rpc("profiles.settings", {"name": "review-bot"})
                assert "workspace" not in settings["settings"]
                prepared = await rpc("profiles.delete.prepare", {"name": "review-bot"})
                deleted = await rpc("profiles.delete.commit", {
                    "name": "review-bot",
                    "confirmation": prepared["confirmation"],
                })
                assert deleted["botId"] == prepared["profile"]["botId"]

                connected = await rpc("profiles.connect", {"name": "writer"})
                assert connected["status"]["state"] == "connected"
                assert any(
                    event.get("event") == "profile.event"
                    and event.get("data", {}).get("profile") == "writer"
                    and event.get("data", {}).get("type") == "connection"
                    and event.get("data", {}).get("data", {}).get("state") == "connected"
                    for event in events
                )
                sessions = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "sessions.list",
                    "params": {},
                })
                assert sessions == {"sessions": []}
                history = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "chat.history",
                    "params": {"sessionKey": f"{session_prefix}:writer-thread"},
                })
                assert history["messages"] == []
                media = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "media.read",
                    "params": {
                        "mediaId": "generated.png",
                        "offset": 2,
                        "length": 3,
                    },
                })
                assert media == {
                    "mediaId": "generated.png",
                    "size": 8,
                    "mimeType": "image/png",
                    "offset": 2,
                    "eof": False,
                    "data": "Y2Rl",
                }
                selected = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "sessions.model.set",
                    "params": {
                        "sessionKey": f"{session_prefix}:writer-thread",
                        "model": "openai/gpt-4o-mini",
                    },
                })
                assert selected["model"] == "openai/gpt-4o-mini"
                model = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "sessions.model.get",
                    "params": {"sessionKey": f"{session_prefix}:writer-thread"},
                })
                assert model["model"] == "openai/gpt-4o-mini"
                approvals = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "exec.approval.list",
                    "params": {},
                })
                assert approvals == {"approvals": []}
                access = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "tools.access.get",
                    "params": {},
                })
                assert "groups" in access
                updated_access = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "tools.access.set",
                    "params": {"disabledToolsets": ["filesystem"]},
                })
                assert updated_access["disabledToolsets"] == ["filesystem"]
                execution = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "exec.policy.set",
                    "params": {"security": "allowlist", "ask": "on-miss"},
                })
                assert execution["security"] == "allowlist"
                assert execution["ask"] == "on-miss"
                codex = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "codex.policy.set",
                    "params": {
                        "approvalPolicy": "on-request",
                        "sandbox": "workspace-write",
                    },
                })
                assert codex["ok"] is True
                read_codex = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "codex.policy.get",
                    "params": {},
                })
                assert read_codex["approvalPolicy"] == "on-request"
                assert read_codex["sandbox"] == "workspace-write"
                clarifies = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "agent.clarify.list",
                    "params": {},
                })
                assert clarifies == {"clarifies": []}
                routines = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "cron.list",
                    "params": {},
                })
                assert routines == {"jobs": [], "running": []}
                added = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "cron.add",
                    "params": {
                        "name": "daily-review",
                        "message": "Review the inbox",
                        "schedule": {"kind": "every", "everyMs": 86_400_000},
                        "deliver": False,
                    },
                })
                job_id = added["job"]["id"]
                updated = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "cron.update",
                    "params": {"id": job_id, "enabled": False},
                })
                assert updated["job"]["enabled"] is False
                removed = await rpc("profiles.rpc", {
                    "name": "writer",
                    "method": "cron.remove",
                    "params": {"id": job_id},
                })
                assert removed == {"ok": True}
                stopped = await rpc("profiles.stop", {"name": "writer"})
                assert stopped["status"]["state"] == "stopped"
    finally:
        await server.stop()
