from __future__ import annotations

import pytest
from textual import on
from textual.app import App
from textual.widgets import Input, OptionList, Static

from flowly.integrations import IntegrationCard
from flowly.integrations.model_catalog import Model
from flowly.tui.panes import model_picker as model_picker_mod
from flowly.tui.panes import provider_picker as provider_picker_mod
from flowly.tui.panes.composer import Composer
from flowly.tui.panes.inline_picker import fuzzy_filter, picker_width_for_columns


def _provider_card(key: str, label: str) -> IntegrationCard:
    return IntegrationCard(
        key=key,
        label=label,
        category="provider",
        description=f"{label} provider",
        docs_url="",
        config_path=f"llm.providers.{key}",
        probe=None,
    )


def test_inline_picker_fuzzy_filter_ranks_subsequence_and_preserves_ties() -> None:
    items = [
        "openai/gpt-5",
        "anthropic/claude-sonnet-4.5",
        "anthropic/claude-opus-4.1",
    ]

    assert fuzzy_filter(items, "cs45", lambda item: item) == [
        "anthropic/claude-sonnet-4.5"
    ]
    assert fuzzy_filter(items, "anthropic", lambda item: item) == [
        "anthropic/claude-sonnet-4.5",
        "anthropic/claude-opus-4.1",
    ]


def test_inline_picker_width_is_stable_and_responsive() -> None:
    assert picker_width_for_columns(160) == 90
    assert picker_width_for_columns(80) == 74
    assert picker_width_for_columns(42) == 36


@pytest.mark.asyncio
async def test_provider_picker_panel_escape_dismisses(monkeypatch) -> None:
    monkeypatch.setattr(provider_picker_mod, "list_cards", lambda category: [])
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield provider_picker_mod.ProviderPickerPanel()

        @on(provider_picker_mod.ProviderPickerPanel.Dismissed)
        def _on_dismissed(self, event: provider_picker_mod.ProviderPickerPanel.Dismissed) -> None:
            dismissed.append(event.result)

    app = _Host()
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [None]


@pytest.mark.asyncio
async def test_provider_picker_panel_is_type_to_filter_not_option_list(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_picker_mod,
        "list_cards",
        lambda category: [
            _provider_card("openai", "OpenAI"),
            _provider_card("anthropic", "Anthropic"),
        ],
    )
    monkeypatch.setattr(provider_picker_mod, "resolve_active_provider", lambda cfg: None)
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield provider_picker_mod.ProviderPickerPanel()

        @on(provider_picker_mod.ProviderPickerPanel.Dismissed)
        def _on_dismissed(self, event: provider_picker_mod.ProviderPickerPanel.Dismissed) -> None:
            dismissed.append(event.result)

    app = _Host()
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()

        assert list(app.query(OptionList)) == []

        await pilot.press("e")
        await pilot.pause()

        assert "filter: e" in str(app.query_one("#provider-filter-line", Static).render())
        assert dismissed == []

        await pilot.press("escape")
        await pilot.pause()

        assert "type to filter" in str(app.query_one("#provider-filter-line", Static).render())
        assert dismissed == []

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [None]


@pytest.mark.asyncio
async def test_provider_picker_panel_supports_page_and_boundary_navigation(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_picker_mod,
        "list_cards",
        lambda category: [
            _provider_card(f"provider-{idx:02d}", f"Provider {idx:02d}")
            for idx in range(20)
        ],
    )
    monkeypatch.setattr(provider_picker_mod, "resolve_active_provider", lambda cfg: None)

    class _Host(App):
        def compose(self):
            yield provider_picker_mod.ProviderPickerPanel()

    app = _Host()
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()

        await pilot.press("pagedown")
        await pilot.pause()
        assert "Provider 12" in str(app.query_one("#provider-row-6", Static).render())

        await pilot.press("end")
        await pilot.pause()
        assert "Provider 19" in str(app.query_one("#provider-row-11", Static).render())

        await pilot.press("home")
        await pilot.pause()
        assert "Provider 00" in str(app.query_one("#provider-row-0", Static).render())


@pytest.mark.asyncio
async def test_model_picker_panel_escape_dismisses(monkeypatch) -> None:
    async def fake_fetch_models(provider_key: str):
        return []

    monkeypatch.setattr(model_picker_mod, "fetch_models", fake_fetch_models)
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield model_picker_mod.ModelPickerPanel(
                provider_key="openrouter",
                provider_label="OpenRouter",
            )

        @on(model_picker_mod.ModelPickerPanel.Dismissed)
        def _on_dismissed(self, event: model_picker_mod.ModelPickerPanel.Dismissed) -> None:
            dismissed.append(event.result)

    app = _Host()
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [None]


@pytest.mark.asyncio
async def test_model_picker_panel_is_type_to_filter_not_form_modal(monkeypatch) -> None:
    async def fake_fetch_models(provider_key: str):
        return [
            Model(id="anthropic/claude-sonnet-4.5", name="Claude Sonnet 4.5", tags=["vision"]),
            Model(id="openai/gpt-5", name="GPT-5", tags=[]),
        ]

    monkeypatch.setattr(model_picker_mod, "fetch_models", fake_fetch_models)
    dismissed: list[object] = []

    class _Host(App):
        def compose(self):
            yield model_picker_mod.ModelPickerPanel(
                provider_key="openrouter",
                provider_label="OpenRouter",
                current_model="openai/gpt-5",
            )

        @on(model_picker_mod.ModelPickerPanel.Dismissed)
        def _on_dismissed(self, event: model_picker_mod.ModelPickerPanel.Dismissed) -> None:
            dismissed.append(event.result)

    app = _Host()
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()

        assert list(app.query(Input)) == []
        assert list(app.query(OptionList)) == []

        await pilot.press("c")
        await pilot.pause()

        assert "filter: c" in str(app.query_one("#model-filter-line", Static).render())
        assert "anthropic/claude-sonnet-4.5" in str(app.query_one("#model-row-0", Static).render())
        assert dismissed == []

        await pilot.press("escape")
        await pilot.pause()

        assert "type to filter" in str(app.query_one("#model-filter-line", Static).render())
        assert dismissed == []

        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [None]


