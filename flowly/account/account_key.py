"""Provision a Flowly account key (``flw_…``) — the Source-0 provider credential.

Shared by the CLI (``flowly login``) and the TUI login modal so both auto-mint a
key right after sign-in, transparently — the user never deals with keys. The key
bills LLM usage to the account with NO server record and NO relay (it doesn't
touch ``channels.web``).

Best-effort + idempotent: it NEVER raises (login must not fail because minting
did), never re-mints an existing key, and still reconciles stale model defaults.
"""
from __future__ import annotations

import asyncio

_KEYS_ENDPOINT = "https://useflowlyapp.com/api/account/keys"


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


def ensure_account_key(account) -> bool:
    """Mint + save an account key for an ALREADY-authenticated ``account`` if one
    isn't already stored. Returns True when a key is present afterwards."""
    try:
        import httpx

        from flowly.account import audit_log
        from flowly.config.loader import load_config, save_config

        cfg = load_config()
        if (getattr(cfg.providers.flowly, "account_key", "") or "").strip():
            # Existing installs may still carry the old schema default
            # (Kimi K2.5). Re-check it against this account's authenticated
            # catalog instead of treating key presence as proof that the whole
            # provider configuration is current.
            try:
                _activate_and_reconcile_flowly(cfg)
            except Exception:  # noqa: BLE001
                pass
            return True

        token = getattr(account, "id_token", "") or ""
        if not token:
            return False

        resp = httpx.post(
            _KEYS_ENDPOINT,
            headers={"Authorization": f"Bearer {token}"},
            json={"label": str(getattr(account, "email", "") or getattr(account, "user_id", "") or "Flowly")},
            timeout=20.0,
        )
        if resp.status_code != 200:
            audit_log.info("account_key.mint_failed", status=resp.status_code)
            return False

        key = (resp.json() or {}).get("key", "")
        if not isinstance(key, str) or not key.startswith("flw_"):
            return False

        cfg.providers.flowly.account_key = key
        cfg.providers.flowly.enabled = True
        save_config(cfg)
        # Make Flowly active only when the user hasn't deliberately picked a
        # BYOK provider, then reconcile the model against this account's plan.
        try:
            _activate_and_reconcile_flowly(cfg)
        except Exception:  # noqa: BLE001
            pass
        audit_log.info("account_key.minted")
        return True
    except Exception:  # noqa: BLE001
        # Truly best-effort: a mint failure must never break login.
        return False
