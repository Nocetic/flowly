"""``flowly login`` must not claim a provider it didn't get.

``ensure_account_key`` mints the ``flw_…`` key that bills LLM usage to the
account. It is best-effort by design and returns False on any failure — a
502 from the backend, a dropped connection, an expired token. Nothing looked
at that return value, so a sign-in whose mint failed still printed

    ✓ Signed in as you@example.com — Flowly provider ready, billed to your
      account (no relay).
    Ready. Run flowly to start chatting.

and the user then found the gateway refusing to start. These tests hold the
final message to what actually happened.
"""

from __future__ import annotations

import pytest

import flowly.cli.login_cmd as login_cmd
from flowly.integrations.active_provider import ProviderReadiness


def _readiness(monkeypatch: pytest.MonkeyPatch, ready: bool) -> None:
    monkeypatch.setattr(
        login_cmd,
        "provider_readiness",
        lambda: ProviderReadiness(ready=ready, provider=None, has_account=True),
    )


def test_a_working_sign_in_says_ready(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _readiness(monkeypatch, ready=True)

    assert login_cmd._print_provider_verdict() is True

    out = capsys.readouterr().out
    assert "Ready" in out
    assert "flowly" in out


def test_a_failed_mint_is_reported_instead_of_claimed(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _readiness(monkeypatch, ready=False)

    assert login_cmd._print_provider_verdict() is False

    out = capsys.readouterr().out
    assert "Ready. Run" not in out
    # Must name the actual repair, not send them back through setup.
    assert "flowly login" in out
    lowered = out.lower()
    assert "signed in" in lowered
    assert "account key" in lowered


def test_repair_reissues_the_account_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``flowly login --repair`` is where a user lands after a failed mint.

    It re-registered the server and rewrote relay config but never retried
    the mint, so the one thing that was actually broken stayed broken.
    """
    minted: list[str] = []
    monkeypatch.setattr(
        login_cmd,
        "_mint_and_save_account_key",
        lambda account: minted.append(getattr(account, "user_id", "?")) or True,
    )

    class _Account:
        user_id = "uid-1"
        email = "someone@example.com"

    login_cmd._repair_account_credentials(_Account())

    assert minted == ["uid-1"]
