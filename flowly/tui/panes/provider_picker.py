"""ProviderPicker — arrow-key list of LLM providers.

Opened by ``/provider`` (no arg) or by future keybinding. Reads the
provider registry, paints a live probe badge + ★ default marker on each
row, and on Enter persists the choice via ``set_active_provider`` + the
gateway hot-reload endpoint. No setup form here — that's still
``IntegrationSetupModal``; this picker is just a fast switcher.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

from flowly.integrations import IntegrationCard, ProbeResult, list_cards
from flowly.integrations.active_provider import (
    _build_for,
    resolve_active_provider,
    set_active_provider,
)
from flowly.integrations.probes import run_with_timeout
from flowly.tui.panes.inline_picker import (
    clamp_index,
    fuzzy_filter,
    is_plain_character,
    picker_width_for_columns,
    visible_window,
)

VISIBLE_ROWS = 12


class ProviderPickerPanel(Vertical):
    """Dismisses with one of:
      {'action': 'switched', 'key': '<provider>'}
      {'action': 'inline_setup', 'key': '<provider>'}  (paste primary key)
      {'action': 'opened_setup', 'key': '<provider>'}   (all-fields inline edit)
      None                                              (cancel)
    """

    can_focus = True

    DEFAULT_CSS = """
    ProviderPickerPanel {
        width: auto;
        min-width: 40;
        max-width: 90;
        height: auto;
        max-height: 22;
        padding: 0 1;
        border: double $primary;
        background: $surface;
    }
    ProviderPickerPanel .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    ProviderPickerPanel .muted,
    ProviderPickerPanel .hint,
    ProviderPickerPanel .scroll-line {
        color: $text-muted;
        height: 1;
    }
    ProviderPickerPanel .filter-line {
        height: 1;
        color: $text-muted;
    }
    ProviderPickerPanel .filter-line.active {
        color: $primary;
    }
    ProviderPickerPanel .warning-line {
        height: 1;
        color: $warning;
    }
    ProviderPickerPanel .picker-row {
        height: 1;
        color: $text-muted;
    }
    ProviderPickerPanel .picker-row.selected {
        background: $primary;
        color: $surface;
        text-style: bold;
    }
    ProviderPickerPanel .footer {
        color: $text-muted;
        height: 1;
    }
    """

    BINDINGS = []

    class Dismissed(Message):
        def __init__(self, result: dict[str, Any] | None) -> None:
            super().__init__()
            self.result = result

    def __init__(self) -> None:
        super().__init__()
        self._cards: list[IntegrationCard] = list_cards("provider")
        self._results: dict[str, ProbeResult] = {}
        self._active_key: str | None = None
        self._active_source: str = ""
        self._filter = ""
        self._selected_idx = 0

    # ── layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("Select provider", classes="title")
        yield Static("Full model catalogs stay under /model · Enter set up / switch", classes="muted")
        yield Static("", id="active-provider-line", classes="muted")
        yield Static("", id="provider-filter-line", classes="filter-line")
        yield Static("", id="provider-warning-line", classes="warning-line")
        yield Static("", id="provider-scroll-top", classes="scroll-line")
        for i in range(VISIBLE_ROWS):
            yield Static("", id=f"provider-row-{i}", classes="picker-row")
        yield Static("", id="provider-scroll-bottom", classes="scroll-line")
        yield Static("↑/↓ select · Enter choose · Ctrl+E edit · Ctrl+D sign out · Esc clear/back · q close",
                     id="provider-footer", classes="footer")

    def on_mount(self) -> None:
        self._sync_panel_width()
        self._resolve_active()
        self._selected_idx = self._active_index()
        self._render_list()
        self._kick_probes()
        self.focus()

    def on_resize(self, _event: events.Resize) -> None:
        self._sync_panel_width()

    def _sync_panel_width(self) -> None:
        try:
            if self._is_composer_inline():
                self.styles.width = "100%"
                self.styles.max_width = "100%"
                return
            self.styles.width = picker_width_for_columns(self.app.size.width)
        except Exception:
            pass

    def _is_composer_inline(self) -> bool:
        return any(
            bool(getattr(node, "has_class", lambda _name: False)("picker-inline-open"))
            for node in self.ancestors
        )

    def _finish(self, result: dict[str, Any] | None) -> None:
        self.post_message(self.Dismissed(result))

    def _resolve_active(self) -> None:
        try:
            from flowly.config.loader import load_config
            active = resolve_active_provider(load_config())
            self._active_key = active.key if active else None
            self._active_source = active.source if active else "configure one with /provider"
        except Exception:
            self._active_key = None
            self._active_source = ""
        self._refresh_active_line()

    def _active_label(self) -> str:
        if not self._active_key:
            return "none"
        card = next((c for c in self._cards if c.key == self._active_key), None)
        return card.label if card is not None else self._active_key

    def _refresh_active_line(self) -> None:
        label = self._active_label()
        if self._active_key:
            text = (
                f"Current: [b]{label}[/b]  [dim]{self._active_source}[/dim]"
            )
        else:
            text = "[yellow]Current: none[/yellow]  [dim]configure one before chatting[/dim]"
        try:
            self.query_one("#active-provider-line", Static).update(text)
        except Exception:
            pass

    def _active_index(self) -> int:
        cards = self._filtered_cards()
        if self._active_key:
            for i, card in enumerate(cards):
                if card.key == self._active_key:
                    return i
        return 0

    def _row_text(self, card: IntegrationCard) -> str:
        res = self._results.get(card.key)
        if res is None:
            mark = "·"
            detail = "probing..."
        else:
            mark = "*" if card.key == self._active_key else (
                "○" if res.status in {"auth_failed", "disabled", "not_configured"} else "●"
            )
            detail = res.detail or res.status
        active = " current" if card.key == self._active_key else ""
        return f"{mark} {card.label} · {detail}{active}"

    def _filtered_cards(self) -> list[IntegrationCard]:
        return fuzzy_filter(self._cards, self._filter, self._card_search_text)

    def _card_search_text(self, card: IntegrationCard) -> str:
        res = self._results.get(card.key)
        return " ".join([
            card.key,
            card.label,
            getattr(card, "description", "") or "",
            res.detail if res is not None else "",
            res.status if res is not None else "",
        ])

    def _selected_card(self) -> IntegrationCard | None:
        cards = self._filtered_cards()
        if not cards:
            return None
        self._selected_idx = clamp_index(self._selected_idx, len(cards))
        return cards[self._selected_idx]

    def _selected_key(self) -> str | None:
        card = self._selected_card()
        return card.key if card is not None else None

    @staticmethod
    def _index_for_key(cards: list[IntegrationCard], key: str | None) -> int | None:
        if not key:
            return None
        for idx, card in enumerate(cards):
            if card.key == key:
                return idx
        return None

    def _render_list(self, preferred_key: str | None = None) -> None:
        cards = self._filtered_cards()
        preferred_idx = self._index_for_key(cards, preferred_key)
        if preferred_idx is not None:
            self._selected_idx = preferred_idx
        else:
            self._selected_idx = clamp_index(self._selected_idx, len(cards))
        start, end = visible_window(self._selected_idx, len(cards), VISIBLE_ROWS)
        visible = cards[start:end]
        try:
            line = self.query_one("#provider-filter-line", Static)
            if self._filter:
                line.add_class("active")
                line.update(f"filter: {self._filter}▎ · {len(cards)}/{len(self._cards)}")
            else:
                line.remove_class("active")
                line.update("type to filter · ↑/↓ select")
        except Exception:
            pass
        try:
            selected = self._selected_card()
            res = self._results.get(selected.key) if selected is not None else None
            warning = res.detail if res is not None and res.status != "ok" else " "
            self.query_one("#provider-warning-line", Static).update(
                f"warning: {warning}" if warning.strip() else " "
            )
        except Exception:
            pass
        try:
            self.query_one("#provider-scroll-top", Static).update(
                f" ↑ {start} more" if start > 0 else " "
            )
            self.query_one("#provider-scroll-bottom", Static).update(
                f" ↓ {len(cards) - end} more" if end < len(cards) else " "
            )
        except Exception:
            pass
        for row in range(VISIBLE_ROWS):
            widget = self.query_one(f"#provider-row-{row}", Static)
            idx = start + row
            if row >= len(visible):
                widget.remove_class("selected")
                empty = (
                    "no providers match filter"
                    if self._filter
                    else "no providers available"
                )
                widget.update(empty if row == 0 and not cards else " ")
                continue
            card = visible[row]
            selected = idx == self._selected_idx
            if selected:
                widget.add_class("selected")
            else:
                widget.remove_class("selected")
            widget.update(f"{'▸' if selected else ' '} {idx + 1}. {self._row_text(card)}")

    def _refresh_row(self, key: str) -> None:
        self._render_list()

    # ── probing ───────────────────────────────────────────────────

    def _kick_probes(self) -> None:
        for card in self._cards:
            if card.probe is None:
                self._results[card.key] = ProbeResult("unknown", "no probe")
                self._refresh_row(card.key)
                continue
            self._probe_one(card)

    @work
    async def _probe_one(self, card: IntegrationCard) -> None:
        from flowly.integrations.config_io import read_card_values
        try:
            values = await asyncio.to_thread(read_card_values, card)
        except Exception as exc:
            self._results[card.key] = ProbeResult("unknown", f"read failed: {exc}")
            self._refresh_row(card.key)
            return
        if card.probe is None:
            return
        self._results[card.key] = await run_with_timeout(card.probe(values))
        self._refresh_row(card.key)

    # ── actions ───────────────────────────────────────────────────

    @work
    async def _do_switch(self, key: str) -> None:
        from flowly.config.loader import load_config
        cfg = load_config()
        if _build_for(cfg, key) is None:
            card = next((c for c in self._cards if c.key == key), None)
            if card is not None and card.custom_action in (
                "xai_login",
                "codex_login",
                "zai_coding_login",
            ):
                # Subscription-style provider: selecting it when not connected
                # should start its dedicated setup flow, not a generic form.
                self._finish({"action": "needs_login", "key": key})
            elif card is not None and card.custom_action == "login":
                # Flowly account: browser sign-in (which auto-provisions the
                # account key), NOT a paste-your-key form. The app opens the
                # LoginModal on "login".
                self._finish({"action": "login", "key": key})
            else:
                # Not configured yet → there's nothing to switch to, so the
                # only useful action is to set it up. Jump straight to the
                # credential form (paste-your-key screen) instead of making
                # the user discover the "E" binding.
                self._finish({"action": "inline_setup", "key": key})
            return
        try:
            model_changed = await asyncio.to_thread(set_active_provider, key)
        except Exception as exc:
            self._set_footer(f"[red]failed to switch: {exc}[/red]")
            return
        # Trigger the gateway's hot-reload so the change applies *now*.
        tail = await self._reload_gateway()
        model_note = f" · model → [b]{model_changed}[/b]" if model_changed else ""
        self._set_footer(f"✓ default → [b]{key}[/b]{model_note} · {tail}")
        await asyncio.sleep(0.9)
        self._finish({"action": "switched", "key": key})

    async def _reload_gateway(self) -> str:
        """Trigger the gateway hot-swap and push the new model into the
        TUI's StatusBar so the bottom chip refreshes without a restart."""
        from flowly.tui.gateway_reload import post_provider_reload
        try:
            r = await post_provider_reload(timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                new_model = str(data.get("model") or "")
                if new_model:
                    try:
                        from flowly.tui.panes.status import StatusBar
                        self.app.query_one(StatusBar).model = new_model
                    except Exception:
                        pass
                return f"gateway → {data.get('source') or data.get('key') or '?'}"
            if r.status_code == 422:
                err = (r.json() or {}).get("error", "no usable provider")
                return f"[yellow]reload rejected: {err}[/yellow]"
            return f"[yellow]reload HTTP {r.status_code}[/yellow]"
        except Exception:
            return "[dim]gateway offline — restart to apply[/dim]"

    def action_open_setup(self) -> None:
        card = self._selected_card()
        if card is None:
            return
        self._finish({"action": "opened_setup", "key": card.key})

    def action_disconnect(self) -> None:
        """Sign out of a subscription-style provider. For pasted-key providers
        there's nothing to "sign out" of, so we just hint."""
        selected = self._selected_card()
        if selected is None:
            return
        key = selected.key
        card = next((c for c in self._cards if c.key == key), None)
        if card is None or card.custom_action not in (
            "xai_login",
            "codex_login",
            "zai_coding_login",
        ):
            self._set_footer("[dim]nothing to sign out of for this provider[/dim]")
            return
        try:
            if card.custom_action == "codex_login":
                from flowly.auth.openai_codex import load_token_payload
                label = "ChatGPT subscription"
            elif card.custom_action == "zai_coding_login":
                from flowly.auth.zai_coding import load_token_payload
                label = "Z.AI GLM Coding Plan"
            else:
                from flowly.auth.xai_oauth import load_token_payload
                label = "xAI Grok OAuth"
            if load_token_payload() is None:
                self._set_footer(f"[dim]{label} is not signed in[/dim]")
                return
        except Exception:
            pass
        self._finish({"action": "disconnect", "key": key})

    def _set_footer(self, text: str) -> None:
        try:
            self.query_one("#provider-footer", Static).update(text)
        except Exception:
            pass

    def action_cancel(self) -> None:
        self._finish(None)

    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""
        cards = self._filtered_cards()
        handled = True
        is_ctrl_u = key == "ctrl+u" or (getattr(event, "ctrl", False) and char == "u")
        is_ctrl_e = key == "ctrl+e" or (getattr(event, "ctrl", False) and char == "e")
        is_ctrl_d = key == "ctrl+d" or (getattr(event, "ctrl", False) and char == "d")
        if key == "escape":
            if self._filter:
                preferred_key = self._selected_key() or self._active_key
                self._filter = ""
                self._render_list(preferred_key)
            else:
                self.action_cancel()
        elif key == "q" and not self._filter:
            self.action_cancel()
        elif key == "up":
            if cards:
                self._selected_idx = max(0, self._selected_idx - 1)
                self._render_list()
        elif key == "down":
            if cards:
                self._selected_idx = min(len(cards) - 1, self._selected_idx + 1)
                self._render_list()
        elif key == "home":
            if cards:
                self._selected_idx = 0
                self._render_list()
        elif key == "end":
            if cards:
                self._selected_idx = len(cards) - 1
                self._render_list()
        elif key == "pageup":
            if cards:
                self._selected_idx = max(0, self._selected_idx - VISIBLE_ROWS)
                self._render_list()
        elif key == "pagedown":
            if cards:
                self._selected_idx = min(len(cards) - 1, self._selected_idx + VISIBLE_ROWS)
                self._render_list()
        elif key in ("enter", "return"):
            card = self._selected_card()
            if card is not None:
                self._do_switch(card.key)
        elif key in ("backspace", "delete"):
            preferred_key = self._selected_key()
            self._filter = self._filter[:-1]
            self._render_list(preferred_key)
        elif is_ctrl_u:
            preferred_key = self._selected_key()
            self._filter = ""
            self._render_list(preferred_key)
        elif is_ctrl_e:
            self.action_open_setup()
        elif is_ctrl_d:
            self.action_disconnect()
        elif is_plain_character(event, char):
            self._filter += char
            self._selected_idx = 0
            self._render_list()
        else:
            handled = False
        if handled:
            event.stop()
            event.prevent_default()


class ProviderPicker(ModalScreen[dict[str, Any] | None]):
    """Modal wrapper kept for setup flows; chat mounts ProviderPickerPanel inline."""

    DEFAULT_CSS = """
    ProviderPicker { align: center middle; }
    """

    def compose(self) -> ComposeResult:
        yield ProviderPickerPanel()

    @on(ProviderPickerPanel.Dismissed)
    def _on_dismissed(self, event: ProviderPickerPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(event.result)
