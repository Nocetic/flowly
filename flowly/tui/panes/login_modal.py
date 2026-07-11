"""LoginModal — in-TUI device-code authorization screen.

Launched by `/login` slash command. Drives the Flowly device-code handshake
without leaving the terminal: shows the user code in a bordered panel,
opens the authorization URL in the user's browser, polls until the web
side completes, then dismisses with the resulting ``Account``.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from flowly.account.auth import Account, LoginTimeout, run_login_flow
from flowly.account.firebase_rest import FirebaseAuthError


def _format_code(code: str) -> str:
    if len(code) >= 6:
        mid = len(code) // 2
        return f"{code[:mid]}-{code[mid:]}"
    return code


def _open_browser_detached(url: str) -> bool:
    """Open ``url`` in the user's default browser without inheriting any
    of Textual's file descriptors.

    Why not ``webbrowser.open()``: Textual keeps a number of high-fd
    handles (memfd, eventfd, pipes) for its render loop. Python's
    builtin ``webbrowser`` module forks without ``close_fds=True``, and
    on Python 3.14 the posix_subprocess module is stricter about
    validating the inherited fd set — invalid handles surface as
    ``ValueError: bad value(s) in fds_to_keep`` and abort the call.

    Spawning explicitly with ``close_fds=True``, redirected std streams,
    and ``start_new_session=True`` fully detaches the child so it can't
    write into our TTY either.
    """
    if sys.platform == "darwin":
        cmd = ["open", url]
    elif sys.platform == "win32":
        # cmd /c start needs an empty title arg, else it consumes the URL
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
    except (OSError, FileNotFoundError, ValueError) as exc:
        # ValueError covers Python 3.14's 'bad value(s) in fds_to_keep'
        # if anything still slips through. Silently fall back — user can
        # copy the URL from the modal manually.
        try:
            from flowly.account import audit_log
            audit_log.warn("browser.open.failed",
                           cmd=cmd[0], error=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        return False


class LoginPanel(Vertical):
    """Composer-inline device authorization state machine."""

    can_focus = True

    class Dismissed(Message):
        def __init__(self, result: Account | None) -> None:
            super().__init__()
            self.result = result

    DEFAULT_CSS = """
    LoginPanel {
        width: 100%;
        max-width: 100%;
        height: auto;
        max-height: 24;
        padding: 0;
        border: none;
        background: transparent;
    }
    LoginPanel .eyebrow {
        color: $text-muted;
        height: 1;
    }
    LoginPanel .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    LoginPanel .hint {
        color: $text-muted;
        height: auto;
    }
    LoginPanel .url-line {
        color: $accent;
        height: auto;
    }
    LoginPanel .code-box {
        color: $accent;
        text-style: bold;
        height: 1;
    }
    LoginPanel .steps {
        height: auto;
        margin: 1 0;
    }
    LoginPanel .step {
        height: auto;
        color: $text;
    }
    LoginPanel .status {
        color: $text-muted;
        height: auto;
        min-height: 1;
        margin-top: 1;
    }
    LoginPanel .status.ok    { color: green; }
    LoginPanel .status.error { color: red; }
    LoginPanel #login-relay {
        height: auto;
        margin-top: 1;
        color: $text;
    }
    LoginPanel #login-footer {
        height: auto;
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._code: str | None = None
        self._url: str | None = None
        self._task: asyncio.Task[Account] | None = None
        self._account: Account | None = None
        self._want_relay = False

    def compose(self) -> ComposeResult:
        yield Label("Account authorization", classes="eyebrow")
        yield Label("Sign in to Flowly", classes="title")
        yield Label(
            "The browser opens automatically. Keep this prompt open until authorization completes.",
            classes="hint",
        )
        yield Static("", id="login-relay", markup=False)
        with Vertical(classes="steps"):
            yield Label("○ Requesting device code", id="step-code", classes="step")
            yield Label("○ Opening browser", id="step-browser", classes="step")
            yield Label("○ Waiting for authorization", id="step-auth", classes="step")
            yield Label("○ Registering this machine", id="step-machine", classes="step")
        yield Static(
            "Fetching authorization URL…",
            id="login-url",
            classes="url-line",
            markup=False,
        )
        yield Static(
            "[dim]code: …[/dim]",
            id="login-code",
            classes="code-box",
            markup=True,
        )
        yield Label("requesting code…", id="login-status", classes="status")
        yield Static("", id="login-footer", markup=False)

    def on_mount(self) -> None:
        self._render_relay()
        self._render_footer()
        self._task = asyncio.create_task(self._drive())

    def on_unmount(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _drive(self) -> None:
        try:
            account = await run_login_flow(
                on_code=self._show_code,
                on_status=self._set_status,
            )
        except LoginTimeout:
            self._set_status("code expired — close and try again", "error")
            self._set_step("step-auth", "error", "Authorization timed out")
            return
        except FirebaseAuthError as exc:
            self._set_status(f"login failed: {exc}", "error")
            self._set_step("step-auth", "error", "Authorization failed")
            return
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._set_status(f"unexpected error: {exc}", "error")
            self._set_step("step-auth", "error", "Authorization failed")
            return

        # Auto-register this machine as a Firestore server. Reuses existing
        # entry by machineId if one exists (e.g. desktop already installed
        # here). Non-fatal on failure — the user is still logged in.
        self._set_step("step-auth", "ok", "Authorized")

        # Auto-provision the account-key provider (Source 0) so the user is
        # billed immediately without dealing with keys. Transparent + best-effort
        # (run off the event loop so the UI doesn't stall).
        try:
            from flowly.account.account_key import ensure_account_key
            await asyncio.to_thread(ensure_account_key, account)
        except Exception:  # noqa: BLE001
            pass

        # Reach: remote / phone access via the relay is OPT-IN (it registers a
        # server). Honour the inline toggle — default OFF = provider only.
        if not self._want_relay:
            self._set_step("step-machine", "ok", "Skipped — no relay (provider only)")
            self._set_status(
                "✓ signed in — Flowly provider ready, billed to your account", "ok"
            )
            self._finish(account)
            return

        self._set_step("step-machine", "pending", "Registering this machine")
        self._set_status("registering this machine…")
        try:
            from flowly.account.auth import save_account
            from flowly.account.relay_config import wire_relay_credentials
            from flowly.account.server import register_machine
            srv = await register_machine(account.id_token)
            account.server_id = srv.server_id
            account.server_name = srv.name
            account.gateway_auth_token = srv.gateway_auth_token
            save_account(account)  # persist updated token bundle
            # Wire relay credentials into the gateway config so a future
            # gateway start auto-connects to wss://relay.useflowlyapp.com.
            # This is the bridge that makes cross-device sync work
            # WITHOUT any direct Firestore client in the TUI.
            change = wire_relay_credentials(srv)
            verb = "reusing" if srv.existing else "registered"
            # Auto-promote Flowly to the default LLM provider when nothing
            # was set before. Avoids the "I logged in but the gateway still
            # complains about no API key" trap.
            from flowly.config.loader import load_config
            from flowly.integrations.active_provider import set_active_provider
            try:
                if not (load_config().providers.active or "").strip():
                    set_active_provider("flowly")
                    promoted = True
                else:
                    promoted = False
            except Exception:
                promoted = False
            tails: list[str] = []
            if change.needs_gateway_restart:
                tails.append("restart gateway to activate sync")
            elif change.changed:
                tails.append("sync enabled")
            if promoted:
                tails.append("Flowly set as default provider")
            tail = " · " + " · ".join(tails) if tails else ""
            self._set_status(
                f"✓ signed in · {verb} server [{srv.name}]{tail}", "ok"
            )
            self._set_step("step-machine", "ok", f"Machine {verb}")
        except Exception as exc:
            # Login succeeded but registration didn't — keep the user
            # signed in so they can manually retry via /whoami; warn.
            self._set_status(
                f"signed in but registration failed: {exc} — /whoami to retry",
                "error",
            )
            self._set_step("step-machine", "error", "Machine registration failed")
            self._finish(account)
            return

        self._finish(account)

    def _show_code(self, code: str, url: str) -> None:
        self._code = code
        self._url = url
        try:
            self.query_one("#login-url", Static).update(f"Authorization URL: {url}")
            # Code is shown as fallback (in case browser auto-open fails and
            # user has to type the code on the page manually).
            self.query_one("#login-code", Static).update(
                f"[dim]if needed, enter code:[/dim] {_format_code(code)}"
            )
        except Exception:
            pass
        self._set_step("step-code", "ok", "Device code ready")
        # Best-effort browser open — detached subprocess (see helper).
        if _open_browser_detached(url):
            self._set_step("step-browser", "ok", "Browser opened")
        else:
            self._set_step("step-browser", "error", "Browser did not open")
            self._set_status("could not open browser automatically", "error")
        self._set_step("step-auth", "pending", "Waiting for authorization")

    def _set_status(self, msg: str, kind: str = "status") -> None:
        try:
            label = self.query_one("#login-status", Label)
            label.update(msg)
            label.set_classes("status")
            if kind == "ok":
                label.add_class("ok")
            elif kind == "error":
                label.add_class("error")
        except Exception:
            pass

    def _open(self) -> None:
        if not self._url:
            self._set_status("authorization URL is not ready yet")
            return
        if _open_browser_detached(self._url):
            self._set_status("opened authorization page", "ok")
            self._set_step("step-browser", "ok", "Browser opened")
        else:
            self._set_status(f"could not open browser — copy URL manually: {self._url}", "error")
            self._set_step("step-browser", "error", "Browser did not open")

    def _copy_code(self) -> None:
        if not self._code:
            self._set_status("device code is not ready yet")
            return
        try:
            copier = getattr(self.app, "copy_to_clipboard")
            copier(self._code)
        except Exception:
            self._set_status(f"copy unavailable — code: {_format_code(self._code)}", "error")
            return
        self._set_status("code copied to clipboard", "ok")

    def action_close(self) -> None:
        if self._account is not None:
            self.post_message(self.Dismissed(self._account))
            return
        if self._task and not self._task.done():
            self._task.cancel()
        self.post_message(self.Dismissed(None))

    def _finish(self, account: Account) -> None:
        self._account = account
        self._render_footer()

    def _render_relay(self) -> None:
        mark = "[x]" if self._want_relay else "[ ]"
        try:
            self.query_one("#login-relay", Static).update(
                f"{mark} Remote phone / Flowly relay access (R toggle)"
            )
        except Exception:
            pass

    def _render_footer(self) -> None:
        hint = (
            "Enter done · Esc close"
            if self._account is not None
            else "R relay · O open browser · C copy code · Esc cancel"
        )
        try:
            self.query_one("#login-footer", Static).update(hint)
        except Exception:
            pass

    def on_key(self, event: events.Key) -> None:
        key = event.key.lower()
        handled = True
        if key == "escape":
            self.action_close()
        elif key in {"enter", "return"} and self._account is not None:
            self.action_close()
        elif key == "r" and self._account is None:
            self._want_relay = not self._want_relay
            self._render_relay()
        elif key == "o" and self._account is None:
            self._open()
        elif key == "c" and self._account is None:
            self._copy_code()
        else:
            handled = False
        if handled:
            event.stop()
            event.prevent_default()

    def _set_step(self, widget_id: str, kind: str, label: str) -> None:
        mark = {
            "ok": "[green]●[/]",
            "error": "[red]●[/]",
            "pending": "[yellow]○[/]",
        }.get(kind, "[dim]○[/]")
        try:
            self.query_one(f"#{widget_id}", Label).update(f"{mark} {label}")
        except Exception:
            pass


class LoginModal(ModalScreen[Account | None]):
    """Compatibility wrapper used by the standalone setup application."""

    DEFAULT_CSS = """
    LoginModal { align: center middle; }
    LoginModal > LoginPanel {
        width: 70%;
        max-width: 80;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield LoginPanel()

    @on(LoginPanel.Dismissed)
    def _on_dismissed(self, event: LoginPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(event.result)
