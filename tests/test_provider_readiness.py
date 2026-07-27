"""One definition of "can Flowly serve a request right now?".

Two call sites used to answer this differently and the disagreement was a
trap a first-run user could fall into and not get out of:

* ``onboard_cmd._already_configured()`` counted a signed-in Flowly account
  as configured, even when no usable credential ever landed in the config
  (``ensure_account_key`` is best-effort and returns False silently).
* ``service_cmd._provider_configured()`` asked ``resolve_active_provider``,
  which in that state answers "nothing usable".

So onboarding said "you're set", ``service install --start`` refused to
start the gateway, and bare ``flowly`` — seeing no provider — reopened
onboarding. Round and round.

These tests pin the two answers together and keep the in-between state
("signed in, nothing usable") nameable so the UI can say what happened
instead of looping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowly.config.loader import convert_to_camel
from flowly.config.schema import Config
from flowly.integrations.active_provider import provider_readiness


def _write_config(home: Path, config: Config) -> None:
    raw = convert_to_camel(config.model_dump())
    path = home / "config.json"
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated FLOWLY_HOME with no account and no credentials.

    The subscription providers (ChatGPT/Codex, xAI Grok, GLM) deliberately
    reuse credentials that live OUTSIDE ``FLOWLY_HOME`` — the user's Codex
    or OpenCode login. That is a feature, but it means a developer machine
    resolves a provider from an otherwise empty config, so these tests
    silence them to keep the cascade under test hermetic.
    """
    flowly_home = tmp_path / "flowly-home"
    flowly_home.mkdir(mode=0o700)
    monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))
    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: None)
    for module in ("openai_codex", "xai_oauth", "zai_coding"):
        monkeypatch.setattr(
            f"flowly.auth.{module}.resolve_runtime_credentials",
            lambda *_args, **_kwargs: None,
        )
    return flowly_home


def _sign_in(monkeypatch: pytest.MonkeyPatch, **fields) -> None:
    """Pretend an account is stored, with whatever credentials are given."""
    from flowly.account.auth import Account

    account = Account(
        user_id="uid-1",
        email="someone@example.com",
        id_token="id-token",
        refresh_token="refresh-token",
        expires_at=4_102_444_800,
        machine_id="machine-1",
        machine_name="test-machine",
        **fields,
    )
    monkeypatch.setattr("flowly.account.auth.load_account_sync", lambda: account)


def test_byok_key_is_ready(home: Path) -> None:
    config = Config()
    config.providers.active = "openrouter"
    config.providers.openrouter.api_key = "sk-or-v1-test"
    _write_config(home, config)

    state = provider_readiness()

    assert state.ready is True
    assert state.has_account is False
    assert state.provider is not None
    assert state.provider.key == "openrouter"


def test_signed_in_but_mint_failed_is_not_ready(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact state a failed ``ensure_account_key`` leaves behind.

    ``providers.flowly.enabled`` defaults True, so the config *looks* like
    the hosted provider is on — but there is no account key, no relay pair
    and no server registration, so nothing can actually be billed.
    """
    _write_config(home, Config())
    _sign_in(monkeypatch, server_id="", gateway_auth_token="")

    state = provider_readiness()

    assert state.ready is False
    assert state.has_account is True
    assert state.provider is None


def test_signed_in_with_relay_registration_is_ready(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(home, Config())
    _sign_in(monkeypatch, server_id="srv-1", gateway_auth_token="tok-1")

    state = provider_readiness()

    assert state.ready is True
    assert state.provider is not None
    assert state.provider.key == "flowly"


def test_nothing_configured_at_all(home: Path) -> None:
    _write_config(home, Config())

    state = provider_readiness()

    assert state.ready is False
    assert state.has_account is False


def test_onboarding_and_service_never_disagree(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression guard for the loop this whole module exists to prevent.

    Whatever the state, the screen that decides "setup is done" and the
    command that decides "the gateway may start" must agree — otherwise the
    user is told they are ready and then refused.
    """
    from flowly.cli.onboard_cmd import _already_configured
    from flowly.cli.service_cmd import _provider_configured

    # 1. Nothing at all.
    _write_config(home, Config())
    assert _already_configured() == _provider_configured() is False

    # 2. Signed in, but the account key never minted — the trap.
    _sign_in(monkeypatch, server_id="", gateway_auth_token="")
    assert _already_configured() == _provider_configured() is False

    # 3. Signed in with a working account key.
    config = Config()
    config.providers.flowly.account_key = "flw_test"
    _write_config(home, config)
    assert _already_configured() == _provider_configured() is True
