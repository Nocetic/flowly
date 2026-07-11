"""BrowserModal — toggle browser_tab tool + Chrome extension link.

Opened via the ``/browser`` slash. Mirrors the desktop app's "Browser
Use" tile (``Dashboard/IntegrationsTab.tsx:592``): one toggle that
writes ``tools.browser_tab.enabled`` to ``~/.flowly/config.json`` plus
a clickable link to the Flowly Chrome extension on the Chrome Web Store
for users who haven't installed it yet.

Why a dedicated modal instead of stuffing this into ``/integrations``:
browser_tab is special — it needs both a config flag AND a
browser-side extension installed AND the side panel open with tabs
added to the "Flowly" group. Pulling all that context into one focused
screen makes the failure modes (extension missing, not connected,
group empty) obvious in one place instead of scattered across the
integrations grid.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Label, Static, Switch


class _LinkLabel(Static):
    """Click-to-open URL widget.

    Textual's default Content markup parser rejects ``[link=URL]`` when
    the URL contains ``://`` (the parser treats it as an attribute
    boundary), so we render the URL as plain text + handle ``Click``
    events ourselves. ``on_click`` works regardless of the parent's
    markup setting and survives a ``markup=False`` Static.
    """

    DEFAULT_CSS = """
    _LinkLabel {
        height: auto;
        color: #5cd2ff;
        text-style: underline;
        padding: 0;
    }
    _LinkLabel:hover {
        color: #a5e8ff;
        text-style: bold underline;
        background: $boost;
    }
    """

    def __init__(self, url: str, label: str | None = None) -> None:
        # Display the URL itself by default — users copying the line
        # still see what they're clicking on. Pass ``label`` to override
        # for shorter chips.
        super().__init__(label or url, markup=False)
        self._url = url

    def on_click(self, event: events.Click) -> None:
        if _open_browser_detached(self._url):
            self.app.notify(f"opened in browser: {self._url}",
                            timeout=3, severity="information")
        else:
            self.app.notify(
                f"could not open browser — copy manually: {self._url}",
                timeout=6, severity="warning",
            )
        event.stop()


# Chrome Web Store listing — kept in sync with desktop
# (``Dashboard/IntegrationsTab.tsx:596``). If the extension is relisted
# under a new id, update this constant in both places.
_CHROME_STORE_URL = (
    "https://chromewebstore.google.com/detail/flowly-in-chrome/"
    "nagcplpapfpnabcfkghdhpnogblidaig"
)


def _open_browser_detached(url: str) -> bool:
    """Open ``url`` in the user's default browser without inheriting any
    of Textual's file descriptors. Mirrors the helper in login_modal —
    Python 3.14's posix_subprocess rejects Textual's high-numbered fds
    if we don't set ``close_fds=True``."""
    if sys.platform == "darwin":
        cmd = ["open", url]
    elif sys.platform == "win32":
        cmd = ["cmd", "/c", "start", "", url]
    else:
        cmd = ["xdg-open", url]
    try:
        from flowly.utils.subprocess_compat import detach_kwargs
        subprocess.Popen(
            cmd,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # POSIX: start_new_session=True. Windows: CREATE_NO_WINDOW |
            # DETACHED_PROCESS (stdio is DEVNULL, so detach is safe here).
            **detach_kwargs(),
        )
        return True
    except (OSError, FileNotFoundError, ValueError):
        return False


