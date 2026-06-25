"""Inline (InquirerPy) configuration of integration cards — the no-Textual path.

The Textual ``IntegrationsModal`` is a full-screen editor; launching it mid-
onboarding is jarring (and flaky from a CLI context). This module configures the
same :class:`IntegrationCard`s as a sequence of terminal prompts — text, masked
secret, toggle, select — reusing the registry's field definitions and the shared
``config_io`` writer. Nothing here opens a Textual screen, so it composes cleanly
into the inline setup home.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console


def _is_configured(card) -> bool:
    """Cheap check for the picker ✓ mark: a required secret is set, or enabled."""
    from flowly.integrations.config_io import read_card_values

    try:
        values = read_card_values(card)
    except Exception:
        return False
    if values.get("enabled") is True:
        return True
    for f in card.fields:
        if getattr(f, "required", False) and str(values.get(f.key) or "").strip():
            return True
    return False


def _prompt_field(field, current: dict):
    """Prompt a single field by its type; return the new value.

    For PASSWORD fields a blank entry keeps the existing value (so re-running
    setup doesn't force you to re-paste a token).
    """
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    from flowly.integrations.cards import FieldType

    cur = current.get(field.key, field.default)

    if field.type == FieldType.BOOL:
        return inquirer.confirm(message=field.label, default=bool(cur)).execute()

    if field.type == FieldType.SELECT:
        choices = [Choice(value=v, name=n) for v, n in (field.choices or [])]
        default = cur if any(c.value == cur for c in choices) else (choices[0].value if choices else cur)
        return inquirer.select(
            message=field.label, choices=choices, default=default, pointer="›"
        ).execute()

    if field.type == FieldType.INT:
        raw = inquirer.number(
            message=field.label,
            default=int(cur) if str(cur).strip().lstrip("-").isdigit() else 0,
        ).execute()
        try:
            return int(raw)
        except (TypeError, ValueError):
            return cur

    if field.type == FieldType.MULTI:
        seed = ", ".join(cur) if isinstance(cur, list) else str(cur or "")
        raw = inquirer.text(message=f"{field.label} (comma-separated)", default=seed).execute()
        return [s.strip() for s in (raw or "").split(",") if s.strip()]

    if field.type == FieldType.PASSWORD:
        suffix = " [enter to keep current]" if cur else ""
        val = inquirer.secret(message=field.label + suffix).execute()
        return val.strip() if (val or "").strip() else cur

    # TEXT (default)
    return inquirer.text(message=field.label, default=str(cur or "")).execute().strip()


def configure_card_inline(card, console: "Console") -> bool:
    """Configure one card via inline prompts and save it. True if saved.

    A channel with a credential entered is auto-enabled so it actually runs; the
    ``enabled`` toggle itself isn't prompted (it would be a confusing extra step).
    """
    from flowly.integrations.config_io import apply_card_values, read_card_values

    console.print()
    console.print(f"  [bold #19d3e6]›[/] [bold]{card.label}[/]   [dim]{card.description}[/dim]")
    if card.docs_url:
        console.print(f"     [dim]{card.docs_url}[/dim]")
    console.print("     [dim](Ctrl-C to skip)[/dim]")

    current = read_card_values(card)
    values = dict(current)
    try:
        for f in card.fields:
            if f.key == "enabled":
                continue  # auto-managed below
            values[f.key] = _prompt_field(f, current)
    except KeyboardInterrupt:
        console.print("  [dim]Skipped.[/dim]")
        return False

    has_secret = any(
        getattr(f, "required", False)
        and str(values.get(f.key) or "").strip()
        for f in card.fields
    )
    if any(f.key == "enabled" for f in card.fields):
        values["enabled"] = has_secret

    if not has_secret:
        console.print("  [yellow]Nothing entered — left unconfigured.[/yellow]")
        return False

    apply_card_values(card, values)
    console.print(f"  [green]✓[/green] {card.label} saved")
    return True


_SECTION_BLURB = {
    "channel": "Connect a messaging channel so Flowly can reach you on it.",
    "tool": "Connect a service so the agent can act through it.",
    "media": "Let the agent generate images (FAL) — paste a key, pick a model.",
}


def configure_section_inline(category: str, title: str, console: "Console") -> None:
    """Pick cards in a category to set up, one at a time, until skipped/done.

    A clear "Skip" entry plus Esc / ← (left) / Ctrl-C all leave the section, so
    this step is fully optional. Configured cards are marked ✓; the header tracks
    how many of the category are set up.
    """
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice
    from InquirerPy.separator import Separator

    from flowly.integrations.registry import get_card, list_cards

    cards = list_cards(category)
    width = max((len(c.label) for c in cards), default=0)
    blurb = _SECTION_BLURB.get(category, "")

    while True:
        done = sum(1 for c in cards if _is_configured(c))
        console.print()
        console.rule(
            f"[bold #00a6c8]{title}[/]  [dim]· {done}/{len(cards)} set up[/dim]",
            style="#0b7c97",
            align="left",
        )
        if blurb:
            console.print(f"  [dim]{blurb} Pick one to set up, or skip.[/dim]")

        # Plain glyphs only — InquirerPy choice text is not rich-markup rendered.
        choices = [
            Choice(
                value=c.key,
                name=f" {'✓' if _is_configured(c) else '·'}  {c.label:<{width}}   {c.description}",
            )
            for c in cards
        ]
        choices.append(Separator())
        choices.append(Choice(value=None, name=" ⏭   Skip — continue"))

        try:
            picked = inquirer.select(
                message=f"{title}:",
                choices=choices,
                pointer="›",
                mandatory=False,
                keybindings={"skip": [{"key": "escape"}, {"key": "left"}]},
            ).execute()
        except KeyboardInterrupt:
            return
        if picked is None:
            return
        card = get_card(picked)
        if card is not None:
            configure_card_inline(card, console)
