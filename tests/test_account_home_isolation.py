"""Multi-home credential isolation.

``FLOWLY_HOME`` lets a second engine built on this codebase (or a named
profile) run fully isolated from the default ``~/.flowly`` install. Four
places used to leak across homes because they didn't consult it:

1. ``account/token_store.py`` — the personal-account keychain service name
   and file fallback path were hardcoded to ``~/.flowly``, so a second home
   would silently read/write the *default* home's Flowly account.
2. ``auth/openai_codex.py`` / ``auth/xai_oauth.py`` / ``auth/zai_coding.py``
   — the file fallback path was already home-scoped, but the OS-keychain
   *service name* wasn't, so a working keychain would still let two homes
   share one entry (ChatGPT subscription, xAI Grok OAuth, GLM Coding Plan).
3. ``channels/feature_rpc.py`` — ``xai_oauth``/``openai_codex`` were absent
   from ``_PROVIDER_SLOTS`` entirely, so Desktop/iOS never saw them.

Every fix here must be a no-op at the DEFAULT home (no existing user
re-logs-in) and only change behavior once ``FLOWLY_HOME`` is non-default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowly import profile
from flowly.account import token_store
from flowly.auth import openai_codex, xai_oauth, zai_coding
from flowly.channels import feature_rpc


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch: pytest.MonkeyPatch):
    """Force the file-fallback path everywhere — deterministic, no OS dialog."""
    monkeypatch.setattr(token_store, "_try_keyring", lambda: None)
    monkeypatch.setattr(openai_codex, "_try_keyring", lambda: None)
    monkeypatch.setattr(xai_oauth, "_try_keyring", lambda: None)
    monkeypatch.setattr(zai_coding, "_try_keyring", lambda: None)


# ── profile.py: the shared scoping primitives ──────────────────────────


def test_default_home_has_no_scope_suffix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FLOWLY_HOME", raising=False)
    assert profile.is_default_home() is True
    assert profile.credential_scope_suffix() == ""


def test_non_default_home_gets_a_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-code"))
    assert profile.is_default_home() is False
    suffix = profile.credential_scope_suffix()
    assert suffix and len(suffix) == 12


def test_suffix_is_deterministic_and_home_specific(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "a"))
    suffix_a1 = profile.credential_scope_suffix()
    suffix_a2 = profile.credential_scope_suffix()
    assert suffix_a1 == suffix_a2  # stable across calls

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "b"))
    suffix_b = profile.credential_scope_suffix()
    assert suffix_b != suffix_a1  # different home -> different suffix


# ── token_store.py: default home is byte-identical to pre-fix behavior ──


def test_token_store_default_home_matches_historical_constants(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FLOWLY_HOME", raising=False)
    assert token_store._service_name() == "flowly-tui"
    assert token_store._fallback_path() == Path.home() / ".flowly" / "credentials" / "account.json"


def test_token_store_non_default_home_is_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    custom_home = tmp_path / "flowly-code"
    monkeypatch.setenv("FLOWLY_HOME", str(custom_home))
    suffix = profile.credential_scope_suffix()
    assert token_store._service_name() == f"flowly-tui:{suffix}"
    assert token_store._fallback_path() == custom_home / "credentials" / "account.json"


def test_token_store_file_roundtrip_stays_under_custom_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    custom_home = tmp_path / "flowly-code"
    monkeypatch.setenv("FLOWLY_HOME", str(custom_home))

    status = token_store.save_credentials({"id_token": "second-engine-token"})
    assert status.backend == "file"
    assert token_store.load_credentials() == {"id_token": "second-engine-token"}
    assert (custom_home / "credentials" / "account.json").exists()


def test_legacy_migration_is_a_noop_outside_default_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A second home must never read, migrate, or delete the default home's
    legacy plaintext account file — it belongs to a different install."""
    legacy = tmp_path / "personal-legacy-account.json"
    legacy.write_text(json.dumps({"id_token": "personal-account-token"}), encoding="utf-8")
    monkeypatch.setattr(token_store, "LEGACY_PATH", legacy)
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-code"))

    result = token_store._migrate_legacy_file_to_keyring(None)

    assert result is None
    assert legacy.exists()
    assert json.loads(legacy.read_text())["id_token"] == "personal-account-token"


