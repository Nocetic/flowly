"""Conversation deletion is a durable external authority boundary."""

import asyncio
import hashlib
import secrets

import pytest

from flowly.mcp.external_access import ExternalAccessError, ExternalAccessStore
from flowly.mcp.server.external_service import ExternalMCPService
from flowly.session.manager import SessionManager
from tests.mcp import test_external_runtime as support

runtime = support.runtime
issue = support.issue


async def test_delete_recreate_and_restart_never_revive_old_keys(runtime):
    token, row = await issue(runtime, ["web_search"])
    manager = runtime.bridge.owner.sessions
    assert manager.delete("web:owner")
    assert (await runtime.owner("list", {}))["credentials"][0]["status"] == "revoked"
    manager.save(manager.get_or_create("web:owner"))
    restarted = ExternalMCPService(runtime.store.path, runtime.bridge)
    try:
        for service in (runtime, restarted):
            with pytest.raises(ExternalAccessError, match="revoked"):
                await service.call(token, "web_search", {})
        fresh, _ = await issue(restarted, ["web_search"])
        assert not (await restarted.call(fresh, "web_search", {}))["isError"]
        assert next(item for item in restarted.store.list() if item["id"] == row["id"])["status"] == "revoked"
    finally:
        await restarted.close()


async def test_deletion_revokes_only_owner_keys_and_uses_captured_profile(runtime, monkeypatch, tmp_path):
    first, _ = await issue(runtime, ["web_search"])
    second, _ = await issue(runtime, ["messages_read"])
    manager = runtime.bridge.owner.sessions
    manager.save(manager.get_or_create("web:other"))
    other = "flm_" + secrets.token_hex(32)
    await runtime.owner("create", {
        "id": secrets.token_hex(16), "label": "Other owner", "sessionKey": "web:other",
        "tools": ["web_search"], "tokenDigest": hashlib.sha256(other.encode()).hexdigest(), "ttlSeconds": 3600,
    })
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "different-profile"))
    manager.delete("web:owner")
    for token in (first, second):
        with pytest.raises(ExternalAccessError):
            runtime.authorize(token)
    assert runtime.authorize(other)["sessionKey"] == "web:other"
    assert not (tmp_path / "different-profile" / "mcp-access.json").exists()


@pytest.mark.parametrize("failure", ["save", "corrupt"])
async def test_failed_durable_revocation_aborts_deletion(runtime, monkeypatch, failure):
    token, _ = await issue(runtime, ["web_search"])
    manager = runtime.bridge.owner.sessions
    before = manager._get_session_path("web:owner").read_bytes()
    if failure == "save":
        def fail(*_):
            raise OSError("simulated persistence failure")
        monkeypatch.setattr(ExternalAccessStore, "_save", fail)
    else:
        runtime.store.path.write_text("invalid state")
    with pytest.raises((OSError, ExternalAccessError)):
        manager.delete("web:owner")
    assert manager._get_session_path("web:owner").read_bytes() == before
    if failure == "save":
        assert runtime.authorize(token)
    else:
        with pytest.raises(ExternalAccessError):
            runtime.authorize(token)


@pytest.mark.parametrize("recreate", [False, True])
async def test_creation_in_discovery_cannot_cross_deletion(runtime, monkeypatch, recreate):
    started, release = asyncio.Event(), asyncio.Event()
    original = runtime.conversation_server.list_tools

    async def delayed():
        started.set()
        await release.wait()
        return await original()

    monkeypatch.setattr(runtime.conversation_server, "list_tools", delayed)
    task = asyncio.create_task(issue(runtime, ["messages_read"]))
    await asyncio.wait_for(started.wait(), 2)
    manager = runtime.bridge.owner.sessions
    manager.delete("web:owner")
    if recreate:
        manager.save(manager.get_or_create("web:owner"))
    release.set()
    with pytest.raises(ExternalAccessError, match="conversation changed"):
        await asyncio.wait_for(task, 2)
    assert runtime.store.list() == []


async def test_other_session_manager_deletion_revokes_durably(runtime):
    token, _ = await issue(runtime, ["web_search"])
    other_manager = SessionManager(runtime.bridge.owner.workspace)
    other_manager.delete("web:owner")
    other_manager.save(other_manager.get_or_create("web:owner"))
    with pytest.raises(ExternalAccessError):
        runtime.authorize(token)


async def test_orphaned_legacy_key_is_not_presented_as_active(runtime, monkeypatch):
    token, row = await issue(runtime, ["web_search"])
    monkeypatch.setattr(runtime.bridge.owner.sessions, "list_sessions", lambda: [])
    assert (await runtime.owner("list", {}))["credentials"][0]["status"] == "unavailable"
    with pytest.raises(ExternalAccessError, match="no longer exists"):
        runtime.authorize(token)
    assert (await runtime.owner("revoke", {"id": row["id"]}))["status"] == "revoked"


async def test_ordinary_save_does_not_invalidate_owner_keys(runtime):
    token, _ = await issue(runtime, ["web_search"])
    manager = runtime.bridge.owner.sessions
    session = manager.get_or_create("web:owner")
    session.add_message("user", "Another isolated test message")
    manager.save(session)
    assert runtime.authorize(token)
