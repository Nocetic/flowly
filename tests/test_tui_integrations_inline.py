from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.widgets import OptionList

from flowly.integrations import IntegrationCard
from flowly.tui.panes.composer import Composer
from flowly.tui.panes.inline_picker import picker_width_for_columns
from flowly.tui.panes.integrations_modal import IntegrationsPanel


def _card(key: str, label: str, category: str = "tool") -> IntegrationCard:
    return IntegrationCard(
        key=key,
        label=label,
        category=category,  # type: ignore[arg-type]
        description=f"{label} integration",
        docs_url="",
        config_path=f"integrations.{key}",
        probe=None,
    )


@pytest.mark.asyncio
async def test_integrations_panel_is_plain_widget_and_dismisses(monkeypatch) -> None:
    monkeypatch.setattr(
        "flowly.tui.panes.integrations_modal.list_cards",
        lambda: [_card("linear", "Linear"), _card("trello", "Trello")],
    )
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield IntegrationsPanel(categories=("tool",))

        @on(IntegrationsPanel.Dismissed)
        def _on_dismissed(self, event: IntegrationsPanel.Dismissed) -> None:
            dismissed.append(event.result)

    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        assert app.focused is app.query_one("#integrations-list", OptionList)

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [None]


@pytest.mark.asyncio
async def test_integrations_panel_selects_highlighted_card(monkeypatch) -> None:
    monkeypatch.setattr(
        "flowly.tui.panes.integrations_modal.list_cards",
        lambda: [_card("linear", "Linear"), _card("trello", "Trello")],
    )
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield IntegrationsPanel(categories=("tool",))

        @on(IntegrationsPanel.Dismissed)
        def _on_dismissed(self, event: IntegrationsPanel.Dismissed) -> None:
            dismissed.append(event.result)

    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert dismissed == [{"action": "opened", "key": "linear"}]


@pytest.mark.asyncio
async def test_integrations_panel_uses_composer_inline_prompt_surface(monkeypatch) -> None:
    monkeypatch.setattr(
        "flowly.tui.panes.integrations_modal.list_cards",
        lambda: [_card("linear", "Linear"), _card("trello", "Trello")],
    )

    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 30)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(
            IntegrationsPanel(categories=("tool",)),
            inline=True,
        )
        await pilot.pause()

        picker_host = app.query_one("#composer-picker")
        panel = app.query_one(IntegrationsPanel)
        input_row = app.query_one("#composer-input-row")

        assert composer.has_class("picker-inline-open")
        assert not composer.has_class("picker-floating-open")
        assert not input_row.display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width > picker_width_for_columns(app.size.width)
        assert app.focused is app.query_one("#integrations-list", OptionList)