def test_legacy_purge_is_a_noop_outside_default_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    legacy = tmp_path / "personal-legacy-account.json"
    legacy.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(token_store, "LEGACY_PATH", legacy)
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-code"))

    token_store._purge_legacy_file()

    assert legacy.exists()  # untouched — not this home's file to delete


def test_legacy_purge_runs_at_the_default_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    legacy = tmp_path / "legacy-account.json"
    legacy.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(token_store, "LEGACY_PATH", legacy)
    monkeypatch.delenv("FLOWLY_HOME", raising=False)

    token_store._purge_legacy_file()

    assert not legacy.exists()  # purge IS allowed at the default home


# ── openai_codex.py / xai_oauth.py / zai_coding.py: keychain scoping ───


def test_oauth_keyring_service_default_home_is_unchanged(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FLOWLY_HOME", raising=False)
    assert openai_codex._keyring_service() == "flowly-tui"
    assert xai_oauth._keyring_service() == "flowly-tui"
    assert zai_coding._keyring_service() == "flowly-tui"


def test_oauth_keyring_service_non_default_home_is_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-code"))
    suffix = profile.credential_scope_suffix()
    expected = f"flowly-tui:{suffix}"
    assert openai_codex._keyring_service() == expected
    assert xai_oauth._keyring_service() == expected
    assert zai_coding._keyring_service() == expected
    # Same suffix for all three (same home) — distinguished by
    # _KEYRING_ACCOUNT, not by service name, so no collision between the
    # three providers themselves.
    assert openai_codex._keyring_service() == xai_oauth._keyring_service() == zai_coding._keyring_service()


def test_zai_coding_file_roundtrip_stays_under_custom_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    custom_home = tmp_path / "flowly-code"
    monkeypatch.setenv("FLOWLY_HOME", str(custom_home))
    # OpenCode's external auth.json is a foreign, singleton store (like
    # ~/.codex/auth.json) — isolate it too so this test doesn't pick up a
    # real OpenCode session on the dev machine running it.
    monkeypatch.setenv("OPENCODE_AUTH_PATH", str(tmp_path / "no-opencode-here.json"))

    status = zai_coding.save_api_key("test-glm-key", base_url=zai_coding.DEFAULT_ZAI_CODING_BASE_URL)
    assert status.startswith("file:")
    payload = zai_coding.load_token_payload()
    assert payload is not None
    assert payload.api_key == "test-glm-key"
    assert (custom_home / "credentials" / "zai_coding.json").exists()


# ── feature_rpc.py: subscription providers are visible to Desktop/iOS ──


def test_provider_slots_include_oauth_subscriptions():
    keys = [key for key, _, _ in feature_rpc._PROVIDER_SLOTS]
    assert "xai_oauth" in keys
    assert "openai_codex" in keys


def test_provider_list_surfaces_oauth_subscriptions_as_non_keyable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    # openai_codex also falls back to a `codex login` session in CODEX_HOME
    # (by design) — isolate it too so this "nothing configured" case is
    # deterministic instead of picking up a real Codex CLI session on the
    # dev machine running this test.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-cli-home"))
    result = feature_rpc.provider_list()
    by_key = {p["key"]: p for p in result["providers"]}

    assert "xai_oauth" in by_key
    assert by_key["xai_oauth"]["keyable"] is False
    assert by_key["xai_oauth"]["hasKey"] is False  # no token in this fresh tmp home

    assert "openai_codex" in by_key
    assert by_key["openai_codex"]["keyable"] is False
    assert by_key["openai_codex"]["hasKey"] is False


def test_provider_list_reflects_a_connected_codex_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    # Plant a fake-but-shaped token so resolve_runtime_credentials succeeds
    # without a network call (token far from expiry).
    import base64
    import time as _time

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    def _jwt(claims: dict) -> str:
        header = _b64url(json.dumps({"alg": "none"}).encode())
        payload = _b64url(json.dumps(claims).encode())
        return f"{header}.{payload}.sig"

    access = _jwt({
        "exp": int(_time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
    })
    id_token = _jwt({
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-test"},
    })
    openai_codex.save_token_payload(openai_codex.CodexTokenPayload(
        access_token=access, refresh_token="r", id_token=id_token,
        account_id="acct-test",
    ))

    result = feature_rpc.provider_list()
    by_key = {p["key"]: p for p in result["providers"]}
    assert by_key["openai_codex"]["hasKey"] is True
