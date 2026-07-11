"""MCPModal — manage MCP servers from the TUI.

Mirrors :class:`flowly.tui.panes.plugins_modal.PluginsModal`: one row per
configured MCP server (enabled/disabled/invalid) plus catalog entries not
yet installed (available). Arrow-key navigation, single-key actions:

  Space/Enter — configured: toggle enable/disable · available: install
  d           — remove a configured server
  r           — reload the list

Like plugins, MCP tools register at agent boot, so any change kicks the
gateway to restart (when it runs as a service) so it applies in seconds.
Catalog entries needing a secret/OAuth can't be installed inline (the CLI
prompts for + stores the secret) — they show a hint pointing at
``flowly mcp install <name>``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList
from textual.widgets.option_list import Option

from flowly.integrations.mcp_io import (
    MCPSecretField,
    MCPServerEntry,
    install_catalog_server,
    list_mcp_servers,
    remove_mcp_server,
    set_mcp_enabled,
)

_PREFIX = "MCP:"


class MCPSecretModal(ModalScreen[dict[str, str] | None]):
    """Collect a catalog entry's declared env values before installing.

    Dismisses with ``{name: value}`` on save, or ``None`` on cancel.
    Secret fields use a password input; values are written to
    ``$FLOWLY_HOME/.env`` by the caller (never into config.json).
    """

    DEFAULT_CSS = """
    MCPSecretModal { align: center middle; }
    MCPSecretModal > Vertical {
        width: 70%; max-width: 80; height: auto; max-height: 24;
        padding: 1 2; border: thick $primary; background: $surface;
    }
    MCPSecretModal .title { text-style: bold; color: $primary; height: 1; }
    MCPSecretModal .hint {
        color: $text-muted; text-style: italic; height: auto; margin-bottom: 1;
    }
    MCPSecretModal Input { height: 3; margin-bottom: 1; }
    MCPSecretModal .flabel { color: $text-muted; height: 1; }
    MCPSecretModal #buttons { height: auto; margin-top: 1; }
    MCPSecretModal #buttons Button { margin-right: 1; }
    """

    AUTO_FOCUS = "Input"
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, name: str, fields: list[MCPSecretField]) -> None:
        super().__init__()
        self._name = name
        self._fields = fields

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Configure {self._name}", classes="title")
            yield Label(
                "Stored in ~/.flowly/.env — config.json only keeps a "
                "${VAR} reference. Esc cancels.",
                classes="hint",
            )
            for f in self._fields:
                yield Label(f.prompt, classes="flabel")
                yield Input(
                    value=f.default,
                    password=f.secret,
                    placeholder=f.name,
                    id=f"sf-{f.name}",
                )
            with Vertical(id="buttons"):
                yield Button("Install", id="sf-save", variant="primary")
                yield Button("Cancel  (Esc)", id="sf-cancel")

    @on(Button.Pressed, "#sf-save")
    def _save(self) -> None:
        values: dict[str, str] = {}
        for f in self._fields:
            try:
                inp = self.query_one(f"#sf-{f.name}", Input)
            except Exception:
                continue
            values[f.name] = inp.value
        self.dismiss(values)

    @on(Button.Pressed, "#sf-cancel")
    def _cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _status_color(status: str) -> str:
    return {
        "enabled":   "green",
        "disabled":  "yellow",
        "available": "cyan",
        "invalid":   "red",
    }.get(status, "dim")


def _status_glyph(status: str) -> str:
    return {
        "enabled":   "●",
        "disabled":  "○",
        "available": "◇",
        "invalid":   "✗",
    }.get(status, "·")


class MCPPanel(Vertical):
    """Dismisses with:
      {'action': 'changed', 'count': N}  → N changes this session
      {'action': 'install_secret', 'name': str, 'fields': list[MCPSecretField]}
      None                                → no changes
    """

    can_focus = True

    class Dismissed(Message):
        def __init__(self, result: dict[str, Any] | None) -> None:
            super().__init__()
            self.result = result

    DEFAULT_CSS = """
    MCPPanel {
        width: 100%;
        max-width: 100%;
        height: auto;
        max-height: 24;
        padding: 0;
        border: none;
        background: transparent;
    }
    MCPPanel .title { text-style: bold; color: $primary; height: 1; }
    MCPPanel .hint {
        color: $text-muted; text-style: italic; height: 1; margin-bottom: 1;
    }
    MCPPanel OptionList { height: 16; border: none; background: transparent; }
    MCPPanel .footer {
        color: $text-muted; text-style: italic; height: auto; margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "dismiss_with_count", "Close"),
        ("q",      "dismiss_with_count", "Close"),
        ("r",      "reload",             "Reload list"),
        ("d",      "remove",             "Remove server"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._servers: list[MCPServerEntry] = []
        self._changes = 0

    # ── layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Label("MCP servers", classes="title")
        yield Label(
            "↑/↓ navigate · Space/Enter toggle/install · D remove · "
            "R reload · Esc close",
            classes="hint",
        )
        yield OptionList(id="mcp-list")
        yield Label("", id="mcp-footer", classes="footer")

    def on_mount(self) -> None:
        self._rebuild_list()
        ol = self.query_one(OptionList)
        if ol.options:
            ol.highlighted = 0
        ol.focus()

    def on_focus(self) -> None:
        try:
            self.query_one(OptionList).focus()
        except Exception:
            pass

    def _rebuild_list(self) -> None:
        self._servers = list_mcp_servers()
        ol = self.query_one(OptionList)
        prev_name: str | None = None
        if ol.highlighted is not None and ol.highlighted < len(ol.options):
            try:
                opt_id = str(ol.get_option_at_index(ol.highlighted).id or "")
                if opt_id.startswith(_PREFIX):
                    prev_name = opt_id[len(_PREFIX):]
            except Exception:
                pass
        ol.clear_options()
        if not self._servers:
            ol.add_option(Option(
                "[dim]No MCP servers. Add one with `flowly mcp add` or pick "
                "from `flowly mcp catalog`.[/dim]",
                id="EMPTY", disabled=True,
            ))
            self._set_footer("")
            return
        for s in self._servers:
            ol.add_option(Option(self._row_text(s), id=f"{_PREFIX}{s.name}"))
        if prev_name:
            for i, s in enumerate(self._servers):
                if s.name == prev_name:
                    ol.highlighted = i
                    break
        counts = {
            "enabled": sum(1 for s in self._servers if s.status == "enabled"),
            "disabled": sum(1 for s in self._servers if s.status == "disabled"),
            "available": sum(1 for s in self._servers if s.status == "available"),
            "invalid": sum(1 for s in self._servers if s.status == "invalid"),
        }
        self._set_footer(
            f"{counts['enabled']} enabled · {counts['disabled']} disabled · "
            f"{counts['available']} available · {counts['invalid']} invalid"
        )

    def _row_text(self, s: MCPServerEntry) -> str:
        col = _status_color(s.status)
        glyph = _status_glyph(s.status)
        auth_chip = " [magenta]oauth[/magenta]" if s.auth == "oauth" else ""
        if s.source == "catalog":
            tail = f"  [dim]{s.description}[/dim]"
            if s.needs_secrets:
                tail += "  [dim](will prompt for a key)[/dim]"
            elif s.needs_oauth:
                tail += "  [dim](installs · then /mcp login)[/dim]"
        else:
            tail = f"  [dim]{s.transport}[/dim]  [dim]tools: {s.tool_filter}[/dim]"
            if s.error:
                tail += f"  [red]✗ {s.error}[/red]"
        return f" [{col}]{glyph}[/{col}]  [b]{s.name:<20}[/b]{auth_chip}{tail}"

    # ── interactions ─────────────────────────────────────────────

    def _highlighted(self) -> MCPServerEntry | None:
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
        name = oid[len(_PREFIX):]
        return next((s for s in self._servers if s.name == name), None)

    @on(OptionList.OptionSelected, "#mcp-list")
    def _on_select(self, event: OptionList.OptionSelected) -> None:
        oid = str(event.option.id or "")
        if oid.startswith(_PREFIX):
            name = oid[len(_PREFIX):]
            entry = next((s for s in self._servers if s.name == name), None)
            if entry:
                self._activate(entry)

    def on_key(self, event: events.Key) -> None:
        if event.key == "space":
            entry = self._highlighted()
            if entry:
                event.stop()
                event.prevent_default()
                self._activate(entry)

    def _activate(self, entry: MCPServerEntry) -> None:
        """Space/Enter: toggle a configured server, or install a catalog one."""
        if entry.source == "catalog":
            self._install(entry)
        else:
            self._toggle(entry)

    @work
    async def _toggle(self, entry: MCPServerEntry) -> None:
        if entry.status == "invalid":
            self._set_footer(f"[red]{entry.name}: {entry.error} — fix config first[/red]")
            return
        new_enabled = not entry.enabled
        try:
            await asyncio.to_thread(set_mcp_enabled, entry.name, new_enabled)
        except Exception as exc:
            self._set_footer(f"[red]toggle failed: {exc}[/red]")
            return
        self._changes += 1
        action = "enabled" if new_enabled else "disabled"
        await self._restart_and_report(f"{entry.name} {action}")
        self._rebuild_list()

    @work
    async def _install(self, entry: MCPServerEntry) -> None:
        # Collect any declared secrets/values inside the TUI (no CLI hop).
        env_values: dict[str, str] = {}
        if entry.needs_secrets:
            self.post_message(self.Dismissed({
                "action": "install_secret",
                "name": entry.name,
                "fields": entry.secret_fields or [],
            }))
            return
        try:
            ok, msg = await asyncio.to_thread(
                install_catalog_server, entry.name, env_values,
            )
        except Exception as exc:
            self._set_footer(f"[red]install failed: {exc}[/red]")
            return
        if not ok:
            self._set_footer(f"[yellow]{msg}[/yellow]")
            return
        self._changes += 1
        await self._restart_and_report(msg)
        self._rebuild_list()

    @work
    async def _remove_worker(self, entry: MCPServerEntry) -> None:
        try:
            removed = await asyncio.to_thread(remove_mcp_server, entry.name)
        except Exception as exc:
            self._set_footer(f"[red]remove failed: {exc}[/red]")
            return
        if not removed:
            self._set_footer(f"[yellow]{entry.name} not configured[/yellow]")
            return
        self._changes += 1
        await self._restart_and_report(f"removed {entry.name}")
        self._rebuild_list()

    def action_remove(self) -> None:
        entry = self._highlighted()
        if entry is None:
            return
        if entry.source != "configured":
            self._set_footer("[dim]Only configured servers can be removed.[/dim]")
            return
        self._remove_worker(entry)

    async def _restart_and_report(self, what: str) -> None:
        """Restart the gateway (MCP loads at boot) and report the outcome."""
        self._set_footer(f"✓ {what} · restarting gateway…")
        from flowly.integrations.service_control import restart_gateway
        result = await restart_gateway()
        if result.ok:
            tail = f"gateway restarted ({result.paused_seconds:.1f}s)"
        elif result.method == "no_service":
            tail = "restart gateway manually to apply"
        else:
            tail = f"auto-restart failed: {result.detail}"
        self._set_footer(f"✓ {what} · {tail}")

    def action_reload(self) -> None:
        self._rebuild_list()
        self._set_footer("reloaded")

    def action_dismiss_with_count(self) -> None:
        self.post_message(self.Dismissed(
            {"action": "changed", "count": self._changes}
            if self._changes else None
        ))

    # ── helpers ──────────────────────────────────────────────────

    def _set_footer(self, text: str) -> None:
        try:
            self.query_one("#mcp-footer", Label).update(text)
        except Exception:
            pass


class MCPModal(ModalScreen[dict[str, Any] | None]):
    """Compatibility wrapper; the chat TUI mounts :class:`MCPPanel`."""

    BINDINGS = MCPPanel.BINDINGS

    DEFAULT_CSS = """
    MCPModal { align: center middle; }
    MCPModal > MCPPanel {
        width: 85%;
        max-width: 100;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield MCPPanel()

    @on(MCPPanel.Dismissed)
    def _on_dismissed(self, event: MCPPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(event.result)

    def action_reload(self) -> None:
        self.query_one(MCPPanel).action_reload()

    def action_remove(self) -> None:
        self.query_one(MCPPanel).action_remove()

    def action_dismiss_with_count(self) -> None:
        self.query_one(MCPPanel).action_dismiss_with_count()
