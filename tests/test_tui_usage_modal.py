"""/usage screen: catalog pricing lookup + the UsageModal renders and dismisses.

Regression guard for two things that bit during build:
  1. The modal method must NOT be named ``_render`` — that shadows Textual's
     ``Widget._render`` and crashes rendering with
     ``'str' object has no attribute 'render_strips'``.
  2. ``get_pricing`` must normalize dash/dot model-version ids like
     ``get_context_window`` so cost resolves regardless of config form.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Static

from flowly.integrations import model_catalog as mc
from flowly.integrations.model_catalog import Model, get_pricing
from flowly.tui.panes.usage_modal import UsageModal


def _seed_catalog():
    mc._CACHE["test"] = [
        Model(
            id="anthropic/claude-opus-4.8", name="Opus",
            context_window=200_000, pricing_in=5.0, pricing_out=25.0,
        )
    ]


def test_get_pricing_dash_dot_normalization():
    _seed_catalog()
    try:
        assert get_pricing("anthropic/claude-opus-4.8") == (5.0, 25.0)
        # dash form (LiteLLM config convention) must resolve to the same row
        assert get_pricing("anthropic/claude-opus-4-8") == (5.0, 25.0)
        assert get_pricing("unknown/model") is None
        assert get_pricing("") is None
    finally:
        mc._CACHE.pop("test", None)


def _totals():
    return {
        "input": 128_000, "output": 12_400, "cache_read": 96_000,
        "cache_write": 4_000, "turns": 6, "cost_usd": 0.9525, "cost_known": 6,
    }


def test_usage_body_contains_cost_and_tokens():
    modal = UsageModal(
        totals=_totals(), model="anthropic/claude-opus-4.8", provider="openrouter",
        ctx_used=140_400, ctx_budget=200_000, elapsed=325, account_email="a@b.com",
    )
    body = modal._body()
    assert "$0.9525" in body          # estimated cost rendered
    assert "input" in body and "cache read" in body
    assert "70%" in body              # 140.4k / 200k context bar
    assert "a@b.com" in body          # account line


def test_usage_body_no_cost_when_price_unknown():
    t = _totals()
    t["cost_known"] = 0
    modal = UsageModal(
        totals=t, model="byok/native", provider="anthropic",
        ctx_used=0, ctx_budget=0, elapsed=1, account_email=None,
    )
    body = modal._body()
    assert "n/a" in body                      # no catalog price → no fake cost
    assert "Not signed in" in body            # signed-out account line
    assert "context window unknown" in body   # no budget → no bar


_CREDIT_INFO = {
    "credits": {
        "plan": {"total": 500, "used": 137, "remaining": 363},
        "bonus": {"total": 50, "used": 0, "remaining": 50},
        "available": 413, "total": 500, "used": 137, "remaining": 363,
        "percentUsed": 27.4, "periodStart": "2026-07-01",
        "periodEnd": "2026-08-01T00:00:00Z",
    },
    "plan": {"id": "pro", "monthlyCredits": 500, "maxServers": 3},
}


def test_usage_body_shows_flowly_credits_when_signed_in():
    body = UsageModal(
        totals=_totals(), model="anthropic/claude-opus-4.8", provider="flowly",
        ctx_used=140_400, ctx_budget=200_000, elapsed=1,
        account_email="me@x.com", credits=_CREDIT_INFO,
    )._body()
    assert "363" in body and "500" in body      # remaining / total credits
    assert "27% used" in body                    # percentUsed
    assert "plan pro" in body                     # plan id
    assert "renews 2026-08-01" in body            # period end (date only)


def test_usage_body_degrades_when_credits_missing():
    body = UsageModal(
        totals=_totals(), model="x/y", provider="openrouter",
        ctx_used=1, ctx_budget=200_000, elapsed=1,
        account_email="me@x.com", credits=None,
    )._body()
    assert "Signed in as" in body
    assert "unavailable" in body                  # no fake zeros


@pytest.mark.asyncio
async def test_fetch_account_credits_is_safe_without_token():
    from flowly.account.billing import fetch_account_credits

    class _NoToken:
        id_token = ""

    assert await fetch_account_credits(_NoToken()) is None
    assert await fetch_account_credits(None) is None


@pytest.mark.asyncio
async def test_usage_modal_mounts_and_dismisses():
    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(UsageModal(
                totals=_totals(), model="anthropic/claude-opus-4.8",
                provider="openrouter", ctx_used=140_400, ctx_budget=200_000,
                elapsed=325, account_email=None,
            ))

    app = _Host()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        assert len(app.screen.query(Static)) >= 1   # rendered, no render_strips crash
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1            # modal dismissed
