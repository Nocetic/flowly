"""Provision a Flowly account key (``flw_…``) — the Source-0 provider credential.

Shared by the CLI (``flowly login``) and the TUI login modal so both auto-mint a
key right after sign-in, transparently — the user never deals with keys. The key
bills LLM usage to the account with NO server record and NO relay (it doesn't
touch ``channels.web``).

Best-effort + account-aware: it NEVER raises (login must not fail because
minting did), reuses a key only when its stored owner matches the authenticated
account, and still reconciles stale model defaults.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

_KEYS_ENDPOINT = "https://useflowlyapp.com/api/account/keys"
_KEYS_VERIFY_ENDPOINT = f"{_KEYS_ENDPOINT}/verify"


@dataclass(frozen=True)
class AccountKeyChange:
    """Outcome of reconciling the local hosted credential with an account."""

    ready: bool
    changed: bool
    replaced_stale: bool = False


@dataclass(frozen=True)
class AccountKeyClearResult:
    """Outcome of removing this installation's hosted credentials."""

    changed: bool
    revoked: bool | None = None


def _audit_info(event: str, **fields) -> None:
    """Audit persistence must not turn a successful credential write into failure."""
    try:
        from flowly.account import audit_log

        audit_log.info(event, **fields)
    except Exception:  # noqa: BLE001
        pass


def _activate_and_reconcile_flowly(cfg) -> None:
    """Keep a valid user model, or adopt the account catalog's default."""
    current_active = (cfg.providers.active or "").strip()
    if current_active not in ("", "flowly"):
        return

    from flowly.integrations import model_catalog
    from flowly.integrations.active_provider import set_active_provider

    set_active_provider("flowly")
    model_catalog.flush_cache()
    asyncio.run(model_catalog.reconcile_flowly_model(force_refresh=True))


def ensure_account_key_change(account) -> AccountKeyChange:
    """Ensure the local key belongs to the authenticated account.

    Unowned legacy/manual keys and keys tagged with another UID are cleared
    before minting. A failed mint therefore leaves no path back to a previous
    account's bearer.
    """
    replaced_stale = False
    metadata_present = False
    try:
        import httpx

        from flowly.config.loader import load_config, save_config

        cfg = load_config()
        flowly = cfg.providers.flowly
        existing_key = (getattr(flowly, "account_key", "") or "").strip()
        existing_owner = (
            getattr(flowly, "account_key_owner_uid", "") or ""
        ).strip()
        existing_origin = (
            getattr(flowly, "account_key_origin", "") or ""
        ).strip()
        user_id = (getattr(account, "user_id", "") or "").strip()

        if (
            existing_key
            and user_id
            and existing_owner == user_id
            and existing_origin == "verified"
        ):
            # Existing installs may still carry the old schema default
            # (Kimi K2.5). Re-check it against this account's authenticated
            # catalog instead of treating key presence as proof that the whole
            # provider configuration is current.
            try:
                _activate_and_reconcile_flowly(cfg)
            except Exception:  # noqa: BLE001
                pass
            return AccountKeyChange(ready=True, changed=False)

        # Older installs and older Desktop versions stored only the plaintext
        # key. Prove it against the currently authenticated Firebase account
        # before replacing it: a same-account migration stays a no-op, while a
        # foreign/revoked key can never cross an account boundary.
        token = (getattr(account, "id_token", "") or "").strip()
        if existing_key and token and user_id:
            try:
                verify_resp = httpx.post(
                    _KEYS_VERIFY_ENDPOINT,
                    headers={"Authorization": f"Bearer {token}"},
                    json={"accountKey": existing_key},
                    timeout=10.0,
                )
                verify_payload = verify_resp.json() if verify_resp.status_code == 200 else {}
                verified_key_id = (
                    verify_payload.get("keyId", "")
                    if isinstance(verify_payload, dict)
                    else ""
                )
                if (
                    isinstance(verify_payload, dict)
                    and verify_payload.get("valid") is True
                    and isinstance(verified_key_id, str)
                    and verified_key_id.strip()
                ):
                    flowly.account_key_owner_uid = user_id
                    flowly.account_key_id = verified_key_id.strip()
                    flowly.account_key_origin = "verified"
                    flowly.enabled = True
                    save_config(cfg)
                    _audit_info("account_key.ownership_adopted")
                    try:
                        _activate_and_reconcile_flowly(cfg)
                    except Exception:  # noqa: BLE001
                        pass
                    return AccountKeyChange(ready=True, changed=False)
            except Exception:  # noqa: BLE001
                # Verification is fail-closed below. Keeping an unverifiable
                # bearer active would risk billing a previously signed-in user.
                pass

        replaced_stale = bool(existing_key)
        metadata_present = bool(
            existing_owner
            or (getattr(flowly, "account_key_id", "") or "").strip()
            or existing_origin
        )
        if replaced_stale or metadata_present:
            flowly.account_key = ""
            flowly.account_key_owner_uid = ""
            flowly.account_key_id = ""
            flowly.account_key_origin = ""
            save_config(cfg)
            _audit_info(
                "account_key.stale_cleared",
                had_owner=bool(existing_owner),
                owner_matched=bool(user_id and existing_owner == user_id),
            )

        if not token or not user_id:
            return AccountKeyChange(
                ready=False,
                changed=replaced_stale or metadata_present,
                replaced_stale=replaced_stale,
            )

        resp = httpx.post(
            _KEYS_ENDPOINT,
            headers={"Authorization": f"Bearer {token}"},
            json={"label": str(getattr(account, "email", "") or getattr(account, "user_id", "") or "Flowly")},
            timeout=20.0,
        )
        if resp.status_code != 200:
            _audit_info("account_key.mint_failed", status=resp.status_code)
            return AccountKeyChange(
                ready=False,
                changed=replaced_stale or metadata_present,
                replaced_stale=replaced_stale,
            )

        payload = resp.json() or {}
        key = payload.get("key", "")
        key_id = payload.get("keyId", "")
        if (
            not isinstance(key, str)
            or not key.startswith("flw_")
            or not isinstance(key_id, str)
            or not key_id.strip()
        ):
            return AccountKeyChange(
                ready=False,
                changed=replaced_stale or metadata_present,
                replaced_stale=replaced_stale,
            )

        flowly.account_key = key
        flowly.account_key_owner_uid = user_id
        flowly.account_key_id = key_id.strip()
        flowly.account_key_origin = "verified"
        flowly.enabled = True
        save_config(cfg)
        # Make Flowly active only when the user hasn't deliberately picked a
        # BYOK provider, then reconcile the model against this account's plan.
        try:
            _activate_and_reconcile_flowly(cfg)
        except Exception:  # noqa: BLE001
            pass
        _audit_info("account_key.minted")
        return AccountKeyChange(
            ready=True,
            changed=True,
            replaced_stale=replaced_stale,
        )
    except Exception:  # noqa: BLE001
        # Truly best-effort: a mint failure must never break login.
        return AccountKeyChange(
            ready=False,
            changed=replaced_stale or metadata_present,
            replaced_stale=replaced_stale,
        )