@pytest.mark.asyncio
async def test_model_picker_panel_uses_fuzzy_filter_and_preserves_clear_selection(monkeypatch) -> None:
    async def fake_fetch_models(provider_key: str):
        return [
            Model(id="openai/gpt-5", name="GPT-5", tags=[]),
            Model(id="anthropic/claude-sonnet-4.5", name="Claude Sonnet 4.5", tags=["vision"]),
            Model(id="anthropic/claude-opus-4.1", name="Claude Opus 4.1", tags=[]),
        ]

    monkeypatch.setattr(model_picker_mod, "fetch_models", fake_fetch_models)

    class _Host(App):
        def compose(self):
            yield model_picker_mod.ModelPickerPanel(
                provider_key="openrouter",
                provider_label="OpenRouter",
            )

    app = _Host()
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()

        for key in ("c", "s", "4", "5"):
            await pilot.press(key)
        await pilot.pause()

        assert "filter: cs45" in str(app.query_one("#model-filter-line", Static).render())
        assert "anthropic/claude-sonnet-4.5" in str(app.query_one("#model-row-0", Static).render())

        await pilot.press("escape")
        await pilot.pause()

        assert "type to filter" in str(app.query_one("#model-filter-line", Static).render())
        assert "anthropic/claude-sonnet-4.5" in str(app.query_one("#model-row-1", Static).render())
        assert app.query_one("#model-row-1", Static).has_class("selected")


@pytest.mark.asyncio
async def test_composer_picker_slot_mounts_and_clears() -> None:
    class _FocusablePicker(Static):
        can_focus = True

    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(90, 30)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(_FocusablePicker("picker body", id="dummy-picker"))
        await pilot.pause()

        picker = app.query_one("#dummy-picker", _FocusablePicker)
        picker_host = app.query_one("#composer-picker")
        input_row = app.query_one("#composer-input-row")

        assert composer.has_class("picker-open")
        assert composer.has_class("picker-floating-open")
        assert not composer.has_class("picker-inline-open")
        assert "Composer.picker-open > #composer-input-row" not in Composer.DEFAULT_CSS
        assert input_row.display
        assert picker_host.styles.overlay == "screen"
        assert picker_host.region.y < input_row.region.y
        assert app.focused is picker
        assert len(list(app.query("#dummy-picker"))) == 1

        await composer.clear_picker()
        await pilot.pause()

        assert not composer.has_class("picker-open")
        assert not composer.has_class("picker-floating-open")
        assert not composer.has_class("picker-inline-open")
        assert list(app.query("#dummy-picker")) == []


@pytest.mark.asyncio
async def test_provider_picker_uses_composer_inline_prompt_surface(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_picker_mod,
        "list_cards",
        lambda category: [
            _provider_card("openai", "OpenAI"),
            _provider_card("anthropic", "Anthropic"),
        ],
    )
    monkeypatch.setattr(provider_picker_mod, "resolve_active_provider", lambda cfg: None)

    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 30)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(provider_picker_mod.ProviderPickerPanel(), inline=True)
        await pilot.pause()

        picker_host = app.query_one("#composer-picker")
        panel = app.query_one(provider_picker_mod.ProviderPickerPanel)
        input_row = app.query_one("#composer-input-row")

        assert composer.has_class("picker-inline-open")
        assert not composer.has_class("picker-floating-open")
        assert "Composer.picker-inline-open > #composer-input-row" in Composer.DEFAULT_CSS
        assert not input_row.display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width > picker_width_for_columns(app.size.width)
        assert app.focused is panel

    assert "background: #000000" not in provider_picker_mod.ProviderPickerPanel.DEFAULT_CSS
    assert "background: #000000" not in model_picker_mod.ModelPickerPanel.DEFAULT_CSS


@pytest.mark.asyncio
async def test_model_picker_uses_composer_inline_prompt_surface(monkeypatch) -> None:
    async def fake_fetch_models(provider_key: str):
        return [
            Model(id="anthropic/claude-sonnet-4.5", name="Claude Sonnet 4.5", tags=[]),
            Model(id="openai/gpt-5", name="GPT-5", tags=[]),
        ]

    monkeypatch.setattr(model_picker_mod, "fetch_models", fake_fetch_models)

    class _Host(App):
        def compose(self):
            yield Composer()

    app = _Host()
    async with app.run_test(size=(120, 30)) as pilot:
        composer = app.query_one(Composer)
        await composer.show_picker(
            model_picker_mod.ModelPickerPanel(
                provider_key="openrouter",
                provider_label="OpenRouter",
            ),
            inline=True,
        )
        await pilot.pause()

        picker_host = app.query_one("#composer-picker")
        panel = app.query_one(model_picker_mod.ModelPickerPanel)
        input_row = app.query_one("#composer-input-row")

        assert composer.has_class("picker-inline-open")
        assert not composer.has_class("picker-floating-open")
        assert not input_row.display
        assert picker_host.styles.overlay != "screen"
        assert panel.styles.background.a == 0
        assert panel.region.width > picker_width_for_columns(app.size.width)
        assert app.focused is panel
