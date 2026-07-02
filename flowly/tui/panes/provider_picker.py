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

from textual import events, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from flowly.integrations import IntegrationCard, ProbeResult, list_cards
from flowly.integrations.active_provider import (
    _build_for,
    resolve_active_provider,
    set_active_provider,
)
from flowly.integrations.probes import run_with_timeout


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


class ProviderPicker(ModalScreen[dict[str, Any] | None]):
    """Dismisses with one of:
      {'action': 'switched', 'key': '<provider>'}
      {'action': 'inline_setup', 'key': '<provider>'}  (paste primary key)
      {'action': 'opened_setup', 'key': '<provider>'}   (user wants to edit)
      None                                              (cancel)
    """

    DEFAULT_CSS = """
    ProviderPicker { align: center middle; }
    ProviderPicker > Vertical {
        width: 75%;
        max-width: 90;
        height: 70%;
        max-height: 26;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    ProviderPicker .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    ProviderPicker .hint {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-bottom: 1;
    }
    ProviderPicker .active-line {
        color: $text;
        height: auto;
        padding: 1;
        margin-bottom: 1;
        background: $boost;
    }
    ProviderPicker OptionList {
        height: 1fr;
        border: none;
        background: $surface;
    }
    ProviderPicker .footer {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "dismiss(None)", "Close"),
        ("q",      "dismiss(None)", "Close"),
        ("e",      "open_setup",    "Edit"),
        ("x",      "disconnect",    "Sign out"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._cards: list[IntegrationCard] = list_cards("provider")
        self._results: dict[str, ProbeResult] = {}
        self._row_index: dict[str, int] = {}
        self._active_key: str | None = None
        self._active_source: str = ""

    # ── layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("LLM provider", classes="title")
            yield Label(
                "↑/↓ navigate · Enter set up / switch · E edit · X sign out · Esc close",
                classes="hint",
            )
            yield Label("", id="active-provider-line", classes="active-line")
            yield OptionList(id="provider-list")
            yield Label("", id="provider-footer", classes="footer")

    def on_mount(self) -> None:
        self._resolve_active()
        self._rebuild_list()
        self._kick_probes()
        ol = self.query_one(OptionList)
        # Land highlight on the current default if any, else the first row.
        if self._active_key and self._active_key in self._row_index:
            ol.highlighted = self._row_index[self._active_key]
        elif ol.options:
            ol.highlighted = 0

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
                f"[dim]Active now[/]  [b]{label}[/b]  "
                f"[dim]{self._active_source}[/dim]"
            )
        else:
            text = "[yellow]No active provider[/yellow]  [dim]configure one before chatting[/dim]"
        try:
            self.query_one("#active-provider-line", Label).update(text)
        except Exception:
            pass

    def _rebuild_list(self) -> None:
        ol = self.query_one(OptionList)
        ol.clear_options()
        self._row_index.clear()
        for i, card in enumerate(self._cards):
            ol.add_option(Option(self._row_text(card), id=f"{_CARD_PREFIX}{card.key}"))
            self._row_index[card.key] = i

    def _row_text(self, card: IntegrationCard) -> str:
        res = self._results.get(card.key)
        if res is None:
            badge = "[dim]·[/dim]"
            detail = "[dim]probing…[/dim]"
        else:
            color = _badge_color(res.status)
            badge = f"[{color}]{res.badge}[/{color}]"
            detail = f"[{color}]{res.detail or res.status}[/{color}]"
        default = (
            "  [yellow]ACTIVE[/yellow]"
            if card.key == self._active_key
            else ""
        )
        return f" {badge}  [b]{card.label:<22}[/b]  {detail}{default}"

    def _refresh_row(self, key: str) -> None:
        ol = self.query_one(OptionList)
        idx = self._row_index.get(key)
        if idx is None:
            return
        card = next((c for c in self._cards if c.key == key), None)
        if card is None:
            return
        try:
            ol.replace_option_prompt_at_index(idx, self._row_text(card))
        except (AttributeError, Exception):
            cur = ol.highlighted
            self._rebuild_list()
            if cur is not None and cur < len(ol.options):
                ol.highlighted = cur

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

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        oid = str(event.option.id or "")
        if not oid.startswith(_CARD_PREFIX):
            return
        key = oid[len(_CARD_PREFIX):]
        # Refuse the switch if the provider isn't actually usable —
        # silently dangling default points are how users end up stuck.
        self._do_switch(key)

    @work
    async def _do_switch(self, key: str) -> None:
        from flowly.config.loader import load_config
        cfg = load_config()
        if _build_for(cfg, key) is None:
            card = next((c for c in self._cards if c.key == key), None)
            if card is not None and card.custom_action in ("xai_login", "codex_login"):
                # Browser-OAuth provider: selecting it when not signed in
                # should just start the browser login — no form, no key to
                # paste. The app handles "needs_login" by firing the flow.
                self.dismiss({"action": "needs_login", "key": key})
            elif card is not None and card.custom_action == "login":
                # Flowly account: browser sign-in (which auto-provisions the
                # account key), NOT a paste-your-key form. The app opens the
                # LoginModal on "login".
                self.dismiss({"action": "login", "key": key})
            else:
                # Not configured yet → there's nothing to switch to, so the
                # only useful action is to set it up. Jump straight to the
                # credential form (paste-your-key screen) instead of making
                # the user discover the "E" binding.
                self.dismiss({"action": "inline_setup", "key": key})
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
        self.dismiss({"action": "switched", "key": key})

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
        ol = self.query_one(OptionList)
        if ol.highlighted is None:
            return
        try:
            opt = ol.get_option_at_index(ol.highlighted)
        except Exception:
            return
        oid = str(opt.id or "")
        if not oid.startswith(_CARD_PREFIX):
            return
        key = oid[len(_CARD_PREFIX):]
        self.dismiss({"action": "opened_setup", "key": key})

    def action_disconnect(self) -> None:
        """Sign out of a browser-OAuth provider (xAI Grok). For pasted-key
        providers there's nothing to "sign out" of, so we just hint."""
        ol = self.query_one(OptionList)
        if ol.highlighted is None:
            return
        try:
            opt = ol.get_option_at_index(ol.highlighted)
        except Exception:
            return
        oid = str(opt.id or "")
        if not oid.startswith(_CARD_PREFIX):
            return
        key = oid[len(_CARD_PREFIX):]
        card = next((c for c in self._cards if c.key == key), None)
        if card is None or card.custom_action not in ("xai_login", "codex_login"):
            self._set_footer("[dim]nothing to sign out of for this provider[/dim]")
            return
        try:
            if card.custom_action == "codex_login":
                from flowly.auth.openai_codex import load_token_payload
                label = "ChatGPT subscription"
            else:
                from flowly.auth.xai_oauth import load_token_payload
                label = "xAI Grok OAuth"
            if load_token_payload() is None:
                self._set_footer(f"[dim]{label} is not signed in[/dim]")
                return
        except Exception:
            pass
        self.dismiss({"action": "disconnect", "key": key})

    def _set_footer(self, text: str) -> None:
        try:
            self.query_one("#provider-footer", Label).update(text)
        except Exception:
            pass
