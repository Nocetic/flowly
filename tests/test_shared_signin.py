"""Subscription sign-ins are the computer's (flowly/auth/shared_signin.py).

Before: a ChatGPT or Grok sign-in lived in the home of the agent that made it,
so every named agent showed it as "main agent only" and could not run on it;
and a refresh token the issuer rejected wiped the store, so two agents
refreshing at once would have logged each other out. Now every agent reads the
main agent's store, refreshes one at a time under a computer-wide lock, and a
rejected token only clears a store that still holds it.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

import flowly.profile as profiles
from flowly.auth import openai_codex, shared_signin, xai_oauth


@pytest.fixture
def homes(tmp_path, monkeypatch):
    default = tmp_path / ".flowly"
    root = default / "profiles"
    (root / "writer").mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    # File stores only: the keychain is the machine's, not the test's.
    monkeypatch.setattr(openai_codex, "_try_keyring", lambda: None)
    monkeypatch.setattr(xai_oauth, "_try_keyring", lambda: None)
    monkeypatch.setattr(openai_codex, "codex_home_dir", lambda: tmp_path / "no-codex-cli")
    return default, root / "writer"


def _be(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(home))


def _codex(refresh: str, *, expires_in: int = 3600) -> openai_codex.CodexTokenPayload:
    return openai_codex.CodexTokenPayload(access_token=f"access-{refresh}", refresh_token=refresh,
                                          account_id="acct-1", expires_at=int(time.time()) + expires_in)


def test_a_named_agent_uses_the_main_agents_home_and_keychain_entry(homes, monkeypatch):
    default, writer = homes
    _be(monkeypatch, writer)
    assert shared_signin.signin_home() == default
    assert shared_signin.signin_scope_suffix() == ""
    assert openai_codex._keyring_service() == "flowly-tui" and xai_oauth._keyring_service() == "flowly-tui"
    assert openai_codex.credentials_path() == default / "credentials" / "openai_codex.json"


def test_another_products_home_keeps_its_own_store(tmp_path, homes, monkeypatch):
    other = tmp_path / "other-product"
    _be(monkeypatch, other)
    assert shared_signin.signin_home() == other
    assert shared_signin.signin_scope_suffix() != ""


def test_a_sign_in_made_on_the_main_agent_runs_every_named_agent(homes, monkeypatch):
    default, writer = homes
    _be(monkeypatch, default)
    openai_codex.save_token_payload(_codex("r1"))
    _be(monkeypatch, writer)
    creds = openai_codex.resolve_runtime_credentials()
    assert creds is not None and creds.api_key == "access-r1"
    assert not (writer / "credentials").exists()


def test_whoever_refreshes_second_uses_what_the_first_saved(homes, monkeypatch):
    default, writer = homes
    _be(monkeypatch, writer)
    openai_codex.save_token_payload(_codex("r1", expires_in=10))  # about to expire
    calls: list[str] = []
    real_load = openai_codex.load_token_payload

    def load():
        # Another agent refreshes while this one waits for the lock.
        if not calls:
            calls.append("first read")
            stale = real_load()
            openai_codex.save_token_payload(_codex("r2"))
            return stale
        return real_load()

    def refresh(**_):
        raise AssertionError("refreshed again what another agent had just refreshed")

    monkeypatch.setattr(openai_codex, "load_token_payload", load)
    monkeypatch.setattr(openai_codex, "refresh_tokens", refresh)
    creds = openai_codex.resolve_runtime_credentials()
    assert creds is not None and creds.api_key == "access-r2"


def test_an_expiring_sign_in_is_refreshed_once_under_the_lock(homes, monkeypatch):
    _default, writer = homes
    _be(monkeypatch, writer)
    openai_codex.save_token_payload(_codex("r1", expires_in=10))
    held: list[bool] = []
    real_lock = shared_signin.refresh_lock

    def refresh(*, client_id, payload):
        held.append(True)
        updated = _codex("r2")
        openai_codex.save_token_payload(updated)
        return updated

    monkeypatch.setattr(openai_codex, "refresh_tokens", refresh)
    monkeypatch.setattr(openai_codex, "refresh_lock", lambda name: real_lock(name))
    creds = openai_codex.resolve_runtime_credentials()
    assert creds is not None and creds.api_key == "access-r2" and held == [True]
    assert (shared_signin.signin_home() / "credentials" / "openai_codex.lock").exists()


def _issuer_rejects(monkeypatch, module):
    class _Client:
        def __init__(self, *_, **__):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, url, **_):
            return httpx.Response(400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(module.httpx, "Client", _Client)


def test_a_rejected_token_keeps_a_sign_in_another_agent_refreshed_meanwhile(homes, monkeypatch):
    default, _writer = homes
    _be(monkeypatch, default)
    openai_codex.save_token_payload(_codex("r2"))  # another agent already rotated r1 to r2
    _issuer_rejects(monkeypatch, openai_codex)
    kept = openai_codex.refresh_tokens(payload=_codex("r1"))
    assert kept.refresh_token == "r2"
    assert openai_codex.load_token_payload().refresh_token == "r2"


def test_a_rejected_token_still_in_the_store_clears_it(homes, monkeypatch):
    default, _writer = homes
    _be(monkeypatch, default)
    openai_codex.save_token_payload(_codex("r1"))
    _issuer_rejects(monkeypatch, openai_codex)
    with pytest.raises(openai_codex.CodexAuthError, match="rejected"):
        openai_codex.refresh_tokens(payload=_codex("r1"))
    assert openai_codex.load_token_payload() is None


def test_grok_signs_in_once_for_the_computer_too(homes, monkeypatch):
    default, writer = homes
    _be(monkeypatch, writer)
    assert xai_oauth.credentials_path() == default / "credentials" / "xai_oauth.json"


@pytest.mark.asyncio
async def test_the_provider_list_says_sign_ins_are_the_computers(monkeypatch):
    from flowly.channels import feature_rpc
    from flowly.config.schema import Config

    monkeypatch.setattr("flowly.config.loader.load_config", lambda: Config())
    monkeypatch.setattr("flowly.integrations.active_provider.resolve_active_provider", lambda _cfg: None)
    monkeypatch.setattr("flowly.auth.openai_codex.resolve_runtime_credentials", lambda **_: None)
    monkeypatch.setattr("flowly.auth.xai_oauth.resolve_runtime_credentials", lambda **_: None)
    rows = {row["key"]: row for row in feature_rpc.provider_list()["providers"]}
    assert rows["openai_codex"]["shared"] is True and rows["xai_oauth"]["shared"] is True
    assert rows["anthropic"]["shared"] is False
