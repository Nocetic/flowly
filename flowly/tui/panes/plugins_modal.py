"""PluginsModal — list installed plugins, toggle enabled, see errors.

Mirrors desktop's plugin tab (``flowly-desktop/src/main/local/flowlyai-
service.ts:2204+`` ``pluginsList`` + the renderer UI). One row per
discovered plugin (bundled + user), colour-coded by status, with
arrow-key navigation and a single-key toggle.

Bundled plugins are default-on (must be explicitly disabled). User
plugins are opt-in (must be added to ``plugins.enabled``). Both flip
through the same :func:`set_plugin_enabled` helper that writes both
config arrays atomically.

After a toggle the gateway needs to restart because plugins register
their tools at boot — we kick launchd from the modal so the change
applies in seconds without leaving the keyboard.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from flowly.integrations.plugins_io import (
    PluginEntry,
    list_plugins,
    set_plugin_enabled,
)


_PREFIX = "PLUGIN:"


def _status_color(status: str) -> str:
    return {
        "enabled":   "green",
        "disabled":  "yellow",
        "available": "cyan",
        "error":     "red",
    }.get(status, "dim")


def _status_glyph(status: str) -> str:
    return {
        "enabled":   "●",
        "disabled":  "○",
        "available": "◇",
        "error":     "✗",
    }.get(status, "·")


class PluginsModal(ModalScreen[dict[str, Any] | None]):
    """Dismisses with one of:
      {'action': 'changed', 'count': N}  → N plugins toggled this session
      None                                → cancel (no changes)
    """

    DEFAULT_CSS = """
    PluginsModal { align: center middle; }
    PluginsModal > Vertical {
        width: 85%;
        max-width: 100;
        height: 80%;
        max-height: 32;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    PluginsModal .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    PluginsModal .hint {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-bottom: 1;
    }
    PluginsModal OptionList {
        height: 1fr;
        border: none;
        background: $surface;
    }
    PluginsModal .footer {
        color: $text-muted;
        text-style: italic;
        height: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "dismiss_with_count", "Close"),
        ("q",      "dismiss_with_count", "Close"),
        ("r",      "reload",             "Reload list"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._plugins: list[PluginEntry] = []
        self._changes = 0   # number of toggles this session

    # ── layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Plugins", classes="title")
            yield Label(
                "↑/↓ navigate · Space/Enter toggle · R reload · Esc close",
                classes="hint",
            )
            yield OptionList(id="plugins-list")
            yield Label("", id="plugins-footer", classes="footer")

    def on_mount(self) -> None:
        self._rebuild_list()
        ol = self.query_one(OptionList)
        if ol.options:
            ol.highlighted = 0

    def _rebuild_list(self) -> None:
        self._plugins = list_plugins()
        ol = self.query_one(OptionList)
        # Preserve highlight across reload so toggling doesn't bounce
        # the cursor back to the top each time.
        prev_key: str | None = None
        if ol.highlighted is not None and ol.highlighted < len(ol.options):
            try:
                opt_id = str(ol.get_option_at_index(ol.highlighted).id or "")
                if opt_id.startswith(_PREFIX):
                    prev_key = opt_id[len(_PREFIX):]
            except Exception:
                pass
        ol.clear_options()
        if not self._plugins:
            ol.add_option(Option(
                "[dim]No plugins installed. Drop one under "
                "~/.flowly/plugins/<name>/ with a plugin.yaml.[/dim]",
                id="EMPTY", disabled=True,
            ))
            self._set_footer("")
            return
        for p in self._plugins:
            ol.add_option(Option(self._row_text(p), id=f"{_PREFIX}{p.key}"))
        # Restore highlight to same plugin if still present
        if prev_key:
            for i, p in enumerate(self._plugins):
                if p.key == prev_key:
                    ol.highlighted = i
                    break
        counts = {
            "enabled": sum(1 for p in self._plugins if p.status == "enabled"),
            "disabled": sum(1 for p in self._plugins if p.status == "disabled"),
            "available": sum(1 for p in self._plugins if p.status == "available"),
            "error": sum(1 for p in self._plugins if p.status == "error"),
        }
        self._set_footer(
            f"{counts['enabled']} enabled · {counts['disabled']} disabled · "
            f"{counts['available']} available · {counts['error']} error"
        )

    def _row_text(self, p: PluginEntry) -> str:
        col = _status_color(p.status)
        glyph = _status_glyph(p.status)
        version = f" [dim]v{p.version}[/dim]" if p.version else ""
        source_chip = (
            "[dim](bundled)[/dim]" if p.source == "bundled"
            else "[dim](user)[/dim]"
        )
        # Truncate description so the row stays single-line on narrow terms.
        desc = (p.description or "").splitlines()[0]
        if len(desc) > 50:
            desc = desc[:47] + "…"
        desc_part = f"  [dim]{desc}[/dim]" if desc else ""
        err_part = f"  [red]✗ {p.error}[/red]" if p.error and p.status == "error" else ""
        return (
            f" [{col}]{glyph}[/{col}]  [b]{p.key:<24}[/b]{version}  "
            f"{source_chip}{desc_part}{err_part}"
        )

    # ── interactions ─────────────────────────────────────────────

    def _highlighted_key(self) -> str | None:
        ol = self.query_one(OptionList)
        if ol.highlighted is None:
            return None
        try:
            opt = ol.get_option_at_index(ol.highlighted)
        except Exception:
            return None
        oid = str(opt.id or "")
        if not oid.startswith(_PREFIX):
            return None
        return oid[len(_PREFIX):]

    @on(OptionList.OptionSelected, "#plugins-list")
    def _on_select(self, event: OptionList.OptionSelected) -> None:
        # Enter on a plugin toggles it — single-keystroke UX matches
        # desktop's switch flick.
        oid = str(event.option.id or "")
        if oid.startswith(_PREFIX):
            self._toggle_plugin(oid[len(_PREFIX):])

    def on_key(self, event: events.Key) -> None:
        # Space also toggles — single-key affordance for fast browsing
        # without forcing Enter on every row.
        if event.key == "space":
            key = self._highlighted_key()
            if key:
                event.stop()
                event.prevent_default()
                self._toggle_plugin(key)

    @work
    async def _toggle_plugin(self, key: str) -> None:
        p = next((x for x in self._plugins if x.key == key), None)
        if p is None:
            return
        if p.status == "error":
            self._set_footer(f"[red]{p.key}: {p.error} — fix manifest first[/red]")
            return
        new_enabled = not p.enabled
        try:
            await asyncio.to_thread(set_plugin_enabled, p.key, new_enabled)
        except Exception as exc:
            self._set_footer(f"[red]toggle failed: {exc}[/red]")
            return
        self._changes += 1
        action = "enabled" if new_enabled else "disabled"
        self._set_footer(
            f"✓ {p.key} {action} · restarting gateway…"
        )
        # Plugins register tools at boot — restart so the change takes
        # effect now instead of after the next manual relaunch.
        from flowly.integrations.service_control import restart_gateway
        result = await restart_gateway()
        if result.ok:
            tail = f"gateway restarted ({result.paused_seconds:.1f}s)"
        elif result.method == "no_service":
            tail = "restart gateway manually to apply"
        else:
            tail = f"auto-restart failed: {result.detail}"
        self._set_footer(f"✓ {p.key} {action} · {tail}")
        # Refresh list — status flips, glyph + colour update in place.
        self._rebuild_list()

    def action_reload(self) -> None:
        self._rebuild_list()
        self._set_footer("reloaded")

    def action_dismiss_with_count(self) -> None:
        self.dismiss(
            {"action": "changed", "count": self._changes}
            if self._changes else None
        )

    # ── helpers ──────────────────────────────────────────────────

    def _set_footer(self, text: str) -> None:
        try:
            self.query_one("#plugins-footer", Label).update(text)
        except Exception:
            pass
