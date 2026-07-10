from __future__ import annotations

import asyncio
import time

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import Static

from flowly.account.auth import Account
from flowly.tui.panes import login_modal as login_mod
from flowly.tui.panes.composer import Composer
from flowly.tui.panes.login_modal import LoginModal, LoginPanel


def _account() -> Account:
    return Account(
        user_id="user-1",
        email="user@example.com",
        id_token="id-token",
        refresh_token="refresh-token",
        expires_at=time.time() + 3600,
        machine_id="machine-1",
        machine_name="Test machine",
    )


@pytest.mark.asyncio
async def test_login_panel_is_plain_widget_and_cancels_pending_flow(monkeypatch) -> None:
    started = asyncio.Event()

    async def pending_login(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(login_mod, "run_login_flow", pending_login)
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield LoginPanel()

        @on(LoginPanel.Dismissed)
        def _on_dismissed(self, event: LoginPanel.Dismissed) -> None:
            dismissed.append(event.result)

    assert not issubclass(LoginPanel, ModalScreen)
    assert issubclass(LoginModal, ModalScreen)

    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await started.wait()
        assert app.focused is app.query_one(LoginPanel)

        await pilot.press("r")
        await pilot.pause()
        assert "[x] Remote" in str(app.query_one("#login-relay", Static).render())

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [None]


@pytest.mark.asyncio
async def test_login_panel_completes_inline_device_flow(monkeypatch) -> None:
    account = _account()

    async def successful_login(*, on_code, on_status):
        on_code("ABCDEF", "https://example.test/device")
        on_status("waiting for authorization")
        return account

    monkeypatch.setattr(login_mod, "run_login_flow", successful_login)
    monkeypatch.setattr(login_mod, "_open_browser_detached", lambda _url: True)
    monkeypatch.setattr(
        "flowly.account.account_key.ensure_account_key",
        lambda _account: None,
    )
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield LoginPanel()

        @on(LoginPanel.Dismissed)
        def _on_dismissed(self, event: LoginPanel.Dismissed) -> None:
            dismissed.append(event.result)

    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        assert "ABC-DEF" in str(app.query_one("#login-code", Static).render())
        assert "Enter done" in str(app.query_one("#login-footer", Static).render())

        await pilot.press("enter")
        await pilot.pause()

    assert dismissed == [account]


@pytest.mark.asyncio
async def test_login_panel_uses_composer_inline_prompt_surface(monkeypatch) -> None:
    async def pending_login(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(login_mod, "run_login_flow", pending_login)

    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 34)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(LoginPanel(), inline=True)
        await pilot.pause()

        panel = app.query_one(LoginPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert app.focused is panel
