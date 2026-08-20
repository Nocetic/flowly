"""Flowly account keys must never cross local account boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from flowly.account import account_key
from flowly.config.loader import load_config, save_config
from flowly.config.schema import Config


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-home"))
    monkeypatch.setattr(account_key, "_activate_and_reconcile_flowly", lambda cfg: None)
    monkeypatch.setattr(account_key, "_audit_info", lambda *args, **kwargs: None)
    save_config(Config())


def _account(user_id: str, token: str = "firebase-token") -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        email=f"{user_id}@example.com",
        id_token=token,
    )


def _response(method: str, status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request(method, "https://example.test"),
    )


def test_same_owner_reuses_existing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_existing"
    cfg.providers.flowly.account_key_owner_uid = "uid-a"
    cfg.providers.flowly.account_key_id = "key-a"
    cfg.providers.flowly.account_key_origin = "verified"
    save_config(cfg)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("same-owner key must not be re-minted"),
    )

    result = account_key.ensure_account_key_change(_account("uid-a"))

    assert result.ready is True
    assert result.changed is False
    assert result.replaced_stale is False
    assert load_config().providers.flowly.account_key == "flw_existing"


def test_different_owner_replaces_existing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_from_account_a"
    cfg.providers.flowly.account_key_owner_uid = "uid-a"
    cfg.providers.flowly.account_key_id = "key-a"
    cfg.providers.flowly.account_key_origin = "verified"
    save_config(cfg)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: _response(
            "POST",
            200,
            {"key": "flw_for_account_b", "keyId": "key-b"},
        ),
    )

    result = account_key.ensure_account_key_change(_account("uid-b"))
    flowly = load_config().providers.flowly

    assert result.ready is True
    assert result.changed is True
    assert result.replaced_stale is True
    assert flowly.account_key == "flw_for_account_b"
    assert flowly.account_key_owner_uid == "uid-b"
    assert flowly.account_key_id == "key-b"
    assert flowly.account_key_origin == "verified"


def test_stale_key_is_removed_even_when_new_mint_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_from_account_a"
    cfg.providers.flowly.account_key_owner_uid = "uid-a"
    cfg.providers.flowly.account_key_id = "key-a"
    cfg.providers.flowly.account_key_origin = "verified"
    save_config(cfg)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: _response("POST", 503, {}),
    )

    result = account_key.ensure_account_key_change(_account("uid-b"))
    flowly = load_config().providers.flowly

    assert result.ready is False
    assert result.changed is True
    assert result.replaced_stale is True
    assert flowly.account_key == ""
    assert flowly.account_key_owner_uid == ""
    assert flowly.account_key_id == ""
    assert flowly.account_key_origin == ""


def test_mint_without_revocation_id_is_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: _response(
            "POST",
            200,
            {"key": "flw_missing_key_id"},
        ),
    )

    result = account_key.ensure_account_key_change(_account("uid-a"))

    assert result.ready is False
    assert load_config().providers.flowly.account_key == ""


def test_legacy_unowned_key_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_legacy_without_owner"
    save_config(cfg)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: _response(
            "POST",
            200,
            {"key": "flw_owned", "keyId": "key-owned"},
        ),
    )

    result = account_key.ensure_account_key_change(_account("uid-a"))

    assert result.replaced_stale is True
    assert load_config().providers.flowly.account_key == "flw_owned"


def test_legacy_unowned_same_account_key_is_adopted_without_remint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_legacy_same_account"
    save_config(cfg)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: (
            _response("POST", 200, {"valid": True, "keyId": "key-existing"})
            if url.endswith("/verify")
            else pytest.fail("verified legacy key must not be re-minted")
        ),
    )

    result = account_key.ensure_account_key_change(_account("uid-a"))
    flowly = load_config().providers.flowly

    assert result.ready is True
    assert result.changed is False
    assert result.replaced_stale is False
    assert flowly.account_key == "flw_legacy_same_account"
    assert flowly.account_key_owner_uid == "uid-a"
    assert flowly.account_key_id == "key-existing"
    assert flowly.account_key_origin == "verified"


def test_desktop_asserted_same_account_key_is_verified_before_cli_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_desktop_same_account"
    cfg.providers.flowly.account_key_owner_uid = "uid-a"
    cfg.providers.flowly.account_key_id = "desktop-key-id"
    cfg.providers.flowly.account_key_origin = "desktop"
    save_config(cfg)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: (
            _response("POST", 200, {"valid": True, "keyId": "desktop-key-id"})
            if url.endswith("/verify")
            else pytest.fail("verified Desktop key must not be re-minted")
        ),
    )

    assert account_key.account_key_matches(_account("uid-a")) is False
    result = account_key.ensure_account_key_change(_account("uid-a"))
    flowly = load_config().providers.flowly

    assert result.ready is True
    assert result.changed is False
    assert flowly.account_key == "flw_desktop_same_account"
    assert flowly.account_key_origin == "verified"


def test_clear_revokes_owned_key_and_preserves_byok(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_owned"
    cfg.providers.flowly.account_key_owner_uid = "uid-a"
    cfg.providers.flowly.account_key_id = "key-a"
    cfg.providers.flowly.account_key_origin = "verified"
    cfg.providers.flowly.server_id = "legacy-server"
    cfg.providers.flowly.auth_token = "legacy-token"
    cfg.providers.openai.api_key = "user-byok-key"
    save_config(cfg)
    monkeypatch.setattr(
        httpx,
        "delete",
        lambda *args, **kwargs: _response("DELETE", 200, {"success": True}),
    )

    result = account_key.clear_account_key(_account("uid-a"), revoke=True)
    reloaded = load_config()

    assert result.changed is True
    assert result.revoked is True
    assert reloaded.providers.flowly.account_key == ""
    assert reloaded.providers.flowly.account_key_owner_uid == ""
    assert reloaded.providers.flowly.account_key_id == ""
    assert reloaded.providers.flowly.account_key_origin == ""
    assert reloaded.providers.flowly.server_id == ""
    assert reloaded.providers.flowly.auth_token == ""
    assert reloaded.providers.openai.api_key == "user-byok-key"


@pytest.mark.asyncio
async def test_legacy_desktop_replacing_key_clears_cli_ownership_metadata() -> None:
    from flowly.channels import feature_rpc

    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_cli_key"
    cfg.providers.flowly.account_key_owner_uid = "uid-cli"
    cfg.providers.flowly.account_key_id = "key-cli"
    cfg.providers.flowly.account_key_origin = "verified"
    save_config(cfg)

    result = await feature_rpc.provider_set_flowly_account(
        {"accountKey": "flw_desktop_key", "serverId": "", "authToken": ""}
    )
    flowly = load_config().providers.flowly

    assert result["ok"] is True
    assert flowly.account_key == "flw_desktop_key"
    assert flowly.account_key_owner_uid == ""
    assert flowly.account_key_id == ""
    assert flowly.account_key_origin == ""


@pytest.mark.asyncio
async def test_legacy_desktop_resending_same_key_preserves_ownership_metadata() -> None:
    from flowly.channels import feature_rpc

    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_same_key"
    cfg.providers.flowly.account_key_owner_uid = "uid-cli"
    cfg.providers.flowly.account_key_id = "key-cli"
    cfg.providers.flowly.account_key_origin = "verified"
    save_config(cfg)

    result = await feature_rpc.provider_set_flowly_account(
        {"accountKey": "flw_same_key", "serverId": "", "authToken": ""}
    )
    flowly = load_config().providers.flowly

    assert result["ok"] is True
    assert result["changed"] is False
    assert flowly.account_key_owner_uid == "uid-cli"
    assert flowly.account_key_id == "key-cli"
    assert flowly.account_key_origin == "verified"


@pytest.mark.asyncio
async def test_desktop_binding_installs_owned_key_only_into_empty_slot() -> None:
    from flowly.channels import feature_rpc

    result = await feature_rpc.provider_bind_flowly_account(
        {
            "action": "install",
            "accountKey": "flw_desktop_key",
            "accountKeyId": "desktop-key-id",
            "accountOwnerUid": "desktop-uid",
            "expectedAccountKeyId": "",
        }
    )
    flowly = load_config().providers.flowly

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["conflict"] is False
    assert flowly.account_key == "flw_desktop_key"
    assert flowly.account_key_owner_uid == "desktop-uid"
    assert flowly.account_key_id == "desktop-key-id"
    assert flowly.account_key_origin == "desktop"


@pytest.mark.asyncio
async def test_desktop_binding_preserves_existing_byok_choice_until_explicit_use() -> None:
    from flowly.channels import feature_rpc

    cfg = load_config()
    cfg.providers.active = "openai"
    cfg.providers.openai.api_key = "user-owned-byok"
    save_config(cfg)

    result = await feature_rpc.provider_bind_flowly_account(
        {
            "action": "install",
            "accountKey": "flw_desktop_key",
            "accountKeyId": "desktop-key-id",
            "accountOwnerUid": "desktop-uid",
            "expectedAccountKeyId": "",
        }
    )
    reloaded = load_config()

    assert result["conflict"] is False
    assert reloaded.providers.active == "openai"
    assert reloaded.providers.openai.api_key == "user-owned-byok"
    assert reloaded.providers.flowly.account_key == "flw_desktop_key"


@pytest.mark.asyncio
async def test_desktop_binding_cannot_overwrite_concurrent_cli_key() -> None:
    from flowly.channels import feature_rpc

    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_cli_key"
    cfg.providers.flowly.account_key_owner_uid = "cli-uid"
    cfg.providers.flowly.account_key_id = "cli-key-id"
    cfg.providers.flowly.account_key_origin = "verified"
    save_config(cfg)

    result = await feature_rpc.provider_bind_flowly_account(
        {
            "action": "install",
            "accountKey": "flw_desktop_key",
            "accountKeyId": "desktop-key-id",
            "accountOwnerUid": "desktop-uid",
            "expectedAccountKeyId": "",
        }
    )
    flowly = load_config().providers.flowly

    assert result["conflict"] is True
    assert result["changed"] is False
    assert flowly.account_key == "flw_cli_key"
    assert flowly.account_key_owner_uid == "cli-uid"
    assert flowly.account_key_id == "cli-key-id"


@pytest.mark.asyncio
async def test_desktop_binding_cannot_overwrite_ownerless_existing_key() -> None:
    from flowly.channels import feature_rpc

    cfg = load_config()
    cfg.providers.flowly.account_key = "flw_legacy_or_manual_key"
    save_config(cfg)

    result = await feature_rpc.provider_bind_flowly_account(
        {
            "action": "install",
            "accountKey": "flw_desktop_key",
            "accountKeyId": "desktop-key-id",
            "accountOwnerUid": "desktop-uid",
            "expectedAccountKeyId": "",
        }
    )

    assert result["conflict"] is True
    assert load_config().providers.flowly.account_key == "flw_legacy_or_manual_key"


@pytest.mark.asyncio
async def test_stale_desktop_binding_cannot_remove_newer_cli_key() -> None:
    from flowly.channels import feature_rpc

    cfg = load_config()
    cfg.providers.active = "flowly"
    cfg.providers.flowly.account_key = "flw_new_cli_key"
    cfg.providers.flowly.account_key_owner_uid = "cli-uid"
    cfg.providers.flowly.account_key_id = "new-cli-key-id"
    cfg.providers.flowly.account_key_origin = "verified"
    save_config(cfg)

    result = await feature_rpc.provider_bind_flowly_account(
        {
            "action": "remove",
            "expectedAccountKeyId": "old-desktop-key-id",
        }
    )
    reloaded = load_config()

    assert result["conflict"] is True
    assert result["changed"] is False
    assert reloaded.providers.flowly.account_key == "flw_new_cli_key"
    assert reloaded.providers.active == "flowly"


@pytest.mark.asyncio
async def test_matching_desktop_binding_removes_only_its_key() -> None:
    from flowly.channels import feature_rpc

    cfg = load_config()
    cfg.providers.active = "flowly"
    cfg.providers.flowly.account_key = "flw_desktop_key"
    cfg.providers.flowly.account_key_owner_uid = "desktop-uid"
    cfg.providers.flowly.account_key_id = "desktop-key-id"
    cfg.providers.flowly.account_key_origin = "desktop"
    cfg.providers.openai.api_key = "preserved-byok"
    save_config(cfg)

    result = await feature_rpc.provider_bind_flowly_account(
        {
            "action": "remove",
            "expectedAccountKeyId": "desktop-key-id",
        }
    )
    reloaded = load_config()

    assert result["conflict"] is False
    assert result["changed"] is True
    assert reloaded.providers.flowly.account_key == ""
    assert reloaded.providers.active == ""
    assert reloaded.providers.openai.api_key == "preserved-byok"


def test_account_a_logout_then_account_b_login_resolves_only_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flowly.integrations.active_provider import resolve_active_provider

    cfg = load_config()
    cfg.providers.active = "flowly"
    cfg.providers.flowly.account_key = "flw_account_a"
    cfg.providers.flowly.account_key_owner_uid = "uid-a"
    cfg.providers.flowly.account_key_id = "key-a"
    cfg.providers.flowly.account_key_origin = "verified"
    save_config(cfg)
    responses = iter(
        [
            _response("DELETE", 200, {"success": True}),
            _response(
                "POST",
                200,
                {"key": "flw_account_b", "keyId": "key-b"},
            ),
        ]
    )
    monkeypatch.setattr(httpx, "delete", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: next(responses))

    account_key.clear_account_key(_account("uid-a"), revoke=True)
    provisioned = account_key.ensure_account_key_change(_account("uid-b"))
    active = resolve_active_provider(load_config())

    assert provisioned.ready is True
    assert active is not None
    assert active.key == "flowly"
    assert active.api_key == "flw_account_b"
    assert active.api_key != "flw_account_a"
