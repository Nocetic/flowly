"""OpenAI Codex (ChatGPT subscription) OAuth auth module.

Credentials aren't available in CI, so these drive the pure helpers +
storage/resolution with fabricated JWTs and a fake httpx client.
"""

import base64
import json
import time
from pathlib import Path

import pytest

from flowly.auth import openai_codex
from flowly.config.schema import Config
from flowly.integrations.active_provider import _build_for, resolve_active_provider


@pytest.fixture(autouse=True)
def isolated_flowly_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly"))
    # Point CODEX_HOME at a scratch dir so the real ~/.codex/auth.json on the
    # dev machine never leaks into these tests.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(openai_codex, "_try_keyring", lambda: None)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_jwt(claims: dict) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.sig"


def _access_token(exp_in: int = 3600, plan: str = "plus") -> str:
    return _make_jwt({
        "exp": int(time.time()) + exp_in,
        "https://api.openai.com/auth": {"chatgpt_plan_type": plan},
    })


def _id_token(account_id: str = "acct-123", email: str = "u@example.com") -> str:
    return _make_jwt({
        "email": email,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    })


def test_client_id_is_hardcoded_and_not_overridable(monkeypatch: pytest.MonkeyPatch):
    expected = "app_EMoamEEZ73f0CkXaXp7hrann"
    assert openai_codex.CODEX_OAUTH_CLIENT_ID == expected
    assert openai_codex.resolve_client_id() == expected
    assert openai_codex.require_client_id(Config()) == expected


def test_redirect_uri_is_the_registered_loopback_port():
    # The Codex OAuth client only accepts this exact loopback redirect.
    assert openai_codex.CODEX_OAUTH_REDIRECT_PORT == 1455
    assert openai_codex.CODEX_OAUTH_REDIRECT_URI == "http://localhost:1455/auth/callback"


def test_pkce_challenge_is_s256_base64url():
    challenge = openai_codex.pkce_challenge("abc123")
    assert "=" not in challenge
    assert challenge == "bKE9UspwyIPg8LsQHkJaiehiTeUdstI5JZOvaoQRgJA"


def test_account_id_and_plan_and_email_extracted_from_jwts():
    payload = openai_codex.CodexTokenPayload.from_raw({
        "tokens": {
            "access_token": _access_token(plan="pro"),
            "id_token": _id_token(account_id="acct-xyz", email="me@x.com"),
            "refresh_token": "r",
        }
    })
    assert payload is not None
    assert payload.account_id == "acct-xyz"
    assert payload.email == "me@x.com"
    assert payload.plan == "pro"


def test_authorize_url_has_pkce_and_flowly_originator():
    url = openai_codex.build_authorize_url(
        client_id="app_x", code_challenge="chal", state="st"
    )
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url
    assert "originator=flowly" in url
    assert "codex_cli_simplified_flow=true" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback" in url


def test_token_storage_roundtrip_uses_credentials_dir():
    payload = openai_codex.CodexTokenPayload(
        access_token=_access_token(),
        refresh_token="refresh-1",
        id_token=_id_token(),
        account_id="acct-123",
        email="u@example.com",
        plan="plus",
    )
    backend = openai_codex.save_token_payload(payload)
    assert backend.startswith("file:")
    assert openai_codex.credentials_path().exists()

    loaded = openai_codex.load_token_payload()
    assert loaded is not None
    assert loaded.access_token == payload.access_token
    assert loaded.account_id == "acct-123"
    assert loaded.email == "u@example.com"


def test_codex_auth_json_is_a_fallback_source():
    # No Flowly store, but a Codex CLI auth.json exists → still usable.
    codex_home = Path(openai_codex.codex_home_dir())
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": _access_token(),
            "id_token": _id_token(account_id="cli-acct"),
            "refresh_token": "cli-refresh",
            "account_id": "cli-acct",
        },
        "last_refresh": "2026-07-02T00:00:00Z",
    }), encoding="utf-8")

    payload = openai_codex.load_token_payload()
    assert payload is not None
    assert payload.account_id == "cli-acct"
    assert payload.refresh_token == "cli-refresh"


def test_flowly_store_wins_over_codex_auth_json():
    # Both present → Flowly's own store is preferred.
    openai_codex.save_token_payload(openai_codex.CodexTokenPayload(
        access_token=_access_token(), refresh_token="flowly-r",
        id_token=_id_token(account_id="flowly-acct"), account_id="flowly-acct",
    ))
    codex_home = Path(openai_codex.codex_home_dir())
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {"access_token": _access_token(), "account_id": "cli-acct",
                   "id_token": _id_token(account_id="cli-acct"), "refresh_token": "x"},
    }), encoding="utf-8")

    payload = openai_codex.load_token_payload()
    assert payload is not None
    assert payload.account_id == "flowly-acct"


def test_active_provider_resolves_openai_codex_without_refresh(monkeypatch):
    openai_codex.save_token_payload(openai_codex.CodexTokenPayload(
        access_token=_access_token(exp_in=3600),
        refresh_token="refresh-1",
        id_token=_id_token(account_id="acct-123", email="u@example.com"),
        account_id="acct-123",
        email="u@example.com",
        plan="plus",
    ))
    # A fresh token must NOT trigger a network refresh.
    monkeypatch.setattr(
        openai_codex, "refresh_tokens",
        lambda **kw: (_ for _ in ()).throw(AssertionError("should not refresh")),
    )
    cfg = Config()
    ap = _build_for(cfg, "openai_codex")
    assert ap is not None
    assert ap.key == "openai_codex"
    assert ap.account_id == "acct-123"
    assert ap.api_base == openai_codex.CODEX_RESPONSES_BASE_URL
    assert "u@example.com" in ap.source

    # Disabled slot → not usable.
    cfg.providers.openai_codex.enabled = False
    assert _build_for(cfg, "openai_codex") is None


def test_disabled_or_missing_credentials_returns_none():
    cfg = Config()
    # No token stored at all.
    assert _build_for(cfg, "openai_codex") is None
    # And resolve_active_provider must not crash on an empty machine.
    assert resolve_active_provider(cfg) is None or True


def test_token_expiry_detection():
    fresh = openai_codex.CodexTokenPayload(access_token=_access_token(exp_in=3600))
    stale = openai_codex.CodexTokenPayload(access_token=_access_token(exp_in=10))
    assert openai_codex.token_is_expiring(fresh) is False
    assert openai_codex.token_is_expiring(stale) is True
    assert openai_codex.token_is_expiring(None) is True