def ensure_account_key(account) -> bool:
    """Compatibility wrapper returning only whether a usable key exists."""
    return ensure_account_key_change(account).ready


def account_key_matches(account) -> bool:
    """Return whether a stored key is explicitly owned by ``account``."""
    try:
        from flowly.config.loader import load_config

        flowly = load_config().providers.flowly
        key = (getattr(flowly, "account_key", "") or "").strip()
        owner = (getattr(flowly, "account_key_owner_uid", "") or "").strip()
        origin = (getattr(flowly, "account_key_origin", "") or "").strip()
        user_id = (getattr(account, "user_id", "") or "").strip()
        return bool(key and user_id and owner == user_id and origin == "verified")
    except Exception:  # noqa: BLE001
        return False


def clear_account_key(account=None, *, revoke: bool = False) -> AccountKeyClearResult:
    """Remove locally stored Flowly-hosted credentials.

    A key minted by this installation is also revoked when its owner, keyId,
    and current Firebase token are available. Backend/network failure never
    prevents the mandatory local clear; local config write failures propagate
    so logout cannot falsely report success.
    """
    from flowly.config.loader import load_config, save_config

    cfg = load_config()
    flowly = cfg.providers.flowly
    account_key_value = (getattr(flowly, "account_key", "") or "").strip()
    owner_uid = (getattr(flowly, "account_key_owner_uid", "") or "").strip()
    key_id = (getattr(flowly, "account_key_id", "") or "").strip()
    origin = (getattr(flowly, "account_key_origin", "") or "").strip()
    server_id = (getattr(flowly, "server_id", "") or "").strip()
    auth_token = (getattr(flowly, "auth_token", "") or "").strip()
    changed = bool(
        account_key_value or owner_uid or key_id or origin or server_id or auth_token
    )
    revoked: bool | None = None

    if revoke and key_id and account is not None:
        account_uid = (getattr(account, "user_id", "") or "").strip()
        token = (getattr(account, "id_token", "") or "").strip()
        if token and owner_uid and owner_uid == account_uid:
            try:
                import httpx

                response = httpx.delete(
                    _KEYS_ENDPOINT,
                    params={"keyId": key_id},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10.0,
                )
                revoked = bool(
                    response.status_code == 200
                    and (response.json() or {}).get("success") is True
                )
                if not revoked:
                    _audit_info("account_key.revoke_failed", status=response.status_code)
            except Exception:  # noqa: BLE001
                revoked = False
                _audit_info("account_key.revoke_failed", status="network")

    if changed:
        flowly.account_key = ""
        flowly.account_key_owner_uid = ""
        flowly.account_key_id = ""
        flowly.account_key_origin = ""
        flowly.server_id = ""
        flowly.auth_token = ""
        save_config(cfg)
        _audit_info("account_key.cleared", remotely_revoked=revoked)

    return AccountKeyClearResult(changed=changed, revoked=revoked)