class BrowserPanel(Vertical):
    """Dismisses with:
      {'action': 'saved', 'enabled': bool}  → caller can refresh status bar
      None                                  → cancel
    """

    can_focus = True

    class Dismissed(Message):
        def __init__(self, result: dict | None) -> None:
            super().__init__()
            self.result = result

    DEFAULT_CSS = """
    BrowserPanel {
        width: 100%;
        max-width: 100%;
        height: auto;
        max-height: 24;
        padding: 0;
        border: none;
        background: transparent;
    }
    BrowserPanel .modal-header {
        height: auto;
        margin-bottom: 1;
    }
    BrowserPanel .eyebrow {
        color: $text-muted;
        height: 1;
    }
    BrowserPanel .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    BrowserPanel .description {
        color: $text;
        height: auto;
        margin-bottom: 1;
    }
    BrowserPanel .checklist {
        layout: vertical;
        height: auto;
        margin-bottom: 1;
        background: transparent;
    }
    BrowserPanel .checklist-title {
        color: $primary;
        text-style: bold;
        height: 1;
    }
    BrowserPanel .checklist-item {
        height: auto;
        color: $text;
    }
    BrowserPanel .toggle-row {
        layout: horizontal;
        height: 3;
        margin-bottom: 1;
    }
    BrowserPanel .toggle-row > Switch { margin-right: 2; }
    BrowserPanel .toggle-row > Label { width: 1fr; padding-top: 1; }
    BrowserPanel .install-block {
        layout: vertical;
        height: auto;
        margin-bottom: 1;
        border: none;
        background: transparent;
    }
    BrowserPanel .install-title {
        height: 1;
        color: $primary;
        text-style: bold;
    }
    BrowserPanel #status-line {
        height: auto;
        min-height: 1;
        color: $text-muted;
        margin-top: 1;
        background: transparent;
    }
    BrowserPanel #status-line.ok    { color: green; }
    BrowserPanel #status-line.warn  { color: yellow; }
    BrowserPanel #status-line.error { color: red; }
    BrowserPanel #browser-footer {
        height: auto;
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("s", "save", "Save"),
        ("r", "refresh", "Refresh"),
        ("o", "open_store", "Open store"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._initial_enabled: bool = False
        self._extension_connected: bool | None = None
        self._saved_result: dict | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-header"):
            yield Label(
                "Browser controls · local Chrome extension",
                id="browser-status-chip",
                classes="eyebrow",
            )
            yield Label("Browser Use", classes="title")
            yield Static(
                "Let the agent interact with web pages in your real Chrome "
                "via the Flowly extension. Pages animate with a cyan glow "
                "while the agent works so you see every action live.",
                classes="description",
                markup=False,
            )

        with Vertical(classes="checklist"):
            yield Static("Readiness checklist", classes="checklist-title")
            yield Label("○ Config loading…", id="item-config", classes="checklist-item")
            yield Label("○ Gateway checking…", id="item-gateway", classes="checklist-item")
            yield Label("○ Extension checking…", id="item-extension", classes="checklist-item")

        with Horizontal(classes="toggle-row"):
            yield Switch(value=False, id="enabled-switch")
            yield Label("Enable [b]browser_tab[/b] tool", markup=True)

        with Vertical(classes="install-block"):
            yield Static("Extension install", classes="install-title")
            yield _LinkLabel(_CHROME_STORE_URL, label="Open Chrome Web Store")
            yield Static(
                "[dim]Install the extension, open its side panel, then "
                "refresh this status.[/dim]",
                markup=True,
            )

        yield Label("", id="status-line")

        yield Static("", id="browser-footer", markup=False)

    # ── lifecycle ────────────────────────────────────────────────

    async def on_mount(self) -> None:
        # Hydrate current state from config + live gateway.
        await self._refresh_state()
        self._focus_toggle()

    def on_focus(self) -> None:
        self._focus_toggle()

    def _focus_toggle(self) -> None:
        try:
            self.query_one("#enabled-switch", Switch).focus()
        except Exception:
            pass

    async def _refresh_state(self) -> None:
        # 1. Config flag — single source of truth for tool registration.
        from flowly.config.loader import load_config
        try:
            cfg = load_config()
            self._initial_enabled = bool(cfg.tools.browser_tab.enabled)
        except Exception:
            self._initial_enabled = False
        try:
            sw = self.query_one("#enabled-switch", Switch)
            sw.value = self._initial_enabled
        except Exception:
            pass
        try:
            row = self.query_one("#item-config", Label)
            mark = "[green]●[/] Enabled in config" if self._initial_enabled \
                   else "[dim]○ Disabled in config[/]"
            row.update(mark)
        except Exception:
            pass

        # 2. Live extension status from /api/extension/status — tells the
        # user whether Chrome is actually talking to the gateway right
        # now (separate from the enabled flag).
        await self._refresh_extension_status()
        self._sync_primary_action()

    async def _refresh_extension_status(self) -> None:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get("http://127.0.0.1:18790/api/extension/status")
            if r.status_code == 200:
                self._update_gateway("[green]●[/] Gateway reachable")
                data = r.json()
                self._extension_connected = bool(data.get("connected"))
                count = int(data.get("client_count") or 0)
                if self._extension_connected:
                    detail = (
                        "[green]●[/] Extension connected" +
                        (f"  [dim]({count} clients)[/]" if count > 1 else "")
                    )
                else:
                    detail = "[yellow]○[/] Extension not connected"
            else:
                self._update_gateway(f"[yellow]○[/] Gateway HTTP {r.status_code}")
                self._extension_connected = None
                detail = "[dim]○ Extension status unavailable[/]"
        except Exception:
            self._update_gateway("[dim]○ Gateway offline[/]")
            self._extension_connected = None
            detail = "[dim]○ Extension status unavailable[/]"
        try:
            row = self.query_one("#item-extension", Label)
            row.update(detail)
        except Exception:
            pass
        self._sync_header_status()

    # ── actions ──────────────────────────────────────────────────

    def action_close(self) -> None:
        self.post_message(self.Dismissed(self._saved_result))

    def action_open_store(self) -> None:
        ok = _open_browser_detached(_CHROME_STORE_URL)
        if ok:
            self._set_status(
                "✓ opened Chrome Web Store — install + pin the extension, "
                "then reopen this modal to verify connection.",
                "ok",
            )
        else:
            self._set_status(
                f"could not auto-open. Copy the link manually: {_CHROME_STORE_URL}",
                "warn",
            )

    def action_refresh(self) -> None:
        self._set_status("refreshing browser status…")
        self._run_refresh()

    @work
    async def _run_refresh(self) -> None:
        await self._refresh_state()
        self._set_status("status refreshed", "ok")

    def action_save(self) -> None:
        if self._saved_result is not None:
            self.post_message(self.Dismissed(self._saved_result))
            return
        self._run_save()

    @on(Switch.Changed, "#enabled-switch")
    def _toggle_changed(self) -> None:
        self._sync_primary_action()

    @work
    async def _run_save(self) -> None:
        try:
            sw = self.query_one("#enabled-switch", Switch)
            new_enabled = bool(sw.value)
        except Exception:
            self._set_status("could not read toggle state", "error")
            return

        if new_enabled == self._initial_enabled:
            self.post_message(self.Dismissed(self._saved_result))
            return

        # Write atomically through the same path /integrations uses so
        # the file stays consistent (camelCase + 0600 perms + backup).
        try:
            await asyncio.to_thread(_write_browser_tab_enabled, new_enabled)
        except Exception as exc:
            self._set_status(f"save failed: {exc}", "error")
            return

        # The browser_tab tool registers at gateway boot — toggling the
        # config has no effect until the gateway picks it up. Restart
        # via launchd so the change applies in seconds without the user
        # touching a terminal.
        self._set_status(
            "✓ saved · restarting gateway so browser_tab "
            f"{'registers' if new_enabled else 'unregisters'}…",
            "",
        )
        from flowly.integrations.service_control import restart_gateway
        result = await restart_gateway()
        if result.ok:
            tail = (
                f"gateway restarted via {result.method} "
                f"({result.paused_seconds:.1f}s)"
            )
        elif result.method == "no_service":
            tail = f"[yellow]{result.detail}[/yellow]"
        else:
            tail = f"[red]auto-restart failed: {result.detail}[/red]"
        self._set_status(
            f"✓ browser_tab "
            f"{'enabled' if new_enabled else 'disabled'} · {tail}",
            "ok",
        )
        self._initial_enabled = new_enabled
        self._saved_result = {"action": "saved", "enabled": new_enabled}
        self._after_saved()

    # ── helpers ──────────────────────────────────────────────────

    def _update_gateway(self, text: str) -> None:
        try:
            self.query_one("#item-gateway", Label).update(text)
        except Exception:
            pass

    def _sync_header_status(self) -> None:
        if self._initial_enabled and self._extension_connected:
            text = "[green]Ready[/] [dim]· browser actions can run[/dim]"
        elif self._initial_enabled:
            text = "[yellow]Needs extension[/] [dim]· install/open Chrome side panel[/dim]"
        else:
            text = "[dim]Disabled[/] [dim]· enable and restart to register tool[/dim]"
        try:
            self.query_one("#browser-status-chip", Label).update(text)
        except Exception:
            pass

    def _sync_primary_action(self) -> None:
        try:
            sw = self.query_one("#enabled-switch", Switch)
            footer = self.query_one("#browser-footer", Static)
        except Exception:
            return
        new_enabled = bool(sw.value)
        if self._saved_result is not None or new_enabled == self._initial_enabled:
            action = "no unsaved changes"
        elif new_enabled:
            action = "S enable + restart"
        else:
            action = "S disable + restart"
        footer.update(f"{action} · R refresh · O open store · Esc close")

    def _after_saved(self) -> None:
        self._sync_primary_action()
        self._sync_header_status()

    def _set_status(self, msg: str, kind: str = "") -> None:
        try:
            line = self.query_one("#status-line", Label)
            line.update(msg)
            line.set_classes("")
            if kind:
                line.add_class(kind)
        except Exception:
            pass


class BrowserModal(ModalScreen[dict | None]):
    """Compatibility wrapper; the chat TUI mounts :class:`BrowserPanel`."""

    BINDINGS = BrowserPanel.BINDINGS

    DEFAULT_CSS = """
    BrowserModal { align: center middle; }
    BrowserModal > BrowserPanel {
        width: 75%;
        max-width: 86;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield BrowserPanel()

    @on(BrowserPanel.Dismissed)
    def _on_dismissed(self, event: BrowserPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(event.result)

    def action_close(self) -> None:
        self.query_one(BrowserPanel).action_close()


def _write_browser_tab_enabled(enabled: bool) -> None:
    """Persist ``tools.browser_tab.enabled`` atomically. Mirrors the
    pattern in :func:`set_active_provider` so on-disk writes share one
    helper and never race."""
    from flowly.config.loader import get_config_path
    from flowly.integrations.config_io import (
        _atomic_write_json,
        _load_raw,
        _set_path,
    )
    raw = _load_raw()
    _set_path(raw, "tools.browser_tab", {"enabled": enabled}, merge=True)
    _atomic_write_json(get_config_path(), raw)
