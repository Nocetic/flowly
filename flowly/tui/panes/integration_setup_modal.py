"""IntegrationSetupModal — per-integration form with Test + Save.

Renders one :class:`flowly.integrations.IntegrationCard` as an editable form,
runs the card's probe on demand ("Test Connection"), then writes the values
back to ``~/.flowly/config.json`` atomically.

After Save the user is told whether a gateway restart is required (channels
and tools yes; LLM provider keys no — they're re-read per request).
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch

from flowly.integrations import (
    Field,
    FieldType,
    IntegrationCard,
    apply_card_values,
    read_card_values,
)
from flowly.integrations.probes import run_with_timeout


class IntegrationSetupModal(ModalScreen[dict[str, Any] | None]):
    """Dismisses with one of:
      {'action': 'saved',   'key': card.key}
      {'action': 'tested',  'key': card.key, 'status': ...}   (rare — usually
        modal stays open while testing)
      None                                                    (cancelled)
    """

    DEFAULT_CSS = """
    IntegrationSetupModal { align: center middle; }
    IntegrationSetupModal > Vertical {
        width: 75%;
        max-width: 88;
        height: 85%;
        max-height: 34;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    IntegrationSetupModal .modal-header {
        height: auto;
        margin-bottom: 1;
    }
    IntegrationSetupModal .eyebrow {
        color: $text-muted;
        height: 1;
    }
    IntegrationSetupModal .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    IntegrationSetupModal .description {
        color: $text;
        height: auto;
    }
    IntegrationSetupModal .docs {
        color: $accent;
        height: auto;
        margin-top: 1;
    }
    IntegrationSetupModal .setup-guide {
        height: auto;
        margin-bottom: 1;
        padding: 1;
        background: $boost;
        border-left: thick $primary;
    }
    IntegrationSetupModal .setup-guide-title {
        color: $primary;
        text-style: bold;
        height: 1;
    }
    IntegrationSetupModal .setup-guide-line {
        color: $text;
        height: auto;
    }
    IntegrationSetupModal .section-title {
        color: $primary;
        text-style: bold;
        height: 1;
        margin-top: 1;
        margin-bottom: 1;
    }
    IntegrationSetupModal VerticalScroll {
        height: 1fr;
        border: none;
        padding-right: 1;
    }
    IntegrationSetupModal .field-row {
        layout: vertical;
        height: auto;
        margin-bottom: 1;
    }
    IntegrationSetupModal .field-row > Label {
        height: 1;
        color: $text-muted;
    }
    IntegrationSetupModal .field-row.error > Label {
        color: red;
    }
    /* Input / Select / Switch all ship with a ``border: tall`` default
       so they need ≥3 rows. Clamping to height: 1 hides the border and
       makes the widget look invisible — same bug as Input. ``auto`` lets
       each widget pick its natural height. */
    IntegrationSetupModal .field-row > Input  { height: 3; }
    IntegrationSetupModal .field-row > Select { height: 3; }
    IntegrationSetupModal .field-row > Switch { height: auto; }
    IntegrationSetupModal .field-help {
        color: $text-muted;
        text-style: italic;
        height: auto;
    }
    IntegrationSetupModal .field-error {
        color: red;
        height: auto;
        display: none;
    }
    IntegrationSetupModal .account-block {
        layout: vertical;
        height: auto;
        margin-bottom: 1;
        padding: 1;
        background: $boost;
    }
    IntegrationSetupModal .account-block > Button {
        width: auto;
        margin-top: 1;
    }
    IntegrationSetupModal #status-line {
        height: auto;
        min-height: 1;
        color: $text-muted;
        margin-top: 1;
        padding: 1;
        background: $boost;
    }
    IntegrationSetupModal #status-line.ok    { color: green; }
    IntegrationSetupModal #status-line.warn  { color: yellow; }
    IntegrationSetupModal #status-line.error { color: red; }
    IntegrationSetupModal #button-row {
        layout: horizontal;
        height: auto;
        align-horizontal: left;
        margin-top: 1;
    }
    IntegrationSetupModal #button-row Button { margin-left: 1; }
    IntegrationSetupModal #button-spacer { width: 1fr; }
    IntegrationSetupModal .restart-hint {
        color: yellow;
        text-style: italic;
        margin-top: 1;
        height: auto;
    }
    """

    BINDINGS = [
        ("escape", "close", "Close"),
    ]

    # Tell Textual to auto-focus the first Input on mount. Without this,
    # ModalScreen's default focus heuristic picks Save/Cancel because
    # they're focusable too — and ours runs INSIDE on_mount, so it gets
    # clobbered when the screen's post-mount focus pass runs afterward.
    AUTO_FOCUS = "Input"

    status_text: reactive[str] = reactive("ready", layout=True)
    status_kind: reactive[str] = reactive("")  # "" | "ok" | "warn" | "error"

    def __init__(self, card: IntegrationCard) -> None:
        super().__init__()
        self._card = card
        self._initial: dict[str, Any] = {}
        # Map field.key → widget for reading values back at Save/Test time.
        self._widgets: dict[str, Any] = {}
        self._field_rows: dict[str, Vertical] = {}
        self._field_errors: dict[str, Static] = {}
        self._saved_result: dict[str, Any] | None = None
        # Save-time validation latch: when the user hits Save on a
        # provider card whose credentials look broken, we ask them once
        # to confirm. Second press persists anyway. Two-step is a guard
        # against pasted-wrong-token disasters (e.g. relay token in an
        # OpenRouter slot).
        self._save_force: bool = False

    # ── layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # The form is intentionally empty here — on_mount() hydrates the
        # field values from disk and rebuilds the scroll contents. This
        # avoids a flash of empty inputs and keeps Pydantic/keyring
        # reads off the compose path.
        with Vertical():
            with Vertical(classes="modal-header"):
                yield Label("", id="setup-status-chip", classes="eyebrow")
                yield Label(self._card.label, id="setup-title", classes="title")
                yield Static(self._card.description, classes="description",
                             markup=False)
                if self._card.docs_url:
                    # Textual's Content markup parser rejects URLs inside a
                    # ``[link=...]`` attribute (the ``://`` and ``[`` chars
                    # confuse it). Render compact, plain text — modern
                    # terminals still make the URL copyable/clickable.
                    yield Static(f"Docs: {self._card.docs_url}",
                                 classes="docs", markup=False)
            # ``can_focus=False`` is critical: otherwise the scroll
            # container grabs initial focus before our call_after_refresh
            # hook reaches the first Input, and users land on the
            # scrollbar instead of the text field they meant to edit.
            yield VerticalScroll(id="form-scroll", can_focus=False)
            yield Label(self.status_text, id="status-line")
            with Horizontal(id="button-row"):
                if self._card.fields:
                    yield Button("Disconnect", id="btn-disconnect",
                                 variant="warning")
                yield Static("", id="button-spacer")
                if self._card.probe is not None:
                    yield Button("Test connection", id="btn-test", variant="default")
                # "Set as default" is only meaningful for providers — for
                # channels/tools every connected one runs in parallel.
                # The button is mounted hidden by default and revealed in
                # _sync_header() when the card is usable.
                if self._card.category == "provider":
                    yield Button("Set as default", id="btn-set-active",
                                 variant="success")
                if self._card.fields:
                    yield Button("Save", id="btn-save", variant="primary")
                yield Button("Close  (Esc)", id="btn-cancel", variant="default")

    def _build_field_row(self, f: Field) -> Vertical:
        """Return a single ``Vertical`` row widget for one field.

        Unlike a compose-time generator, this works in any context
        (``on_mount``, button handlers, …) because all children are
        passed to the parent constructor — no ``with`` stack required.
        ``self._widgets[f.key]`` is populated so :meth:`_collect_values`
        can read the value back at Save/Test time.
        """
        cur = self._initial.get(f.key)
        label_text = f.label + (" *" if f.required else "")
        children: list[Any] = [Label(label_text)]

        if f.type == FieldType.BOOL:
            editor: Any = Switch(value=bool(cur), id=f"f-{f.key}")
        elif f.type == FieldType.SELECT:
            opts = [(label, value) for value, label in f.choices]
            editor = Select(
                options=opts,
                value=str(cur) if cur else (f.choices[0][0] if f.choices else Select.BLANK),
                id=f"f-{f.key}",
                allow_blank=False,
            )
        elif f.type == FieldType.MULTI:
            val_str = ", ".join(cur) if isinstance(cur, list) else str(cur or "")
            editor = Input(value=val_str, placeholder=f.placeholder, id=f"f-{f.key}")
        elif f.type == FieldType.INT:
            editor = Input(
                value=str(cur if cur is not None else ""),
                placeholder=f.placeholder, type="integer", id=f"f-{f.key}",
            )
        elif f.type == FieldType.PASSWORD:
            editor = Input(
                value=str(cur or ""),
                placeholder=f.placeholder or "•" * 8,
                password=True, id=f"f-{f.key}",
            )
        else:   # TEXT
            editor = Input(value=str(cur or ""), placeholder=f.placeholder, id=f"f-{f.key}")

        self._widgets[f.key] = editor
        children.append(editor)
        if f.help:
            children.append(Static(f.help, classes="field-help", markup=False))
        err = Static("", classes="field-error", markup=False)
        self._field_errors[f.key] = err
        children.append(err)

        row = Vertical(*children, classes="field-row")
        self._field_rows[f.key] = row
        return row

    # ── lifecycle ─────────────────────────────────────────────────

    async def on_mount(self) -> None:
        # Hydrate current values from config.json. Done in on_mount (not
        # __init__) so we don't block the parent thread on a disk read.
        try:
            self._initial = read_card_values(self._card)
        except Exception:
            self._initial = {}

        configured = self._has_existing_values()
        self._sync_header(configured)

        try:
            scroll = self.query_one("#form-scroll", VerticalScroll)
        except Exception:
            return
        self._widgets.clear()
        self._field_rows.clear()
        self._field_errors.clear()

        if self._card.custom_action == "login":
            # iOS pairing flow — handled exclusively by /login, no form.
            await scroll.mount(Static(
                "[dim]This integration is set up via [b]/login[/b] "
                "(account-based pairing) instead of a config form.[/dim]",
                markup=True,
            ))
        elif self._card.custom_action == "flowly_account":
            # Flowly hosted — show account status block ABOVE the form so
            # users can sign in / sign out without leaving this modal.
            await self._mount_flowly_account_block(scroll)
            if self._card.fields:
                await self._mount_field_form(scroll)
                self._focus_first_input()
        elif not self._card.fields:
            await scroll.mount(Static(
                "[dim]No editable fields for this integration.[/dim]",
                markup=True,
            ))
        else:
            # Await each mount so the Input widgets are fully attached
            # *before* we call .focus(). Without the await, the default
            # focus heuristic picks Save/Cancel because our inputs aren't
            # in the tree yet at the time the heuristic runs.
            await self._mount_field_form(scroll)
            self._focus_first_input()

        # Kick off a fresh probe so the title can flip from "configured"
        # to "● connected" (or to a clear ⚠ if the saved credentials
        # rotted). Done in the background — doesn't block typing.
        if configured and self._card.probe is not None:
            self._auto_probe()

    async def _mount_field_form(self, scroll: VerticalScroll) -> None:
        guide = self._setup_guide_lines()
        if guide:
            await scroll.mount(
                Vertical(
                    Static("Setup checklist", classes="setup-guide-title"),
                    *[
                        Static(line, classes="setup-guide-line", markup=False)
                        for line in guide
                    ],
                    classes="setup-guide",
                )
            )

        current_section = ""
        for f in self._card.fields:
            section = self._field_section(f)
            if section and section != current_section:
                current_section = section
                await scroll.mount(Static(section, classes="section-title", markup=False))
            await scroll.mount(self._build_field_row(f))

    def _setup_guide_lines(self) -> list[str]:
        if self._card.key == "telegram":
            return [
                "1. Create a bot in BotFather and paste the bot token.",
                "2. Choose who can send messages to Flowly.",
                "3. Test the token, then save and restart the gateway.",
            ]
        if self._card.key == "imessage":
            return [
                "1. Grant Flowly Full Disk Access (System Settings → Privacy & Security).",
                "2. Choose who may message Flowly, then Test to verify chat.db access.",
                "3. Save to restart the gateway — replies go out via Messages.app.",
            ]
        if self._card.key == "voice":
            return [
                "1. Add Twilio credentials and the phone number Flowly answers.",
                "2. Set a public webhook URL or ngrok token for inbound calls.",
                "3. Choose STT/TTS providers, then save and restart the gateway.",
            ]
        if self._card.category == "channel":
            return [
                "1. Fill the channel credentials.",
                "2. Test when available.",
                "3. Save to restart the gateway and activate the channel.",
            ]
        if self._card.needs_gateway_restart:
            return [
                "1. Fill the required credentials.",
                "2. Test when available.",
                "3. Save to restart the gateway and activate this tool.",
            ]
        return []

    def _field_section(self, f: Field) -> str:
        if self._card.key == "voice":
            if f.key.startswith("twilio_"):
                return "Twilio account"
            if f.key in {"webhook_base_url", "ngrok_authtoken"}:
                return "Webhook routing"
            if f.key in {"stt_provider", "groq_api_key", "deepgram_api_key"}:
                return "Speech to text"
            if f.key in {"tts_provider", "elevenlabs_api_key"}:
                return "Text to speech"
        if self._card.key == "telegram":
            if f.key in {"token"}:
                return "Bot connection"
            if f.key in {"allow_from", "dm_policy"}:
                return "Access policy"
        if self._card.key == "imessage":
            if f.key in {"allow_from", "dm_policy"}:
                return "Direct messages"
            if f.key in {"group_policy", "group_allow_from"}:
                return "Group chats"
        return ""

    async def _mount_flowly_account_block(self, scroll: VerticalScroll) -> None:
        """Render the signed-in-aware header for the Flowly hosted card.

        Shows either: "✓ Signed in as <email> · [Sign out]" (Vertical row
        of label + button) OR "○ Not signed in · [Sign in to Flowly]".
        After sign-in/out the block re-renders so the user sees the new
        state without dismissing the modal."""
        from flowly.account.auth import load_account_sync
        account = load_account_sync()
        if account is not None:
            email = account.email or account.user_id
            children = [
                Static(f"[green]✓[/] signed in as [b]{email}[/b]",
                       markup=True),
                Static(
                    "[dim]Toggle [b]Active[/b] below off to fall back to your "
                    "BYOK keys (Anthropic / OpenAI / …). Signing out also "
                    "disables iOS pairing.[/dim]",
                    markup=True,
                ),
                Button("Sign out of Flowly", id="btn-flowly-signout",
                       variant="warning"),
            ]
        else:
            children = [
                Static("[yellow]○[/] not signed in to Flowly", markup=True),
                Static(
                    "[dim]Sign in to use Flowly's hosted models without "
                    "pasting an API key. The account also enables iOS "
                    "pairing.[/dim]",
                    markup=True,
                ),
                Button("Sign in to Flowly", id="btn-flowly-signin",
                       variant="primary"),
            ]
        await scroll.mount(Vertical(*children, classes="account-block"))

    def _has_existing_values(self) -> bool:
        """True if any field on disk holds a non-empty value.

        Used to decide between "Connect Telegram" and "Edit Telegram",
        and to hide/show the Disconnect button."""
        for f in self._card.fields:
            v = self._initial.get(f.key)
            if f.type == FieldType.BOOL:
                if bool(v):
                    return True
            elif f.type == FieldType.INT:
                if int(v or 0) != 0:
                    return True
            elif f.type == FieldType.MULTI:
                if v:
                    return True
            elif f.type == FieldType.SELECT:
                # Defaults are non-empty selects; only "set away from default"
                # counts. Cheap approximation: any value at all is a signal.
                if v:
                    return True
            else:
                if str(v or "").strip():
                    return True
        return False

    def _sync_header(self, configured: bool) -> None:
        """Refresh the title + Disconnect button + Set-as-default visibility
        based on whether the card has saved values + the current global
        ``providers.active`` selection. Probe results override the title
        later."""
        title = self.query_one("#setup-title", Label)
        is_default = self._is_current_default()
        if is_default and self._card.category == "provider":
            title.update(
                f"[yellow]★[/yellow] {self._card.label}"
                f"  [dim](default LLM provider)[/dim]"
            )
            self._set_header_status("Default provider", "ok")
        elif configured:
            title.update(f"● {self._card.label}  [dim](configured)[/dim]")
            self._set_header_status("Configured", "ok")
        else:
            title.update(f"Connect {self._card.label}")
            self._set_header_status("Not configured", "warn")
        # Disconnect button only makes sense when there's something to wipe.
        try:
            btn = self.query_one("#btn-disconnect", Button)
            btn.display = configured
        except Exception:
            pass
        # Save button reads "Update" when editing an existing config.
        try:
            save_btn = self.query_one("#btn-save", Button)
            verb = "Update" if configured else "Save"
            if self._card.needs_gateway_restart:
                verb = f"{verb} + restart"
            save_btn.label = verb
        except Exception:
            pass
        # "Set as default": only show on providers, only when usable, and
        # hide entirely when this *is* already the default.
        try:
            set_btn = self.query_one("#btn-set-active", Button)
            if self._card.category != "provider":
                set_btn.display = False
            elif is_default:
                set_btn.display = False   # already the active one
            else:
                set_btn.display = self._is_provider_usable()
        except Exception:
            pass

    def _is_current_default(self) -> bool:
        """Read ``providers.active`` from disk and check against this card."""
        try:
            from flowly.config.loader import load_config
            return (load_config().providers.active or "").strip() == self._card.key
        except Exception:
            return False

    def _is_provider_usable(self) -> bool:
        """Can this provider serve a request right now? Used to gate the
        Set-as-default button so users can't pick an empty-credentials
        provider as their default (which would crash the gateway)."""
        try:
            from flowly.config.loader import load_config
            from flowly.integrations.active_provider import _build_for
            return _build_for(load_config(), self._card.key) is not None
        except Exception:
            return False

    @work
    async def _auto_probe(self) -> None:
        """Run the card's probe once on open to confirm the saved
        credentials still work. Updates the title with the live result so
        the user sees ● green when good or ⚠ yellow on auth_failed."""
        if self._card.probe is None:
            return
        values = self._initial or {}
        result = await run_with_timeout(self._card.probe(values))
        title = self.query_one("#setup-title", Label)
        color = {
            "ok": "green", "auth_failed": "yellow", "down": "red",
            "disabled": "yellow", "not_configured": "dim",
            "unknown": "dim",
        }.get(result.status, "dim")
        detail = f" · {result.detail}" if result.detail else ""
        title.update(
            f"[{color}]{result.badge}[/{color}] {self._card.label}"
            f"  [dim]{result.status}{detail}[/dim]"
        )
        if result.status == "ok":
            self._set_header_status("Connected", "ok")
        elif result.status in {"auth_failed", "down"}:
            self._set_header_status("Needs attention", "error")
        else:
            self._set_header_status(result.status.replace("_", " "), "warn")

    def _focus_first_input(self) -> None:
        for f in self._card.fields:
            if f.type in (FieldType.TEXT, FieldType.PASSWORD, FieldType.MULTI, FieldType.INT):
                w = self._widgets.get(f.key)
                if w is not None:
                    try:
                        w.focus()
                    except Exception:
                        pass
                    return

    # ── value collection ──────────────────────────────────────────

    def _collect_values(self) -> dict[str, Any] | None:
        """Read every field's current widget value. Returns None on validation
        failure (missing required, bad int) after surfacing an error toast."""
        out: dict[str, Any] = {}
        self._clear_field_errors()
        for f in self._card.fields:
            w = self._widgets.get(f.key)
            if w is None:
                continue
            if f.type == FieldType.BOOL:
                out[f.key] = bool(getattr(w, "value", False))
            elif f.type == FieldType.SELECT:
                v = getattr(w, "value", "")
                if v == Select.BLANK:
                    v = ""
                out[f.key] = str(v)
            elif f.type == FieldType.MULTI:
                raw = str(getattr(w, "value", "") or "")
                out[f.key] = [s.strip() for s in raw.split(",") if s.strip()]
            elif f.type == FieldType.INT:
                raw = str(getattr(w, "value", "") or "").strip()
                if raw == "":
                    out[f.key] = 0
                else:
                    try:
                        out[f.key] = int(raw)
                    except ValueError:
                        self._set_field_error(f.key, "Enter a whole number.")
                        self._set_status(f"'{f.label}' must be an integer", "error")
                        return None
            else:
                out[f.key] = str(getattr(w, "value", "") or "")

            if f.required and not out[f.key]:
                self._set_field_error(f.key, "Required field.")
                self._set_status(f"'{f.label}' is required", "error")
                return None
        return out

    # ── actions ───────────────────────────────────────────────────

    def action_close(self) -> None:
        self.dismiss(self._saved_result)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self) -> None:
        self.action_close()

    @on(Button.Pressed, "#btn-test")
    def _test(self) -> None:
        values = self._collect_values()
        if values is None:
            return
        self._set_status("testing…", "")
        self._run_test(values)

    @work
    async def _run_test(self, values: dict[str, Any]) -> None:
        if self._card.probe is None:
            self._set_status("no probe defined", "warn")
            return
        result = await run_with_timeout(self._card.probe(values))
        kind = {
            "ok": "ok", "auth_failed": "error", "down": "error",
            "not_configured": "warn", "disabled": "warn", "unknown": "warn",
        }.get(result.status, "")
        prefix = {
            "ok": "✓ connected", "auth_failed": "✗ auth failed",
            "down": "✗ unreachable", "not_configured": "○ not configured",
            "disabled": "○ disabled", "unknown": "· status unknown",
        }.get(result.status, "·")
        detail = f" — {result.detail}" if result.detail else ""
        self._set_status(f"{prefix}{detail}", kind)

    @on(Button.Pressed, "#btn-save")
    def _save(self) -> None:
        if self._saved_result is not None:
            self.dismiss(self._saved_result)
            return
        values = self._collect_values()
        if values is None:
            return
        self._run_save(values)

    @work
    async def _run_save(self, values: dict[str, Any]) -> None:
        # PROVIDER GUARD: before persisting (possibly broken) credentials
        # and hot-reloading them into the running gateway, force-run the
        # probe. If it fails, show why and require a second press to
        # override. Spares users from typoed / wrong-format keys producing
        # cryptic upstream errors.
        if (
            self._card.category == "provider"
            and self._card.probe is not None
            and not self._save_force
        ):
            self._set_status("validating credentials…", "")
            try:
                result = await run_with_timeout(self._card.probe(values))
            except Exception as exc:
                result = None
                self._set_status(f"probe crashed: {exc}", "error")
            if result is not None and result.status not in ("ok", "unknown"):
                self._save_force = True
                try:
                    btn = self.query_one("#btn-save", Button)
                    btn.label = "Save anyway"
                    btn.variant = "warning"
                except Exception:
                    pass
                pretty = {
                    "auth_failed":    "✗ credentials rejected",
                    "down":           "✗ provider unreachable",
                    "not_configured": "○ required fields missing",
                    "disabled":       "○ provider disabled",
                }.get(result.status, result.status)
                self._set_status(
                    f"{pretty} — {result.detail or 'fix the fields above'} "
                    f"· press Save again to override",
                    "error",
                )
                return
        # Persist atomically.
        try:
            await asyncio.to_thread(apply_card_values, self._card, values)
        except Exception as exc:
            self._set_status(f"save failed: {exc}", "error")
            return
        # Activation strategy:
        # • providers → hot-reload in place (zero downtime)
        # • channels/tools/voice → service restart via launchd/systemd
        # • no-op fields → next request picks them up naturally
        if self._card.category == "provider":
            tail = await self._trigger_provider_reload()
            self._set_status(f"✓ saved · {tail}", "ok")
        elif self._card.needs_gateway_restart:
            self._set_status("✓ saved · restarting gateway…", "")
            tail = await self._auto_restart_service()
            self._set_status(f"✓ saved · {tail}", "ok")
        else:
            self._set_status("✓ saved — takes effect on next request", "ok")
        self._saved_result = {"action": "saved", "key": self._card.key}
        self._after_saved()

    async def _trigger_provider_reload(self) -> str:
        """POST to ``/api/provider/reload`` on the local gateway.

        Returns a short status string for the modal footer. Silent failure
        when the gateway isn't running — saved config will be picked up
        at next boot anyway.
        """
        from flowly.tui.gateway_reload import post_provider_reload
        try:
            r = await post_provider_reload(timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                src = data.get("source") or data.get("key") or "?"
                return f"gateway switched → [b]{src}[/b]"
            if r.status_code == 422:
                err = (r.json() or {}).get("error", "no usable provider")
                return f"[yellow]gateway reload rejected: {err}[/yellow]"
            return f"[yellow]gateway reload HTTP {r.status_code}[/yellow]"
        except Exception as exc:
            return (
                f"[dim]gateway not reloaded ({type(exc).__name__}) — "
                f"restart manually to pick up changes[/dim]"
            )

    async def _auto_restart_service(self) -> str:
        """Bounce the gateway through launchd/systemd. Falls back to a
        clear hint when no service manager owns the gateway."""
        from flowly.integrations.service_control import restart_gateway
        result = await restart_gateway()
        if result.ok:
            return (
                f"gateway restarted via {result.method} "
                f"({result.paused_seconds:.1f}s downtime)"
            )
        if result.method == "no_service":
            return f"[yellow]{result.detail}[/yellow]"
        return f"[red]auto-restart failed: {result.detail}[/red]"

    @on(Button.Pressed, "#btn-set-active")
    @work
    async def _set_as_default(self) -> None:
        """Make this provider the explicit ``providers.active``."""
        if not self._is_provider_usable():
            self._set_status(
                "configure credentials first (Save) — then set as default",
                "warn",
            )
            return
        from flowly.integrations.active_provider import set_active_provider
        try:
            await asyncio.to_thread(set_active_provider, self._card.key)
        except Exception as exc:
            self._set_status(f"failed to set default: {exc}", "error")
            return
        self._set_status(
            f"✓ {self._card.label} is now the default LLM provider "
            f"[dim](takes effect on next request)[/dim]",
            "ok",
        )
        # Re-sync header so the ★ badge appears immediately.
        self._sync_header(self._has_existing_values())

    @on(Button.Pressed, "#btn-flowly-signin")
    @work
    async def _flowly_signin(self) -> None:
        """Open the device-code login flow inline. After success the
        account header re-renders so the user sees signed-in state
        without dismissing this modal. If no provider was previously
        the default, we set Flowly as the default so the user gets LLM
        access without an extra step."""
        from flowly.tui.panes.login_modal import LoginModal
        result = await self.app.push_screen_wait(LoginModal())
        if result is None:
            self._set_status("login cancelled", "warn")
            return
        # Auto-promote Flowly to default if nothing was set. Avoids the
        # "I logged in but nothing happened" trap.
        from flowly.config.loader import load_config
        from flowly.integrations.active_provider import set_active_provider
        try:
            current = (load_config().providers.active or "").strip()
            if not current:
                await asyncio.to_thread(set_active_provider, "flowly")
        except Exception:
            pass
        self.dismiss({"action": "saved", "key": self._card.key})

    @on(Button.Pressed, "#btn-flowly-signout")
    @work
    async def _flowly_signout(self) -> None:
        from flowly.account.auth import clear_account
        from flowly.account.relay_config import clear_relay_credentials
        from flowly.integrations.active_provider import clear_active_if_matches
        try:
            await asyncio.to_thread(clear_account)
            await asyncio.to_thread(clear_relay_credentials)
            # Don't leave a dangling default pointing at an account that
            # no longer exists — the gateway would refuse to boot.
            await asyncio.to_thread(clear_active_if_matches, "flowly")
        except Exception as exc:
            self._set_status(f"sign-out failed: {exc}", "error")
            return
        self._set_status(
            "✓ signed out of Flowly · iOS pairing disabled · default cleared "
            "(restart gateway to apply)",
            "ok",
        )
        await asyncio.sleep(0.8)
        self.dismiss({"action": "saved", "key": self._card.key})

    @on(Button.Pressed, "#btn-disconnect")
    def _disconnect(self) -> None:
        # Two-step confirmation: first press flips the button label to a
        # warning state; second press wipes credentials. Less surprising
        # than an immediate destructive action behind a single click.
        try:
            btn = self.query_one("#btn-disconnect", Button)
        except Exception:
            return
        if getattr(self, "_disconnect_armed", False):
            self._disconnect_armed = False
            btn.label = "Disconnect"
            self._run_disconnect()
        else:
            self._disconnect_armed = True
            btn.label = "Confirm wipe?"
            self._set_status(
                "press Disconnect again to wipe credentials for this integration",
                "warn",
            )

    @work
    async def _run_disconnect(self) -> None:
        from flowly.integrations.active_provider import clear_active_if_matches
        from flowly.integrations.config_io import clear_card
        try:
            await asyncio.to_thread(clear_card, self._card)
            # Disconnecting a provider that's currently the default would
            # leave the gateway pointing at empty credentials. Clear the
            # default pointer so we fall back to the cascade automatically.
            cleared_default = await asyncio.to_thread(
                clear_active_if_matches, self._card.key,
            )
        except Exception as exc:
            self._set_status(f"disconnect failed: {exc}", "error")
            return
        tail = " · default cleared" if cleared_default else ""
        self._set_status(
            f"✓ {self._card.label} disconnected{tail} — restart gateway to apply",
            "ok",
        )
        self._saved_result = {"action": "saved", "key": self._card.key}
        self._after_saved(disconnected=True)

    # ── helpers ───────────────────────────────────────────────────

    def _after_saved(self, disconnected: bool = False) -> None:
        self._set_header_status("Disconnected" if disconnected else "Saved", "ok")
        try:
            save_btn = self.query_one("#btn-save", Button)
            save_btn.label = "Done"
            save_btn.variant = "primary"
        except Exception:
            pass
        try:
            close_btn = self.query_one("#btn-cancel", Button)
            close_btn.label = "Done  (Esc)"
        except Exception:
            pass
        try:
            test_btn = self.query_one("#btn-test", Button)
            test_btn.display = False
        except Exception:
            pass
        try:
            disconnect_btn = self.query_one("#btn-disconnect", Button)
            disconnect_btn.display = False
        except Exception:
            pass

    def _set_header_status(self, text: str, kind: str = "") -> None:
        color = {
            "ok": "green",
            "warn": "yellow",
            "error": "red",
        }.get(kind, "dim")
        restart = ""
        if self._card.needs_gateway_restart and self._saved_result is None:
            restart = " · restart required on save"
        try:
            chip = self.query_one("#setup-status-chip", Label)
            chip.update(
                f"[{color}]{text}[/]"
                f"[dim]{restart} · {self._card.category}[/dim]"
            )
        except Exception:
            pass

    def _clear_field_errors(self) -> None:
        for row in self._field_rows.values():
            try:
                row.remove_class("error")
            except Exception:
                pass
        for err in self._field_errors.values():
            try:
                err.update("")
                err.display = False
            except Exception:
                pass

    def _set_field_error(self, key: str, message: str) -> None:
        try:
            row = self._field_rows[key]
            row.add_class("error")
        except Exception:
            pass
        try:
            err = self._field_errors[key]
            err.update(message)
            err.display = True
        except Exception:
            pass

    def _set_status(self, msg: str, kind: str = "") -> None:
        self.status_text = msg
        self.status_kind = kind
        try:
            line = self.query_one("#status-line", Label)
            line.update(msg)
            line.set_classes("")
            if kind:
                line.add_class(kind)
        except Exception:
            pass

    # ESC handling — bind Esc to the cancel handler even when focus is in
    # an Input (Inputs swallow Esc by default).
    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.action_close()
