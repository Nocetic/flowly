"""Gateway-independent setup surface.

``flowly setup`` has to work *before* a gateway or a provider exists —
that's its whole job. The full :class:`~flowly.tui.app.FlowlyTUI` can't
serve that: it opens a WebSocket to the local gateway in ``on_mount`` and
bails out early when the socket isn't there, so the provider picker never
gets a chance to mount. And the gateway itself refuses to start until a
provider is configured. Routing setup through the TUI therefore wedges a
brand-new user in a loop with no exit.

This module breaks the loop by hosting the existing setup screens
(:class:`ProviderPicker`, :class:`IntegrationsModal`,
:class:`IntegrationSetupModal`, :class:`LoginModal`) inside a minimal
Textual app that never touches the gateway. Those screens already write
config straight to disk (``config_io`` / ``set_active_provider`` /
``save_account``); the only gateway interaction they have is an *optional*
post-save hot-reload that already degrades to "restart to apply" when the
gateway is offline. So nothing here needs a running gateway — the runtime
is a separate, later step the CLI nudges toward once setup is done.
"""

from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Center, Middle
from textual.widgets import Static

# Recognised setup targets → mirrors the deep-links `flowly setup …`
# subcommands used to pass through ``run_tui(open_modal=…)``.
_PROVIDER_TARGETS = {"provider", "providers", "model"}
_CHANNEL_TARGETS = {"channels", "channel"}
_TOOL_TARGETS = {"integrations", "tools"}


class SetupApp(App[None]):
    """Tiny host app whose only job is to surface one setup screen.

    No transcript, no composer, no gateway client. It pushes the
    requested screen on mount, lets the user configure things (saved to
    disk by the screen itself), and exits when they're done. The base
    screen is just a backdrop the modal sits on top of.
    """

    # Standard Textual theme variables only ($surface/$accent/$text/…),
    # so the bundled screens render correctly under the default theme
    # without the main TUI's custom palette.
    CSS = """
    Screen {
        align: center middle;
    }
    #setup-backdrop {
        width: auto;
        height: auto;
        color: $text-muted;
        text-align: center;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("escape", "quit", "Quit"),
    ]

    def __init__(self, target: str = "provider") -> None:
        super().__init__()
        self._target = (target or "provider").strip().lower()

    def compose(self) -> ComposeResult:
        with Middle():
            with Center():
                yield Static(
                    "flowly setup\n\n[dim]loading…  ·  press q to quit[/dim]",
                    id="setup-backdrop",
                )

    def on_mount(self) -> None:
        self.title = "flowly setup"
        self._run_flow()

    @work
    async def _run_flow(self) -> None:
        """Drive the requested setup screen, then quit.

        ``push_screen_wait`` must run inside a worker — hence ``@work``.
        Any unexpected error is surfaced via ``notify`` rather than
        crashing the app out from under the user.
        """
        try:
            if self._target in _PROVIDER_TARGETS:
                await self._provider_flow()
            elif self._target in _CHANNEL_TARGETS:
                await self._catalog_flow(
                    categories=("channel",),
                    title="Channels",
                    item_label="channel",
                )
            else:  # default + tools/integrations
                await self._catalog_flow(
                    categories=("tool", "web_search", "media", "voice"),
                    title="Integrations",
                    item_label="integration",
                )
        except Exception as exc:  # noqa: BLE001 — never wedge the screen
            self.notify(f"setup error: {exc}", severity="error", timeout=8)
        finally:
            self.exit()

    # ── provider picker ──────────────────────────────────────────────

    async def _provider_flow(self) -> None:
        """Pick / configure the LLM provider — the one mandatory step.

        Mirrors :meth:`FlowlyTUI.action_provider`'s no-arg branch, minus
        the gateway hot-reload (the picker handles that itself, gracefully
        skipping it when the gateway is offline).
        """
        from flowly.integrations import get_card
        from flowly.tui.panes.integration_setup_modal import IntegrationSetupModal
        from flowly.tui.panes.provider_picker import ProviderPicker

        result = await self.push_screen_wait(ProviderPicker())
        if not result:
            return

        action = result.get("action")
        if action == "switched":
            self.notify(
                f"default provider → {result.get('key')}", title="saved"
            )
        elif action in {"opened_setup", "inline_setup"}:
            card = get_card(result.get("key") or "")
            if card is None:
                return
            if card.custom_action == "xai_login":
                # Browser-OAuth providers need the dedicated login flow,
                # which lives on the full app. Point at the CLI path so a
                # fresh user isn't stranded here.
                self.notify(
                    "Run `flowly xai login` to connect your xAI Grok "
                    "subscription.",
                    severity="warning",
                    timeout=8,
                )
                return
            saved = await self.push_screen_wait(IntegrationSetupModal(card))
            if saved and saved.get("action") == "saved":
                self.notify(f"{card.label} saved", title="saved")
        elif action == "needs_login":
            self.notify(
                "Run `flowly xai login` to connect your xAI Grok "
                "subscription.",
                severity="warning",
                timeout=8,
            )
        elif action == "login":
            # Flowly account picked while signed out → browser sign-in via the
            # LoginModal (auto-provisions the account key + relay opt-in), NOT a
            # paste-your-key form.
            from flowly.account.auth import load_account_sync
            from flowly.tui.panes.login_modal import LoginModal

            if load_account_sync():
                self.notify("already signed in to Flowly", title="account")
            else:
                account = await self.push_screen_wait(LoginModal())
                if account:
                    self.notify("signed in to Flowly", title="account")

    # ── channels / tools catalog ─────────────────────────────────────

    async def _catalog_flow(
        self,
        *,
        categories: tuple[str, ...],
        title: str,
        item_label: str,
    ) -> None:
        """Open a filtered catalog, then the chosen card's setup form.

        Mirrors :meth:`FlowlyTUI._open_card_catalog` so channel/tool setup
        behaves identically to the in-TUI path — just without a gateway.
        Loops back to the catalog after each card so the user can wire up
        several in one sitting.
        """
        from flowly.integrations import get_card
        from flowly.tui.panes.integration_setup_modal import IntegrationSetupModal
        from flowly.tui.panes.integrations_modal import IntegrationsModal
        from flowly.tui.panes.login_modal import LoginModal

        while True:
            result = await self.push_screen_wait(
                IntegrationsModal(
                    categories=categories,
                    title=title,
                    item_label=item_label,
                )
            )
            if not result or result.get("action") != "opened":
                return
            key = str(result.get("key") or "")
            card = get_card(key)
            if card is None:
                return

            # Flowly-account cards open the sign-in flow, not a form —
            # LoginModal persists the account to disk itself.
            if card.custom_action == "login":
                from flowly.account.auth import load_account_sync

                existing = load_account_sync()
                if existing:
                    self.notify(
                        f"already signed in as "
                        f"{existing.email or existing.user_id}",
                        title="account",
                    )
                    continue
                account = await self.push_screen_wait(LoginModal())
                if account:
                    self.notify("signed in to Flowly", title="account")
                continue

            saved = await self.push_screen_wait(IntegrationSetupModal(card))
            if saved and saved.get("action") == "saved":
                tail = (
                    " · restart gateway to activate"
                    if card.needs_gateway_restart
                    else ""
                )
                self.notify(f"{card.label} saved{tail}", title="saved")


def run_setup(target: str = "provider") -> None:
    """Launch the gateway-free setup surface for ``target``.

    ``target`` is one of ``provider`` (default), ``channels``, or
    ``tools``/``integrations``. Returns once the user dismisses the
    screen; the caller (the CLI) is responsible for printing runtime
    next-steps afterwards.
    """
    SetupApp(target=target).run()
