from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import OptionList

from flowly.integrations.plugins_io import PluginEntry
from flowly.tui.panes import plugins_modal as plugins_mod
from flowly.tui.panes.composer import Composer
from flowly.tui.panes.plugins_modal import PluginsModal, PluginsPanel


def _plugin(key: str, *, enabled: bool = True) -> PluginEntry:
    return PluginEntry(
        key=key,
        name=key.title(),
        version="1.0.0",
        description=f"{key} tools",
        source="bundled",
        kind="tools",
        enabled=enabled,
        error=None,
        status="enabled" if enabled else "disabled",
    )


@pytest.mark.asyncio
async def test_plugins_panel_is_plain_widget_focuses_list_and_dismisses(monkeypatch) -> None:
    monkeypatch.setattr(plugins_mod, "list_plugins", lambda: [_plugin("workspace")])
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield PluginsPanel()

        @on(PluginsPanel.Dismissed)
        def _on_dismissed(self, event: PluginsPanel.Dismissed) -> None:
            dismissed.append(event.result)

    assert not issubclass(PluginsPanel, ModalScreen)
    assert issubclass(PluginsModal, ModalScreen)

    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one("#plugins-list", OptionList)

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [None]


@pytest.mark.asyncio
async def test_plugins_panel_reports_change_count(monkeypatch) -> None:
    monkeypatch.setattr(plugins_mod, "list_plugins", lambda: [_plugin("workspace")])
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield PluginsPanel()

        @on(PluginsPanel.Dismissed)
        def _on_dismissed(self, event: PluginsPanel.Dismissed) -> None:
            dismissed.append(event.result)

    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        panel = app.query_one(PluginsPanel)
        panel._changes = 2
        await pilot.press("q")
        await pilot.pause()

    assert dismissed == [{"action": "changed", "count": 2}]


@pytest.mark.asyncio
async def test_plugins_panel_uses_composer_inline_prompt_surface(monkeypatch) -> None:
    monkeypatch.setattr(
        plugins_mod,
        "list_plugins",
        lambda: [_plugin("workspace"), _plugin("memory", enabled=False)],
    )

    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 34)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(PluginsPanel(), inline=True)
        await pilot.pause()

        panel = app.query_one(PluginsPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert app.focused is app.query_one("#plugins-list", OptionList)

