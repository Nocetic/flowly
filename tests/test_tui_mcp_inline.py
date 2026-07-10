from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import OptionList

from flowly.integrations.mcp_io import MCPServerEntry
from flowly.tui.panes import mcp_modal as mcp_mod
from flowly.tui.panes.composer import Composer
from flowly.tui.panes.mcp_modal import MCPModal, MCPPanel


def _server(name: str, *, enabled: bool = True) -> MCPServerEntry:
    return MCPServerEntry(
        name=name,
        transport=f"stdio: {name}",
        enabled=enabled,
        auth="",
        tool_filter="all",
        source="configured",
        description=f"{name} server",
        status="enabled" if enabled else "disabled",
    )


@pytest.mark.asyncio
async def test_mcp_panel_is_plain_widget_focuses_list_and_dismisses(monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "list_mcp_servers", lambda: [_server("context")])
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield MCPPanel()

        @on(MCPPanel.Dismissed)
        def _on_dismissed(self, event: MCPPanel.Dismissed) -> None:
            dismissed.append(event.result)

    assert not issubclass(MCPPanel, ModalScreen)
    assert issubclass(MCPModal, ModalScreen)

    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one("#mcp-list", OptionList)

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [None]


@pytest.mark.asyncio
async def test_mcp_panel_uses_composer_inline_prompt_surface(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_mod,
        "list_mcp_servers",
        lambda: [_server("context"), _server("browser", enabled=False)],
    )

    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 34)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(MCPPanel(), inline=True)
        await pilot.pause()

        panel = app.query_one(MCPPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert app.focused is app.query_one("#mcp-list", OptionList)

