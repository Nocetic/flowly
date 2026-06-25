"""IntegrationsModal — catalog cards with live status badges.

Opens via slash commands such as ``/integrations`` and ``/channels``. Each row shows:

  ●  linear            connected
  ⚠  x (twitter)       configured · auth failed (401)
  ○  trello            not configured

Keybindings
-----------
  ↑/↓     navigate (section headers are skipped)
  Enter   open setup form for the highlighted card
  T       re-run the probe for the highlighted card
  D       disconnect (clear) the highlighted card  (press twice to confirm)
  R       re-run every probe
  Esc/q   close

Returns from ``dismiss`` (used by the caller to react after close):
  None                              cancelled
  {'action': 'opened', 'key': str}  user opened a setup modal
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual import events, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from flowly.integrations import (
    IntegrationCard,
    ProbeResult,
    list_cards,
    read_card_values,
)
from flowly.integrations.probes import run_with_timeout


_CATEGORY_ORDER: list[tuple[str, str]] = [
    ("channel", "messaging channels"),
    ("tool", "integrations"),
    ("media", "media generation"),
    ("voice", "voice"),
    ("provider", "LLM providers"),
    ("system", "system"),
]


# Marker prefixes encoded into Option.id so on_option_selected can route.
_HEADER_PREFIX = "HEADER:"
_CARD_PREFIX = "CARD:"


def _badge_color(status: str) -> str:
    return {
        "ok": "green",
        "auth_failed": "yellow",
        "down": "red",
        "disabled": "yellow",
        "not_configured": "dim",
        "unknown": "dim",
    }.get(status, "dim")


class IntegrationsModal(ModalScreen[dict[str, Any] | None]):
    """Catalog modal — picks one card to set up."""

    DEFAULT_CSS = """
    IntegrationsModal { align: center middle; }
    IntegrationsModal > Vertical {
        width: 85%;
        max-width: 100;
        height: 80%;
        max-height: 32;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    IntegrationsModal .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    IntegrationsModal .hint {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-bottom: 1;
    }
    IntegrationsModal OptionList {
        height: 1fr;
        border: none;
        background: $surface;
    }
    IntegrationsModal .footer {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "dismiss(None)", "Close"),
        ("q",      "dismiss(None)", "Close"),
        ("r",      "reprobe_all",   "Re-test all"),
    ]

    def __init__(
        self,
        *,
        categories: tuple[str, ...] | None = None,
        title: str = "Integrations",
        item_label: str = "integration",
    ) -> None:
        super().__init__()
        self._categories = categories
        self._title = title
        self._item_label = item_label
        self._cards: list[IntegrationCard] = [
            card for card in list_cards()
            if categories is None or card.category in categories
        ]
        self._results: dict[str, ProbeResult] = {}
        self._row_index: dict[str, int] = {}  # card.key → OptionList index
        self._pending_delete: str | None = None
        # Which provider would actually serve the next LLM request? Used
        # to paint a ★ on that row so users see at a glance "this one is
        # live". Resolved on mount (synchronous, reads config + account).
        self._active_provider_key: str | None = None

    # ── layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, classes="title")
            yield Label(
                "↑/↓ navigate · Enter open · T re-test · D disconnect (×2) · R re-test all · Esc close",
                classes="hint",
            )
            yield OptionList(id="integrations-list")
            yield Label("", id="integrations-footer", classes="footer")

    def on_mount(self) -> None:
        if self._shows_providers:
            self._resolve_active_provider()
        self._rebuild_list()
        self._kick_probes()
        # Focus first selectable row.
        ol = self.query_one(OptionList)
        for i, opt in enumerate(ol.options):
            if not opt.disabled:
                ol.highlighted = i
                break

    def _resolve_active_provider(self) -> None:
        """Compute which provider serves the next LLM request. Safe to
        call repeatedly — the result feeds the ★ badge on each row."""
        try:
            from flowly.config.loader import load_config
            from flowly.integrations.active_provider import resolve_active_provider
            active = resolve_active_provider(load_config())
            self._active_provider_key = active.key if active else None
        except Exception:
            self._active_provider_key = None

    @property
    def _shows_providers(self) -> bool:
        return any(card.category == "provider" for card in self._cards)

    # ── list building ─────────────────────────────────────────────

    def _rebuild_list(self) -> None:
        """(Re)populate the OptionList with section headers + card rows.

        Headers are inserted as **disabled** options so arrow-key navigation
        skips them automatically.
        """
        ol = self.query_one(OptionList)
        ol.clear_options()
        self._row_index.clear()

        idx = 0
        for cat_key, cat_label in _CATEGORY_ORDER:
            cards = [c for c in self._cards if c.category == cat_key]
            if not cards:
                continue
            ol.add_option(Option(
                f"[bold dim]── {cat_label} ─────────────────────────────────[/bold dim]",
                id=f"{_HEADER_PREFIX}{cat_key}",
                disabled=True,
            ))
            idx += 1
            for card in cards:
                ol.add_option(Option(self._row_text(card), id=f"{_CARD_PREFIX}{card.key}"))
                self._row_index[card.key] = idx
                idx += 1

    def _row_text(self, card: IntegrationCard) -> str:
        res = self._results.get(card.key)
        if res is None:
            badge = "[dim]·[/dim]"
            detail = "[dim]probing…[/dim]"
        else:
            color = _badge_color(res.status)
            badge = f"[{color}]{res.badge}[/{color}]"
            detail = f"[{color}]{res.detail or res.status}[/{color}]"
        # ★ marker calls out the provider that's about to serve the next
        # LLM request. Helps answer the implicit "wait, which key am I
        # actually using right now?" question in one glance.
        # Explicit "★ default" beats implicit "active" — answers
        # "which one will the next LLM request actually use" at a glance.
        default_marker = ""
        if self._shows_providers and card.key == self._active_provider_key:
            default_marker = "  [yellow]★ default[/yellow]"
        # 2-col badge + 22-col label + flexible detail. Labels with non-ASCII
        # chars will look fine; OptionList uses Rich text width.
        return f" {badge}  [b]{card.label:<22}[/b]  {detail}{default_marker}"

    def _refresh_row(self, card_key: str) -> None:
        ol = self.query_one(OptionList)
        idx = self._row_index.get(card_key)
        if idx is None:
            return
        card = next((c for c in self._cards if c.key == card_key), None)
        if card is None:
            return
        # OptionList exposes ``replace_option_prompt_at_index`` in newer
        # Textual; fall back to remove+insert when missing.
        new_prompt = self._row_text(card)
        try:
            ol.replace_option_prompt_at_index(idx, new_prompt)
            return
        except (AttributeError, Exception):
            pass
        # Fallback path: rebuild the whole list (cheap — <30 rows).
        cur_highlight = ol.highlighted
        self._rebuild_list()
        if cur_highlight is not None and cur_highlight < len(ol.options):
            ol.highlighted = cur_highlight

    # ── probing ───────────────────────────────────────────────────

    def _kick_probes(self) -> None:
        """Fire every card's probe in parallel; refresh rows as they land."""
        for card in self._cards:
            if card.probe is None:
                self._results[card.key] = ProbeResult("unknown", "no probe")
                self._refresh_row(card.key)
                continue
            self._probe_one(card)

    @work
    async def _probe_one(self, card: IntegrationCard) -> None:
        try:
            values = read_card_values(card)
        except Exception as exc:
            self._results[card.key] = ProbeResult("unknown", f"read failed: {exc}")
            self._refresh_row(card.key)
            return
        if card.probe is None:
            return
        result = await run_with_timeout(card.probe(values))
        self._results[card.key] = result
        self._refresh_row(card.key)

    def action_reprobe_all(self) -> None:
        self._results.clear()
        # Active provider may have flipped (user signed in/out, key added) —
        # refresh before the rebuild so ★ lands on the right row.
        if self._shows_providers:
            self._resolve_active_provider()
        self._rebuild_list()
        self._kick_probes()
        self._set_footer(f"re-testing every {self._item_label}…")

    # ── selection & actions ───────────────────────────────────────

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        oid = str(event.option.id or "")
        if not oid.startswith(_CARD_PREFIX):
            return
        card_key = oid[len(_CARD_PREFIX):]
        # Defer to caller — dismiss with the key. The TUI will open the
        # setup modal, and the user can re-enter this catalog after.
        self.dismiss({"action": "opened", "key": card_key})

    def on_key(self, event: events.Key) -> None:
        # Cancel the two-step delete if the user changes focus or presses
        # anything that isn't another 'd'.
        if event.key != "d":
            self._pending_delete = None

    def _highlighted_card_key(self) -> str | None:
        ol = self.query_one(OptionList)
        if ol.highlighted is None:
            return None
        try:
            opt = ol.get_option_at_index(ol.highlighted)
        except Exception:
            return None
        oid = str(opt.id or "")
        if not oid.startswith(_CARD_PREFIX):
            return None
        return oid[len(_CARD_PREFIX):]

    def key_t(self) -> None:
        key = self._highlighted_card_key()
        if not key:
            return
        card = next((c for c in self._cards if c.key == key), None)
        if card is None or card.probe is None:
            self._set_footer(f"no probe defined for this {self._item_label}")
            return
        self._results.pop(key, None)
        self._refresh_row(key)
        self._probe_one(card)
        self._set_footer(f"re-testing {card.label}…")

    def key_d(self) -> None:
        key = self._highlighted_card_key()
        if not key:
            return
        card = next((c for c in self._cards if c.key == key), None)
        if card is None:
            return
        if not card.fields:
            self._set_footer(f"{card.label} has no fields to clear")
            return
        if self._pending_delete == key:
            self._pending_delete = None
            self._do_disconnect(card)
            return
        self._pending_delete = key
        self.notify(
            f"press 'd' again to disconnect {card.label} (all credentials wiped)",
            severity="warning", timeout=4,
        )

    @work
    async def _do_disconnect(self, card: IntegrationCard) -> None:
        from flowly.integrations.config_io import clear_card
        try:
            await asyncio.to_thread(clear_card, card)
        except Exception as exc:
            self._set_footer(f"disconnect failed: {exc}")
            return
        self._set_footer(f"✓ disconnected {card.label}")
        self._results.pop(card.key, None)
        self._refresh_row(card.key)
        # Re-probe so badge updates immediately.
        if card.probe is not None:
            self._probe_one(card)

    # ── footer helpers ────────────────────────────────────────────

    def _set_footer(self, text: str) -> None:
        try:
            self.query_one("#integrations-footer", Label).update(text)
        except Exception:
            pass
