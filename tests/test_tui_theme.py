from __future__ import annotations

from flowly.tui.setup_app import SetupApp
from flowly.tui.theme import css_for, get_theme, list_themes, resolve_theme_name


def test_composer_hint_and_attachments_are_themed() -> None:
    amber = get_theme("amber")
    assert amber is not None

    css = css_for(amber)

    assert "Composer > #composer-hint" in css
    assert f"background: {amber.bg}; color: {amber.text_muted};" in css
    assert "Composer > #composer-attachments" in css
    assert f"background: {amber.bg}; color: {amber.accent};" in css


def test_tui_screens_render_as_bottom_sheets() -> None:
    """Modals are positioned generically off the ``ModalScreen`` base class, so
    any new modal is a composer-adjacent bottom sheet automatically — no
    per-name list to fall out of sync (which is how /usage first shipped
    centered). The generic selector only reaches a screen because it subclasses
    ModalScreen, so spot-check that too."""
    from textual.screen import ModalScreen

    from flowly.tui.panes.help_modal import HelpModal
    from flowly.tui.panes.model_picker import ModelPicker

    css = css_for()

    assert "ModalScreen {" in css
    assert "ModalScreen > Vertical {" in css
    assert "align: center bottom;" in css
    assert "margin-bottom: 5;" in css

    for cls in (HelpModal, ModelPicker):
        assert issubclass(cls, ModalScreen)


def test_setup_screens_render_as_bottom_sheets() -> None:
    css = SetupApp.CSS

    for screen in (
        "ProviderPicker",
        "IntegrationsModal",
        "IntegrationSetupModal",
    ):
        assert f"{screen}," in css or f"{screen} {{" in css
        assert f"{screen} > Vertical," in css or f"{screen} > Vertical {{" in css
    assert "LoginModal" in css
    assert "LoginModal > LoginPanel" in css
    assert "align: center bottom;" in css
    assert "margin-bottom: 5;" in css


def test_user_bubble_surface_is_visible_and_assistant_bubbles_stay_transparent() -> None:
    for palette in list_themes():
        css = css_for(palette)

        assert "background: transparent;" in css
        assert f"Bubble.user      {{ border: none; background: {palette.boost}; }}" in css
        assert (
            f"Bubble.assistant {{ border: round {palette.assistant}; "
            "background: transparent; }"
        ) in css
        assert (
            f"Bubble.system    {{ border: round {palette.system}; "
            "background: transparent; }"
        ) in css
        assert (
            f"Bubble.slash     {{ border: round {palette.system}; "
            "background: transparent; }"
        ) in css
        assert (
            f"Bubble.error     {{ border: round {palette.error}; "
            "background: transparent; }"
        ) in css


def test_moonfly_theme_matches_svg_palette() -> None:
    moonfly = get_theme("moonfly")
    assert moonfly is not None

    names = {theme.name for theme in list_themes()}
    assert "moonfly" in names
    assert moonfly.bg == "#080808"
    assert moonfly.text == "#bdbdbd"
    assert moonfly.accent == "#80a0ff"
    assert moonfly.accent_soft == "#79dac8"
    assert moonfly.error == "#ff5454"
    assert moonfly.success == "#8cc85f"
    assert moonfly.warning == "#e3c78a"
    assert get_theme("moonfly-default") == moonfly
    assert get_theme("moon") == moonfly


def test_retired_theme_names_alias_to_curated_replacements() -> None:
    names = {theme.name for theme in list_themes()}

    assert "midnight" not in names
    assert "rose-pine" not in names
    assert "gruvbox" not in names
    assert "future" not in names
    assert "retro" not in names
    assert get_theme("midnight") == get_theme("moonfly")
    assert get_theme("rose-pine") == get_theme("catppuccin")
    assert get_theme("rose") == get_theme("catppuccin")
    assert get_theme("gruvbox") == get_theme("amber")
    assert get_theme("gruv") == get_theme("amber")
    assert get_theme("future") == get_theme("synthwave")
    assert get_theme("retro") == get_theme("synthwave")
    assert resolve_theme_name(state={"theme": "midnight"}) == "moonfly"
    assert resolve_theme_name(state={"theme": "rose-pine"}) == "catppuccin"
    assert resolve_theme_name(state={"theme": "gruvbox"}) == "amber"
    assert resolve_theme_name(state={"theme": "future"}) == "synthwave"
    assert resolve_theme_name(state={"theme": "retro"}) == "synthwave"
