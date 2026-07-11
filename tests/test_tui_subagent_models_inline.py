from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList

from flowly.tui.panes import subagent_models as models_mod
from flowly.tui.panes.composer import Composer
from flowly.tui.panes.subagent_models import (
    SpecialistModelPickerPanel,
    SubagentModelsModal,
    SubagentModelsPanel,
)


class _Client:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []

    async def subagents_assistants(self):
        return {
            "botModel": "openai/gpt-5",
            "assistants": [
                {
                    "name": "researcher",
                    "builtin": True,
                    "defaultModel": "openai/gpt-5-mini",
                    "override": "",
                }
            ],
        }

    async def subagents_set_model(self, name: str, choice: str):
        self.saved.append((name, choice))
        return {
            "override": choice,
            "effectiveModel": "openai/gpt-5",
            "botModel": "openai/gpt-5",
        }


@pytest.mark.asyncio
async def test_subagent_models_panel_edits_inline_and_saves(monkeypatch) -> None:
    async def no_models(_provider: str):
        return []

    monkeypatch.setattr(models_mod, "fetch_models", no_models)
    client = _Client()
    dismissed = 0

    class _Host(App):
        def compose(self):
            yield SubagentModelsPanel(client)

        @on(SubagentModelsPanel.Dismissed)
        def _on_dismissed(self, event: SubagentModelsPanel.Dismissed) -> None:
            nonlocal dismissed
            dismissed += 1

    assert not issubclass(SubagentModelsPanel, ModalScreen)
    assert issubclass(SubagentModelsModal, ModalScreen)

    app = _Host()
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one("#spec-list", OptionList)

        await pilot.press("enter")
        await pilot.pause()
        assert len(app.query(SpecialistModelPickerPanel)) == 1
        assert app.focused is app.query_one("#spm-filter", Input)

        await pilot.press("enter")
        await pilot.pause()
        assert client.saved == [("researcher", "inherit")]
        assert len(app.query(SpecialistModelPickerPanel)) == 0
        assert app.focused is app.query_one("#spec-list", OptionList)

        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert dismissed == 0
        assert len(app.query(SpecialistModelPickerPanel)) == 0

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == 1


@pytest.mark.asyncio
async def test_subagent_models_panel_uses_composer_inline_prompt_surface() -> None:
    client = _Client()

    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 36)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(SubagentModelsPanel(client), inline=True)
        await pilot.pause()

        panel = app.query_one(SubagentModelsPanel)
        picker_host = app.query_one("#composer-picker")

        assert composer.has_class("picker-inline-open")
        assert not app.query_one("#composer-input-row").display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width == picker_host.content_region.width
        assert app.focused is app.query_one("#spec-list", OptionList)

