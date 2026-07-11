"""/usage — catalog pricing lookup, the inline body builder, credit fetch
safety, and the inline UsagePanel (mounts, populates, Esc-dismisses).

/usage is an INLINE panel mounted into the composer (not a ModalScreen overlay),
so it must mount as a plain widget and post ``Dismissed`` on Esc.
"""

from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.widgets import Static

from flowly.integrations import model_catalog as mc
from flowly.integrations.model_catalog import Model, get_pricing
from flowly.tui.panes.composer import Composer
from flowly.tui.panes.usage_panel import UsagePanel, build_usage_body


def _totals():
    return {
        "input": 128_000, "output": 12_400, "cache_read": 96_000,
        "cache_write": 4_000, "turns": 6, "cost_usd": 0.9525, "cost_known": 6,
    }


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


def test_get_pricing_dash_dot_normalization():
    mc._CACHE["test"] = [Model(
        id="anthropic/claude-opus-4.8", name="Opus",
        context_window=200_000, pricing_in=5.0, pricing_out=25.0)]
    try:
        assert get_pricing("anthropic/claude-opus-4.8") == (5.0, 25.0)
        assert get_pricing("anthropic/claude-opus-4-8") == (5.0, 25.0)  # dash form
        assert get_pricing("unknown/model") is None
        assert get_pricing("") is None
    finally:
        mc._CACHE.pop("test", None)


def test_body_contains_cost_tokens_context_and_credits():
    body = build_usage_body(
        totals=_totals(), model="anthropic/claude-opus-4.8", provider="flowly",
        ctx_used=140_400, ctx_budget=200_000, elapsed=325,
        account_email="me@x.com", credits=_CREDIT_INFO,
    )
    assert "$0.9525" in body                       # estimated cost
    assert "input" in body and "cache read" in body
    assert "70%" in body                            # 140.4k / 200k context bar
    assert "363" in body and "27% used" in body     # live credits
    assert "plan pro" in body and "renews 2026-08-01" in body


def test_body_degrades_without_price_or_account():
    body = build_usage_body(
        totals={**_totals(), "cost_known": 0}, model="byok/native",
        provider="anthropic", ctx_used=0, ctx_budget=0, elapsed=1,
        account_email=None, credits=None,
    )
    assert "n/a" in body                            # no catalog price
    assert "Not signed in" in body
    assert "context window unknown" in body


@pytest.mark.asyncio
async def test_fetch_account_credits_is_safe_without_token():
    from flowly.account.billing import fetch_account_credits

    class _NoToken:
        id_token = ""

    assert await fetch_account_credits(_NoToken()) is None
    assert await fetch_account_credits(None) is None


@pytest.mark.asyncio
async def test_usage_panel_mounts_populates_and_dismisses():
    dismissed: list[bool] = []

    class _Host(App):
        def compose(self):
            yield UsagePanel(id="composer-usage")

        @on(UsagePanel.Dismissed)
        def _on_dismissed(self) -> None:
            dismissed.append(True)

    app = _Host()
    async with app.run_test(size=(90, 40)) as pilot:
        panel = app.query_one(UsagePanel)
        panel.set_data(
            totals=_totals(), model="anthropic/claude-opus-4.8", provider="openrouter",
            ctx_used=140_400, ctx_budget=200_000, elapsed=5,
            account_email=None, credits=None,
        )
        await pilot.pause()
        body = str(app.query_one("#usage-body", Static).render())
        assert "Session" in body and "Context window" in body
        await pilot.press("escape")
        await pilot.pause()
        assert dismissed == [True]           # Esc posts Dismissed → app closes it


@pytest.mark.asyncio
async def test_composer_usage_replaces_input_row():
    class _Host(App):
        def compose(self):
            yield Composer()

        @on(UsagePanel.Dismissed)
        def _on_dismissed(self, event: UsagePanel.Dismissed) -> None:
            event.stop()
            self.query_one(Composer).clear_usage()

    app = _Host()
    async with app.run_test(size=(90, 40)) as pilot:
        composer = app.query_one(Composer)
        composer.show_usage(
            totals=_totals(), model="anthropic/claude-opus-4.8", provider="openrouter",
            ctx_used=140_400, ctx_budget=200_000, elapsed=5,
            account_email=None, credits=None,
        )
        await pilot.pause()

        assert composer.has_class("usage-open")
        assert not app.query_one("#composer-input-row").display
        assert app.focused is app.query_one(UsagePanel)

        await pilot.press("escape")
        await pilot.pause()

        assert not composer.has_class("usage-open")
