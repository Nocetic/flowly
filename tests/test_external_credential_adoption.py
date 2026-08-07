"""Credentials Flowly reads from another tool must be chosen, not assumed.

``openai_codex`` falls back to the Codex CLI's ``~/.codex/auth.json`` and
``zai_coding`` to OpenCode's ``auth.json``, so "already signed in over there"
needs no extra step. The cost of that convenience was silence: on such a
machine a brand-new Flowly resolved a provider through the cascade, onboarding
saw "configured" and skipped itself, and the user ended up billed to an
account nobody named. (Reproduced on a real machine: a fresh, isolated
FLOWLY_HOME reported ready with a ChatGPT subscription.)

The rule these tests hold: adoption is visible while it is implicit, and once
the user answers — which writes ``providers.active`` — the question is over.
"""

from __future__ import annotations

import pytest

from flowly.config.schema import Config
from flowly.integrations import active_provider as ap
from flowly.integrations.active_provider import ActiveProvider


def _codex_active(config):
    return ActiveProvider(
        key="openai_codex", api_key="tok", api_base=None, source="ChatGPT subscription"
    )


def test_credential_from_the_codex_cli_is_reported(monkeypatch):
    cfg = Config()
    cfg.providers.active = ""
    monkeypatch.setattr(ap, "resolve_active_provider", _codex_active)
    monkeypatch.setattr("flowly.auth.openai_codex._payload_source", lambda: "codex_cli")

    found = ap.external_credential_in_use(cfg)
    assert found is not None
    assert found.key == "openai_codex"
    assert "Codex CLI" in found.origin


def test_flowlys_own_codex_login_is_not_an_adoption(monkeypatch):
    """`flowly codex login` writes Flowly's own store — the user chose it."""
    cfg = Config()
    cfg.providers.active = ""
    monkeypatch.setattr(ap, "resolve_active_provider", _codex_active)
    monkeypatch.setattr("flowly.auth.openai_codex._payload_source", lambda: "flowly")

    assert ap.external_credential_in_use(cfg) is None


def test_an_explicit_choice_silences_the_question(monkeypatch):
    """Answering the prompt sets providers.active — it must never ask twice."""
    cfg = Config()
    cfg.providers.active = "openai_codex"
    monkeypatch.setattr(ap, "resolve_active_provider", _codex_active)
    monkeypatch.setattr("flowly.auth.openai_codex._payload_source", lambda: "codex_cli")

    assert ap.external_credential_in_use(cfg) is None


def test_ordinary_byok_is_never_an_adoption(monkeypatch):
    cfg = Config()
    cfg.providers.active = ""
    monkeypatch.setattr(
        ap,
        "resolve_active_provider",
        lambda config: ActiveProvider(
            key="openrouter", api_key="sk-or-x", api_base=None, source="BYOK"
        ),
    )
    assert ap.external_credential_in_use(cfg) is None


def test_nothing_configured_reports_nothing(monkeypatch):
    cfg = Config()
    cfg.providers.active = ""
    monkeypatch.setattr(ap, "resolve_active_provider", lambda config: None)
    assert ap.external_credential_in_use(cfg) is None


def test_a_broken_probe_never_blocks_first_run(monkeypatch):
    cfg = Config()
    cfg.providers.active = ""
    monkeypatch.setattr(ap, "resolve_active_provider", _codex_active)

    def _boom():
        raise RuntimeError("keyring on fire")

    monkeypatch.setattr("flowly.auth.openai_codex._payload_source", _boom)
    assert ap.external_credential_in_use(cfg) is None


@pytest.mark.parametrize("source,expected", [("opencode", True), ("flowly", False)])
def test_opencode_credential_follows_the_same_rule(monkeypatch, source, expected):
    cfg = Config()
    cfg.providers.active = ""
    monkeypatch.setattr(
        ap,
        "resolve_active_provider",
        lambda config: ActiveProvider(
            key="zai_coding", api_key="k", api_base=None, source="GLM"
        ),
    )

    class _Payload:
        pass

    payload = _Payload()
    payload.source = source
    monkeypatch.setattr("flowly.auth.zai_coding.load_token_payload", lambda: payload)

    found = ap.external_credential_in_use(cfg)
    assert (found is not None) is expected
