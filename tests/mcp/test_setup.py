"""Setup transactions require owner consent and preserve unrelated settings."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.shared.auth import OAuthToken

from flowly.mcp.oauth import FlowlyTokenStorage, token_storage_for
from flowly.mcp.oauth_handoff import IOS_OAUTH_REDIRECT_URI, handoff_for
from flowly.mcp.setup import MCPSetupError, MCPSetupManager

REDIRECT = "http://127.0.0.1:54321/mcp/oauth/callback/" + "a" * 32


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return MCPSetupManager(tmp_path / "config.json", AsyncMock(return_value=(True, ["read", "write"], "")), AsyncMock(return_value={"ok": True}))


async def ready(manager, operation_id):
    for _ in range(50):
        op = manager.get(operation_id)
        if op.phase != "checking":
            return op
        await asyncio.sleep(0)
    pytest.fail("Setup did not progress")


def begin(manager, **params):
    return manager.begin({"name": "demo", "requestId": "request-identifier-001", "config": {"command": "demo"}, **params})["id"]


@pytest.mark.asyncio
async def test_probe_never_saves_before_consent(manager):
    operation = begin(manager)
    op = await ready(manager, operation)
    assert op.phase == "review"
    assert not manager.store.path.exists()
    manager.apply.assert_not_called()
    await manager.cancel(operation)
    assert not manager.store.path.exists()


@pytest.mark.asyncio
async def test_confirmation_is_idempotent_and_preserves_mail_settings(manager):
    operation = begin(manager)
    op = await ready(manager, operation)
    # Another settings pane updates an unrelated integration during review.
    unrelated = {"channels": {"email": {"enabled": True, "imapPassword": "unchanged"}}}
    manager.store.path.write_text(json.dumps(unrelated))
    policy = {"mode": "selected", "include": ["read"]}
    manager.confirm(operation, policy)
    manager.confirm(operation, policy)
    await op.task
    assert op.phase == "complete"
    manager.confirm(operation, policy)
    manager.apply.assert_awaited_once_with("demo")
    stored = json.loads(manager.store.path.read_text())
    assert stored["channels"] == unrelated["channels"]
    assert stored["mcpServers"]["demo"]["tools"]["include"] == ["read"]
    assert "unchanged" not in str(op.snapshot())


@pytest.mark.asyncio
async def test_last_selection_is_explicitly_none_and_disabled(manager):
    operation = begin(manager)
    op = await ready(manager, operation)
    manager.confirm(operation, {"mode": "selected", "include": []})
    await op.task
    config = manager.store.read()["mcpServers"]["demo"]
    assert config["tools"]["mode"] == "none"
    assert config["enabled"] is False


@pytest.mark.asyncio
async def test_conflicting_edit_is_not_overwritten(manager):
    operation = begin(manager)
    op = await ready(manager, operation)
    manager.store.path.write_text(json.dumps({"mcpServers": {"demo": {"command": "newer"}}}))
    manager.confirm(operation, {"mode": "all"})
    await op.task
    assert op.phase == "failed"
    assert op.error["code"] == "CONFLICT"
    assert manager.store.read()["mcpServers"]["demo"]["command"] == "newer"
    manager.apply.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_start_and_invalid_permission_do_not_probe_twice(manager):
    operation = begin(manager)
    assert begin(manager) == operation
    op = await ready(manager, operation)
    with pytest.raises(MCPSetupError):
        manager.confirm(operation, {"mode": "selected", "include": ["unknown"]})
    assert op.phase == "review"
    manager.probe.assert_awaited_once()
    with pytest.raises(MCPSetupError, match="different"):
        begin(manager, config={"command": "different"})
    await manager.cancel(operation)


@pytest.mark.asyncio
async def test_cancellation_before_worker_starts_is_terminal(manager):
    operation = begin(manager)
    assert (await manager.cancel(operation))["phase"] == "cancelled"
    manager.probe.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_uri", [REDIRECT, IOS_OAUTH_REDIRECT_URI])
async def test_reauth_publishes_new_slot_only_after_consent(manager, redirect_uri):
    url = "https://mcp.example"
    old = FlowlyTokenStorage("demo", url)
    await old.set_tokens(OAuthToken(access_token="working", token_type="Bearer"))
    original = old._path.read_bytes()
    manager.store.path.write_text(json.dumps({"mcpServers": {"demo": {"url": url, "auth": "oauth"}}}))
    async def probe(name, config, *, interactive):
        assert interactive is True
        assert handoff_for(name, url) is not None
        assert handoff_for(name, url).redirect_uri == redirect_uri
        await token_storage_for(name, url, credential_id=config["oauth_credential_id"]).set_tokens(
            OAuthToken(access_token="fresh", token_type="Bearer"),
        )
        return True, ["read"], ""
    manager.probe = probe
    operation = begin(manager, config=None, intent="reauthorize", redirectUri=redirect_uri)
    op = await ready(manager, operation)
    assert old._path.read_bytes() == original
    assert (await FlowlyTokenStorage("demo", url).get_tokens()).access_token == "working"
    manager.confirm(operation, {"mode": "all"})
    await op.task
    assert op.phase == "complete"
    assert (await FlowlyTokenStorage("demo", url).get_tokens()).access_token == "fresh"
    assert old._path.read_bytes() == original


@pytest.mark.asyncio
async def test_runtime_failure_is_reported_as_saved_not_connected(manager):
    manager.apply = AsyncMock(return_value={"ok": False})
    operation = begin(manager)
    op = await ready(manager, operation)
    manager.confirm(operation, {"mode": "all"})
    await op.task
    assert op.phase == "failed"
    assert op.saved is True
    assert op.error["code"] == "APPLY_FAILED"


@pytest.mark.asyncio
async def test_corrupt_config_is_not_silently_replaced(manager):
    manager.store.path.write_text("{corrupt")
    with pytest.raises(MCPSetupError, match="safely read"):
        begin(manager)
    assert manager.store.path.read_text() == "{corrupt"


@pytest.mark.asyncio
async def test_secret_error_and_status_are_redacted(manager):
    async def fail(*args, **kwargs):
        raise RuntimeError("Failed with private-token-123")
    manager.probe = fail
    operation = begin(manager, config={"command": "demo", "env": {"API_KEY": "private-token-123"}})
    op = await ready(manager, operation)
    await op.task
    assert op.phase == "failed"
    assert "private-token-123" not in str(op.snapshot())


@pytest.mark.asyncio
async def test_resources_only_permission_requires_advertised_capability(manager):
    from flowly.mcp.probe import ProbedTools

    manager.probe.return_value = (True, ProbedTools([], resources=True), "")
    operation = begin(manager)
    op = await ready(manager, operation)
    assert op.snapshot()["capabilities"] == {"resources": True, "prompts": False}
    with pytest.raises(MCPSetupError):
        manager.confirm(operation, {"mode": "all", "prompts": True})
    manager.confirm(operation, {"mode": "selected", "resources": True})
    await op.task
    stored = manager.store.read()["mcpServers"]["demo"]
    assert stored["enabled"] is True
    assert stored["tools"]["mode"] == "selected"
    assert stored["tools"]["include"] == []
    assert stored["tools"]["resources"] is True


@pytest.mark.asyncio
async def test_catalog_secret_is_draft_only_until_consent(manager, monkeypatch):
    from flowly.mcp.catalog import CatalogEntry, EnvVarSpec

    entry = CatalogEntry("demo", "", "", {"type": "stdio", "command": "demo"}, "api_key", [EnvVarSpec("API_KEY", "Key")])
    monkeypatch.setattr("flowly.mcp.catalog.get_entry", lambda name: entry)
    operation = begin(manager, config=None, catalog=True, envValues={"API_KEY": "private-catalog-key"})
    op = await ready(manager, operation)
    assert manager.probe.call_args.args[1]["env"] == {"API_KEY": "private-catalog-key"}
    assert not manager.store.path.exists()
    assert not (manager.store.path.parent / ".env").exists()
    assert "private-catalog-key" not in str(op.snapshot())
    await manager.cancel(operation)
    assert not manager.store.path.exists()


@pytest.mark.asyncio
async def test_remove_drains_before_erasing_credentials(manager, monkeypatch):
    manager.store.path.write_text(json.dumps({"mcpServers": {"demo": {"command": "demo"}}, "channels": {"email": {"enabled": True}}}))
    clear = Mock()
    reached = asyncio.Event()
    release = asyncio.Event()
    def clear_tokens(name):
        assert release.is_set()
        clear(name)
    monkeypatch.setattr("flowly.mcp.oauth.clear_all_tokens", clear_tokens)
    async def apply(name):
        assert "demo" not in manager.store.read()["mcpServers"]
        reached.set()
        await release.wait()
        return {"ok": True}
    manager.apply = apply
    params = {"name": "demo", "requestId": "request-identifier-001", "action": "remove"}
    result = manager.manage(params)
    assert manager.manage(params)["id"] == result["id"]
    op = manager.get(result["id"])
    await reached.wait()
    with pytest.raises(MCPSetupError, match="applied"):
        await manager.cancel(op.id)
    clear.assert_not_called()
    release.set()
    await op.task
    assert op.phase == "complete"
    assert manager.store.read()["channels"] == {"email": {"enabled": True}}
    clear.assert_called_once_with("demo")


@pytest.mark.asyncio
async def test_enable_does_not_widen_none_permissions(manager):
    manager.store.path.write_text(json.dumps({"mcpServers": {"demo": {"command": "demo", "enabled": False, "tools": {"mode": "none"}}}}))
    with pytest.raises(MCPSetupError, match="permissions"):
        manager.manage({"name": "demo", "requestId": "request-identifier-001", "action": "enable"})


@pytest.mark.asyncio
async def test_cancel_before_begin_acknowledgment_prevents_late_probe(manager):
    params = {"name": "demo", "requestId": "request-identifier-001"}
    assert (await manager.cancel_request(params))["phase"] == "cancelled"
    with pytest.raises(MCPSetupError) as error:
        begin(manager)
    assert error.value.code == "CANCELLED"
    manager.probe.assert_not_called()
    assert not manager.store.path.exists()


@pytest.mark.asyncio
async def test_cancel_by_request_id_resolves_lost_begin_acknowledgment(manager):
    operation = begin(manager)
    op = await ready(manager, operation)
    assert op.snapshot()["requestId"] == "request-identifier-001"
    assert (await manager.cancel_request({"name": "demo", "requestId": op.request_id}))["phase"] == "cancelled"
    assert not manager.store.path.exists()
