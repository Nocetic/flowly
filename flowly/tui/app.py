"""Flowly TUI — Textual app shell."""

from __future__ import annotations

import asyncio
import atexit
import time
from typing import Any

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding

from flowly.integrations import Field, FieldType, IntegrationCard
from flowly.tui.artifact_open import (
    is_external_artifact_type,
    open_artifact_external,
)
from flowly.tui.attachments import (
    is_video_path,
    render_message_with_attachments,
)
from flowly.tui.client import (
    ApprovalRequest,
    ArtifactEvent,
    ChatAborted,
    ChatError,
    ChatFinal,
    CompactionEvent,
    ConnectionLost,
    GatewayClient,
    GatewayUnavailable,
    Reconnected,
    Reconnecting,
    StreamDelta,
    SubagentCompleted,
    SubagentStarted,
    ToolComplete,
    ToolStart,
)
from flowly.tui.first_touch import (
    HINT_FIRST_APPROVAL,
    HINT_FIRST_CLEAR,
    HINT_FIRST_TOOL,
    HINT_FIRST_TURN,
)
from flowly.tui.first_touch import (
    get_text as _hint_text,
)
from flowly.tui.first_touch import (
    is_seen as _hint_is_seen,
)
from flowly.tui.first_touch import (
    mark_seen as _hint_mark_seen,
)
from flowly.tui.media_upload import AttachmentPreparationError, prepare_media_attachments
from flowly.tui.panes.activity_modal import ActivityModal
from flowly.tui.panes.approvals_modal import ApprovalsModal
from flowly.tui.panes.artifacts_modal import ArtifactsModal
from flowly.tui.panes.assistant_picker import AssistantPicker
from flowly.tui.panes.composer import (
    ApprovalPrompt,
    ApprovalPromptRequest,
    Composer,
    InlineSecretPrompt,
    InlineSecretPromptRequest,
    InlineSetupField,
    InlineSetupPrompt,
    InlineSetupPromptRequest,
)
from flowly.tui.panes.confirm_modal import ConfirmModal
from flowly.tui.panes.help_hint import HelpHint
from flowly.tui.panes.help_modal import HelpModal
from flowly.tui.panes.integrations_modal import IntegrationsPanel
from flowly.tui.panes.login_modal import LoginModal
from flowly.tui.panes.memory_review import MemoryReviewPanel
from flowly.tui.panes.model_picker import ModelPickerPanel
from flowly.tui.panes.policy_modal import PolicyModal
from flowly.tui.panes.provider_picker import ProviderPickerPanel
from flowly.tui.panes.session_picker import SessionPicker
from flowly.tui.panes.status import ContextHeader, StatusBar
from flowly.tui.panes.status_panel import SessionStatusPanel
from flowly.tui.panes.subagents import SubagentPane
from flowly.tui.panes.transcript import Bubble, TranscriptPane
from flowly.tui.panes.usage_panel import UsagePanel
from flowly.tui.panes.welcome import build_welcome
from flowly.tui.state import fresh_session_key, load_state, save_state
from flowly.tui.theme import (
    css_for,
    get_palette,
    get_theme,
    list_themes,
    resolve_theme_name,
    set_active_theme,
)

HISTORY_PRELOAD_LIMIT = 20

# Inline board view (/board) — status icons + labels. Monochrome glyphs so
# they sit well in any theme. Cancelled cards aren't shown (not a column).
_BOARD_ICONS = {"todo": "○", "in_progress": "◐", "waiting": "⏸", "done": "✓"}
_BOARD_LABELS = {
    "todo": "To do",
    "in_progress": "In progress",
    "waiting": "Waiting",
    "done": "Done",
}


_BOARD_HELP = (
    "/board — task board\n"
    "  /board                 show the board\n"
    "  /board add <title>     add a card\n"
    "  /board run <id>        run a card (agent works it)\n"
    "  /board done <id>       move a card to Done\n"
    "  /board cancel <id>     cancel a running card\n"
    "  /board del <id>        delete a card\n"
    "  /board clear           remove all Done cards\n"
    "  (id can be a prefix, e.g. c_a1 — must be unique)"
)


# Shown by /computer in the terminal. Computer use (desktop control) needs the
# macOS Accessibility + Screen Recording permissions that only the Flowly Desktop
# app can hold and delegate to the agent — the standalone CLI can't acquire them.
# Plain text on purpose: system bubbles render with markup disabled.
_COMPUTER_INFO = (
    "🖥  Computer use — controlling your desktop (mouse, keyboard, screen, and\n"
    "app/UI automation) — runs only in the Flowly Desktop app.\n"
    "\n"
    "The desktop app holds the macOS Accessibility + Screen Recording permissions\n"
    "this terminal can't acquire on its own, and drives the computer tool through\n"
    "its bundled helper.\n"
    "\n"
    "Get it at https://useflowlyapp.com — once installed, computer use works\n"
    "automatically. A terminal-native version is planned."
)


def _format_board(snap: dict) -> str:
    """Render a board snapshot as a compact markdown list, grouped by status."""
    total = int(snap.get("total", 0) or 0)
    if not total:
        return "**📋 Board** — no cards yet. Add one with `board_add` or from the desktop."

    lines = [f"**📋 Board** · {total} card{'s' if total != 1 else ''}"]
    for col in snap.get("columns", []):
        status = col.get("status", "")
        cards = col.get("cards", []) or []
        icon = _BOARD_ICONS.get(status, "•")
        label = _BOARD_LABELS.get(status, status)
        lines.append("")
        lines.append(f"**{icon} {label}** ({len(cards)})")
        if not cards:
            lines.append("_(empty)_")
            continue
        for c in cards:
            cid = c.get("id", "")
            title = c.get("title", "")
            origin = (c.get("originChannel") or "").strip()
            tag = (
                f" · _{origin}_"
                if origin and origin not in ("cli", "desktop", "direct")
                else ""
            )
            lines.append(f"- `{cid}` {title}{tag}")
    return "\n".join(lines)


def _inline_provider_key_field(card: IntegrationCard) -> Field | None:
    """Return the primary API-key field when a provider can use inline setup."""
    if card.category != "provider" or card.custom_action:
        return None
    for field in card.fields:
        if field.key == "api_key" and field.type == FieldType.PASSWORD:
            return field
    return None


def _inline_setup_field(field: Field, value: object) -> InlineSetupField:
    kind = {
        FieldType.TEXT: "text",
        FieldType.PASSWORD: "password",
        FieldType.INT: "int",
        FieldType.BOOL: "bool",
        FieldType.SELECT: "select",
        FieldType.MULTI: "multi",
    }[field.type]
    return InlineSetupField(
        key=field.key,
        label=field.label,
        kind=kind,
        placeholder=field.placeholder,
        help=field.help,
        required=field.required,
        value=value,
        choices=list(field.choices),
    )


# Quick permission-level cycle (F5). One keystroke sets BOTH the exec tool
# policy and the codex_session runtime policy to a coherent level, live over
# RPC — a fast way to exercise exec.policy.set / codex.policy.set without a
# settings screen. Each entry: (key, label, (exec_security, exec_ask),
# (codex_approval, codex_sandbox)).
# codex approval values are the FLOWLY policy names (on-request / never /
# auto-review / granular) — codex.policy.set maps them to codex's own
# ask_for_approval vocabulary. auto-review → codex "untrusted" (prompt for
# everything but safe reads); never → run unattended.
_PERMISSION_LEVELS: tuple[tuple[str, str, tuple[str, str], tuple[str, str]], ...] = (
    ("ask",  "🔒 Ask",  ("full", "always"),       ("auto-review", "workspace-write")),
    ("auto", "⚖️ Auto", ("allowlist", "on-miss"), ("on-request",  "workspace-write")),
    ("yolo", "🚀 YOLO", ("full", "off"),          ("never",       "full-access")),
)


def _match_permission_level(policy: dict) -> int:
    """Index of the level whose exec (security, ask) matches ``policy``, else -1
    (so the first cycle lands on the first level)."""
    sec, ask = policy.get("security"), policy.get("ask")
    for i, (_key, _label, (s, a), _codex) in enumerate(_PERMISSION_LEVELS):
        if s == sec and a == ask:
            return i
    return -1


class FlowlyTUI(App[None]):
    CSS = css_for()

    BINDINGS = [
        Binding("ctrl+c", "abort_or_quit", "Abort/Quit", priority=True),
        Binding("ctrl+l", "clear_session", "Clear", priority=True),
        Binding("ctrl+d", "quit", "Quit", priority=True),
        Binding("ctrl+s", "open_sessions", "Sessions", priority=True),
        Binding("ctrl+m", "open_assistants", "Assistants", priority=True),
        Binding("ctrl+a", "toggle_subagents", "Subagents", priority=True),
        Binding("f1", "open_help", "Help", priority=True),
        Binding("f2", "open_activity", "Activity", priority=True),
        Binding("f3", "open_approvals", "Approvals", priority=True),
        Binding("f4", "open_artifacts", "Artifacts", priority=True),
        # Shift+Tab cycles the permission level. App-level (not composer on_key)
        # so Textual awaits the async action; priority so it fires over the
        # focused composer. Plain Tab is left alone — the composer binds it to
        # apply slash/path autocomplete.
        Binding("shift+tab", "cycle_permission", "Permission", priority=True),
        Binding("ctrl+y", "copy_last", "Copy", priority=True),
    ]

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 18790,
        session_key: str = "tui:default",
        model_hint: str = "",
        client: "GatewayClient | None" = None,
        auto_open_modal: str | None = None,
        theme_name: str | None = None,
    ) -> None:
        state = load_state()
        self._theme_name = resolve_theme_name(theme_name, state)
        self._palette = set_active_theme(self._theme_name)
        super().__init__()
        self.stylesheet.add_source(
            css_for(self._palette),
            read_from=("flowly-tui", "runtime-theme"),
            tie_breaker=100,
        )
        # TUI always uses the local gateway. Tests/callers can inject a
        # custom client; otherwise we build one from host/port.
        self._client = client if client is not None else GatewayClient(host=host, port=port)
        self._session_key = session_key
        self._model_hint = model_hint
        self._restored_draft: str = str(state.get("last_draft") or "")
        # Optional modal to auto-open after launch (e.g. `flowly setup`
        # passes "integrations" so the catalogue surfaces immediately).
        self._auto_open_modal: str | None = auto_open_modal
        # Set once the user actually sends a message here. Gates the
        # ``last_session_key`` write so ``--resume`` reopens the last chat
        # that was *used*, not an empty session from an idle launch+quit.
        self._session_used = False
        self._current_run: str | None = None
        self._current_bubble: Bubble | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._skill_commands: dict[str, str] = {}
        self._skill_notice_by_run: dict[str, str] = {}
        self._approval_queue: list[ApprovalRequest] = []
        self._approval_active = False
        self._approval_choice_future: asyncio.Future[str] | None = None
        self._inline_secret_future: asyncio.Future[str | None] | None = None
        self._inline_setup_future: asyncio.Future[dict[str, object] | None] | None = None
        self._composer_picker_future: asyncio.Future[Any] | None = None
        # Inline memory-review queue (the on-open "review new memories" panel).
        self._memory_review_items: list[dict] = []
        self._memory_review_idx = 0
        # Esc dismisses the auto-popup for the rest of this TUI session (resets on
        # relaunch). `/memory` still opens it explicitly.
        self._memory_review_dismissed = False
        # Account (lazy: loaded in on_mount)
        self._account = None
        self._account_ref: list = [None]
        self._account_refresh_task: asyncio.Task[None] | None = None
        # Cumulative session usage (billed view) — summed across turns for
        # /usage and the live cost badge. Distinct from the status bar's
        # context-window numbers, which show only the LATEST turn's occupancy.
        # Reset alongside context on /clear and /new (see _reset_context_usage).
        self._usage_totals: dict[str, float] = {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "turns": 0, "cost_usd": 0.0, "cost_known": 0,
        }
        self._session_started = time.monotonic()
        # Crash-safe: write state on interpreter exit even if Textual's
        # on_unmount didn't run (segfault, SIGKILL is unrecoverable; SIGTERM,
        # uncaught exception, MemoryError all hit atexit).
        atexit.register(self._persist_state)

    def compose(self) -> ComposeResult:
        yield ContextHeader(show_clock=False, id="context-header")
        yield SubagentPane(id="subagents")
        yield TranscriptPane(id="transcript")
        yield HelpHint(id="help-hint")
        yield Composer(id="composer")

    async def on_mount(self) -> None:
        self.title = "flowly"
        self.sub_title = self._session_key
        status = self.query_one(StatusBar)
        status.session = self._session_key
        self._refresh_active_provider_status()
        if self._model_hint:
            status.model = self._model_hint

        try:
            await self._client.connect()
        except GatewayUnavailable as exc:
            self._set_state("offline")
            self.query_one(TranscriptPane).add_error(
                f"Gateway unreachable: {exc}\n"
                f"Start it in another terminal: `flowly gateway`"
            )
            self.query_one(Composer).set_enabled(False)
            return

        self._event_task = asyncio.create_task(self._consume_events())
        composer = self.query_one(Composer)
        # Restore an unsent draft from last session, if any.
        if self._restored_draft.strip():
            try:
                ed = composer.query_one("#composer-input")
                ed.text = self._restored_draft  # type: ignore[attr-defined]
                self.query_one(TranscriptPane).add_system(
                    "restored unsent draft from last session"
                )
            except Exception:
                pass
        composer.focus_input()

        # Fire background bootstrap tasks in parallel.
        asyncio.create_task(self._refresh_command_palette())
        asyncio.create_task(self._preload_history())
        asyncio.create_task(self._refresh_session_artifacts())
        asyncio.create_task(self._check_gateway_capabilities())
        asyncio.create_task(self._load_account_on_mount())
        # Seed the permission badge from the live exec policy, then keep it in
        # sync on a slow poll so a mode changed elsewhere (Desktop, another
        # client) shows up here without a restart.
        asyncio.create_task(self._sync_permission_badge())
        self.set_interval(8.0, self._sync_permission_badge)
        # Prefetch the OpenRouter catalog in the background so the
        # context-window bar can size itself from the model's real
        # context_length (no hardcoded family tables). Also seeds the
        # /model picker so it opens instantly.
        asyncio.create_task(self._warm_model_catalogs())
        # Periodic badge poll (5s) keeps approvals/artifacts counts fresh.
        self.set_interval(5.0, self._poll_badges)
        # Run once immediately so first launch shows current state.
        asyncio.create_task(self._poll_badges())
        # Memory monitor (30s) — warns at 500MB, errors at 1GB.
        self._mem_warned = False
        self._mem_errored = False
        self.set_interval(30.0, self._check_memory)

        # Caller (e.g. `flowly setup`) can request a modal to surface
        # right after launch. Defer via call_later so the modal mounts
        # on top of an already-painted base UI rather than racing the
        # initial compose pass.
        if self._auto_open_modal:
            self.call_later(self._maybe_open_initial_modal)
        else:
            # Surface the bot's memory review queue inline on open (the
            # "review new memories" panel), if it has anything pending.
            asyncio.create_task(self._maybe_show_memory_review())

    def on_paste(self, event: events.Paste) -> None:
        try:
            composer = self.query_one(Composer)
        except Exception:
            return
        if event.text.strip():
            if composer.attach_pasted_image_path(event.text):
                event.stop()
                event.prevent_default()
            return
        if composer.attach_clipboard_image(notify=False):
            event.stop()
            event.prevent_default()

    def _set_state(self, state: str) -> None:
        """Propagate connection/activity state to status bar + composer hint.

        Called from every state-transition site so the composer's hint
        line stays in sync with the status bar's spinner. Keeps the
        existing ``status.state = "…"`` pattern compatible — call sites
        can still set the reactive directly and the hint will catch up
        on the next transition through here, but using this helper keeps
        the two surfaces atomic.
        """
        try:
            self.query_one(StatusBar).state = state
        except Exception:
            pass
        try:
            self.query_one(Composer).set_state(state)
        except Exception:
            pass

    def _fire_hint(self, hint_id: str) -> None:
        """Show a first-touch hint in the transcript at most once per profile.

        Safe to call from any code path — the seen-marker check is
        cheap and persistence failures degrade silently. Use stable
        ids from ``flowly.tui.first_touch`` so a hint added today
        doesn't replay every launch for users who saw it last week.
        """
        try:
            if _hint_is_seen(hint_id):
                return
            text = _hint_text(hint_id)
            if not text:
                return
            self.query_one(TranscriptPane).add_system(text)
            _hint_mark_seen(hint_id)
        except Exception:
            # First-touch hints are pure UX polish — never crash the
            # app over a transcript / state-file hiccup.
            pass

    def _maybe_open_initial_modal(self) -> None:
        """Push the modal requested via ``--open`` (CLI entry indirection).

        Recognised targets mirror what ``flowly setup …`` subcommands
        deep-link to. ``provider`` is the default for bare
        ``flowly setup`` since picking a model is the only mandatory
        step before chatting.
        """
        target = (self._auto_open_modal or "").lower()
        self._auto_open_modal = None  # one-shot
        if target in ("provider", "providers", "model"):
            # action_provider is @work — bare call, no await.
            self.action_provider("")
        elif target == "channels":
            self.action_open_channels()
        elif target in ("integrations", "tools"):
            self.action_open_integrations()

    async def _warm_model_catalogs(self) -> None:
        """Prefetch model lists for any provider with a working fetcher.

        Today that's just OpenRouter (its catalog is also what the Flowly
        proxy exposes), so a single call covers ~262 models. The status
        bar's ``_model_budget`` picks up the cache on the next render."""
        try:
            from flowly.integrations.model_catalog import warm_cache
            await warm_cache("openrouter")
            # Cache just landed — directly refresh the _TokenBar so it
            # re-renders with the live ``context_length`` instead of the
            # 200k default it baked in while the cache was empty. We
            # bypass the reactive system because Textual deduplicates
            # same-value assignments, and the model field hasn't actually
            # changed — only the catalog backing the budget lookup has.
            try:
                header = self.query_one(ContextHeader)
                tokens_bar = getattr(header, "_tokens", None)
                if tokens_bar is not None:
                    tokens_bar._refresh()
            except Exception:
                pass
        except Exception:
            pass

    async def _load_account_on_mount(self) -> None:
        """Hydrate saved Flowly account on launch (silent if not signed in)."""
        from flowly.account.auth import (
            background_refresh_loop,
            load_account_refreshing,
        )
        account = await load_account_refreshing()
        if account is None:
            return
        self._account = account
        self._account_ref = [account]
        self._account_refresh_task = asyncio.create_task(
            background_refresh_loop(self._account_ref)
        )
        self.query_one(TranscriptPane).add_system(
            f"✓ Flowly account: [b]{account.email or account.user_id}[/b]"
        )

    def _check_memory(self) -> None:
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # On macOS ru_maxrss is bytes; on Linux it's KB. Normalize to MB.
            import platform
            mb = usage / (1024 * 1024) if platform.system() == "Darwin" else usage / 1024
        except Exception:
            return
        transcript = self.query_one(TranscriptPane)
        if mb >= 1024 and not self._mem_errored:
            self._mem_errored = True
            transcript.add_error(
                f"⚠ TUI memory usage critical: {mb:.0f} MB. Consider /clear or restart."
            )
        elif mb >= 500 and not self._mem_warned:
            self._mem_warned = True
            transcript.add_system(
                f"ℹ memory usage {mb:.0f} MB — heads up, /compact can help"
            )

    async def _poll_badges(self) -> None:
        session_key = self._session_key
        pending, arts, session_arts = await asyncio.gather(
            self._client.approval_list(),
            self._client.artifacts_list(limit=200, include_content=False),
            self._client.artifacts_list(
                limit=200,
                session_key=session_key,
                include_content=False,
            ),
            return_exceptions=True,
        )
        status = self.query_one(StatusBar)
        if not isinstance(pending, BaseException):
            status.approvals_pending = len(pending)
        if not isinstance(arts, BaseException):
            status.artifacts_count = len(arts)
        if session_key == self._session_key and not isinstance(session_arts, BaseException):
            self.query_one(Composer).set_artifacts(session_arts)

    async def _refresh_session_artifacts(self) -> None:
        session_key = self._session_key
        try:
            artifacts = await self._client.artifacts_list(
                limit=200,
                session_key=session_key,
                include_content=False,
            )
        except Exception:
            return
        if session_key == self._session_key:
            self.query_one(Composer).set_artifacts(artifacts)

    async def _check_gateway_capabilities(self) -> None:
        """Probe /health for capabilities; warn if running gateway is stale."""
        import aiohttp
        url = f"http://{self._client._url.split('://', 1)[1].split('/')[0]}/health"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                    data = await r.json()
        except Exception:
            return
        caps = set(data.get("capabilities") or [])
        if "tool_events" not in caps:
            self.query_one(TranscriptPane).add_error(
                "⚠ Tool events not supported by the running gateway.\n"
                "Live tool trails (spinner + ✓/✗) will NOT appear.\n\n"
                "Restart the gateway on a current build, then reopen this TUI:\n"
                "  1. Kill the running gateway (Ctrl+C in its terminal, "
                "or `kill <pid>` from `ps aux | grep flowly`)\n"
                "  2. Start it again: `flowly gateway`\n"
                "  3. Reopen this TUI"
            )

    async def on_unmount(self) -> None:
        self._persist_state()
        if self._event_task:
            self._event_task.cancel()
        await self._client.close()

    def _persist_state(self) -> None:
        """Persist session + current draft. Called on unmount, on
        session switch, and as an atexit fallback so a hard crash still
        leaves the user where they were.
        """
        try:
            state = load_state()
            if self._session_used:
                state["last_session_key"] = self._session_key
            try:
                draft = self.query_one("#composer-input").text  # type: ignore[attr-defined]
            except Exception:
                draft = ""
            state["last_draft"] = draft
            save_state(state)
        except Exception:
            pass

    def _active_provider_display(self) -> tuple[str, str]:
        """Return (label, source) for the provider serving the next request."""
        try:
            from flowly.config.loader import load_config
            from flowly.integrations import get_card
            from flowly.integrations.active_provider import resolve_active_provider
            active = resolve_active_provider(load_config())
            if active is None:
                return ("none", "configure one with /provider")
            card = get_card(active.key)
            label = card.label if card is not None else active.key
            return (label, active.source)
        except Exception:
            return ("unknown", "")

    def _refresh_active_provider_status(self) -> None:
        provider, _source = self._active_provider_display()
        try:
            self.query_one(StatusBar).provider = provider
        except Exception:
            pass

    async def _resync_config_after_reconnect(self) -> None:
        """Re-read model + provider from disk and push into the StatusBar.

        Called from the Reconnected handler so that a config change that
        happened while we were disconnected — the most common case being
        Desktop's Save flow restarting the gateway — surfaces in the UI
        immediately instead of waiting for the user to restart the TUI.
        Safe to run as a background task: a config read failure leaves the
        existing StatusBar label intact.
        """
        try:
            from flowly.config.loader import load_config
            cfg = await asyncio.to_thread(load_config)
        except Exception:
            return
        new_model = (cfg.agents.defaults.model or "").strip()
        if new_model:
            try:
                self.query_one(StatusBar).model = new_model
            except Exception:
                pass
        # Provider label also moves whenever ``providers.active`` flips —
        # e.g. Save in Desktop pushes ``active="flowly"``. _refresh… reads
        # the same Config object indirectly via _active_provider_display,
        # so this stays in lockstep with the StatusBar.model update above.
        self._refresh_active_provider_status()

    # --- bootstrap -------------------------------------------------

    async def _refresh_command_palette(self) -> None:
        try:
            cat = await self._client.commands_list()
        except Exception:
            return
        items: list[tuple[str, str]] = []
        for entry in cat.get("builtin", []):
            name = str(entry.get("name", "")).strip()
            if name:
                items.append((f"/{name}", str(entry.get("description", ""))))
        for entry in cat.get("plugin", []):
            name = str(entry.get("name", "")).strip()
            if name:
                items.append((f"/{name}", str(entry.get("description", "")) + "  (plugin)"))
        for entry in cat.get("bundle", []):
            name = str(entry.get("name", "")).strip()
            if name:
                count = entry.get("skill_count")
                desc = str(entry.get("description", ""))
                if count:
                    desc = f"{desc} · {count} skills" if desc else f"{count} skills"
                items.append((f"/{name}", desc + "  (bundle)"))
        skill_commands: dict[str, str] = {}
        for entry in cat.get("skill", []):
            name = str(entry.get("name", "")).strip()
            if name:
                command = f"/{name}".lower()
                skill_commands[command] = str(entry.get("display_name") or name)
                desc = str(entry.get("description", ""))
                items.append((f"/{name}", desc + "  (skill)"))
        self._skill_commands = skill_commands
        if items:
            self.query_one(Composer).set_palette(items)

    def _refresh_command_palette_after_skill_write(self, ev: ToolComplete) -> bool:
        """Refresh slash/skill completions after tools mutate the skill tree."""
        if not ev.success:
            return False
        if ev.name not in {"skill_manage", "skill_improve"}:
            return False
        asyncio.create_task(self._refresh_command_palette())
        return True

    def _reset_context_usage(self) -> None:
        self._usage_totals = {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "turns": 0, "cost_usd": 0.0, "cost_known": 0,
        }
        try:
            self.query_one(StatusBar).reset_context_usage()
        except Exception:
            pass

    def _accumulate_usage(
        self, tin: int, tout: int, cread: int, cwrite: int, model: str
    ) -> None:
        """Fold one turn's token counts into the cumulative session totals and
        estimate its cost from the catalog price of ``model``. Feeds the live
        cost badge and backs the /usage screen. Cost is billed per-turn (each
        turn pays for its full input), so input is summed across turns — unlike
        the context-window bar, which shows only the latest turn's occupancy."""
        t = self._usage_totals
        t["input"] += tin
        t["output"] += tout
        t["cache_read"] += cread
        t["cache_write"] += cwrite
        t["turns"] += 1
        try:
            from flowly.integrations.model_catalog import get_pricing
            pricing = get_pricing(model)
        except Exception:
            pricing = None
        if pricing is not None:
            pin, pout = pricing
            t["cost_usd"] += (tin * (pin or 0) + tout * (pout or 0)) / 1_000_000
            t["cost_known"] += 1
            try:
                self.query_one(StatusBar).cost_usd = t["cost_usd"]
            except Exception:
                pass

    async def _preload_history(self) -> None:
        # A preload can render an empty session or a history without usage
        # metadata. Clear stale context-window numbers first, then hydrate
        # them again below if the loaded history carries usage.
        self._reset_context_usage()
        # Primary: query history under the current (canonical) key.
        try:
            messages = await self._client.chat_history(
                self._session_key, limit=HISTORY_PRELOAD_LIMIT
            )
        except Exception as exc:
            self.query_one(TranscriptPane).add_system(
                f"history preload skipped: {exc}"
            )
            return

        # Fallback: a pre-fix session stored under the raw ``tui-…`` key
        # might still own messages on disk if the user never reopened
        # since the canonicalisation migration landed. If our canonical
        # lookup came back empty AND the key has a ``cli:`` prefix, peek
        # at the raw form too — recover history transparently so users
        # don't think their conversation evaporated.
        if not messages and self._session_key.startswith("cli:"):
            raw_key = self._session_key[len("cli:"):]
            try:
                raw_messages = await self._client.chat_history(
                    raw_key, limit=HISTORY_PRELOAD_LIMIT
                )
            except Exception:
                raw_messages = []
            if raw_messages:
                messages = raw_messages

        transcript = self.query_one(TranscriptPane)
        if not messages:
            # Empty session → render Flowly welcome banner.
            palette = get_palette()
            width = self.size.width or 100
            # Use the actual gateway URL the client is bound to (host/port
            # may have been overridden via --host/--port), not a hardcoded
            # default. Pairing state comes from the loaded account: an
            # account with a server_id means /login wired channels.web
            # into the gateway config, so iOS can reach this machine.
            gateway_url = getattr(self._client, "_url", "ws://127.0.0.1:18790")
            ios_paired = bool(
                self._account
                and getattr(self._account, "server_id", None)
            )
            provider_label, provider_source = self._active_provider_display()
            transcript.add_welcome(
                build_welcome(
                    self._session_key, self._model_hint, palette,
                    width=width,
                    gateway_url=gateway_url,
                    ios_paired=ios_paired,
                    provider=provider_label,
                    provider_source=provider_source,
                )
            )
            return
        # Hydrate status-bar tokens from the most recent usage record so
        # the context bar reflects current occupancy on resume instead
        # of looking like a fresh chat. Walk backwards through history
        # to find the last message that carries usage metadata (the
        # gateway attaches it to assistant turns).
        last_usage = None
        for msg in reversed(messages):
            usage = msg.get("usage")
            if isinstance(usage, dict) and usage:
                last_usage = usage
                break
        if last_usage:
            try:
                status = self.query_one(StatusBar)
                tin = int(
                    last_usage.get("prompt_tokens")
                    or last_usage.get("input_tokens") or 0
                )
                tout = int(
                    last_usage.get("completion_tokens")
                    or last_usage.get("output_tokens") or 0
                )
                if tin:
                    status.tokens_in = tin
                if tout:
                    status.tokens_out = tout
            except Exception:
                pass

        for msg in messages[-HISTORY_PRELOAD_LIMIT:]:
            role = msg.get("role", "")
            text = _flatten_content(msg.get("content"))
            if not text:
                continue
            if role == "user":
                transcript.add_user(text, timestamp=msg.get("timestamp"))
            elif role == "assistant":
                transcript.add_assistant(text)
            elif role == "system":
                transcript.add_system(text)
        transcript.add_marker(
            f"· resumed {len(messages)} messages ·"
        )

    # --- event pump ------------------------------------------------

    async def _consume_events(self) -> None:
        try:
            async for ev in self._client.events():
                self._handle_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self.query_one(TranscriptPane).add_error(f"event loop crashed: {exc}")

    def _handle_event(self, ev: object) -> None:
        transcript = self.query_one(TranscriptPane)
        status = self.query_one(StatusBar)

        if isinstance(ev, StreamDelta):
            # Pre-stream placeholder bubble was opened in _dispatch; reuse
            # it for the very first delta of this run. Only create a new
            # bubble when the run_id changes (e.g. after a tool break that
            # closes the bubble via ToolStart).
            if not self._current_bubble:
                self._current_bubble = transcript.start_assistant()
                self._current_bubble.mark_streaming(True)
            if not self._current_run:
                self._current_run = ev.run_id
            self._current_bubble.append(ev.text)
            transcript.request_tail_scroll()
            return

        if isinstance(ev, ChatFinal):
            if self._current_bubble:
                if not self._current_bubble._text.strip():
                    self._current_bubble.update_text(ev.text)
                self._current_bubble.mark_streaming(False)
            else:
                b = transcript.start_assistant()
                b.update_text(ev.text)
            self._finish_skill_notice(ev.run_id)
            self._current_bubble = None
            self._current_run = None
            self._set_state("idle")
            # Token usage — REPLACES, doesn't accumulate (prompt_tokens
            # already includes the full conversation history that the
            # LLM sees on every turn).
            #
            # Cache semantics (see ``agent/usage_pricing.py`` for
            # the canonical normalize_usage helper):
            #   • **OAI-compat mode** (OpenAI / OpenRouter / Groq /
            #     xAI via OR / DeepSeek / Codex) — ``prompt_tokens``
            #     is the FULL input incl. cached chunks. Cache details
            #     are nested under ``prompt_tokens_details.cached_tokens``
            #     (or top-level ``cache_read_input_tokens`` when OR
            #     proxies Anthropic). Adding them again would triple-
            #     count the same bytes.
            #   • **Anthropic native** — ``input_tokens`` is NEW only,
            #     cache fields are separate; sum is required.
            # ``OpenRouterProvider`` is always OAI-compat, so we use
            # ``prompt_tokens`` directly. The bare ``input_tokens``
            # fallback covers any pure-Anthropic adapter we might
            # introduce later.
            if ev.usage:
                u = ev.usage
                tin = int(
                    u.get("prompt_tokens")
                    or u.get("input_tokens")
                    or u.get("inputTokens")
                    or 0
                )
                tout = int(
                    u.get("completion_tokens")
                    or u.get("output_tokens")
                    or u.get("outputTokens")
                    or 0
                )
                if tin:
                    status.tokens_in = tin
                if tout:
                    status.tokens_out = tout
                cread = int(u.get("cache_read_tokens") or u.get("cache_read_input_tokens") or 0)
                cwrite = int(u.get("cache_write_tokens") or u.get("cache_creation_input_tokens") or 0)
                self._accumulate_usage(tin, tout, cread, cwrite, status.model)
            asyncio.create_task(self._drain_queue())
            # First-touch hint: brand-new user just saw their first
            # turn finish — point at /retry and /undo, which they have
            # no other way to discover.
            self._fire_hint(HINT_FIRST_TURN)
            return

        if isinstance(ev, ChatAborted):
            if self._current_bubble:
                self._current_bubble.mark_streaming(False)
            transcript.add_system("⊘ aborted")
            self._discard_skill_notice(ev.run_id)
            self._current_bubble = None
            self._current_run = None
            self._set_state("idle")
            asyncio.create_task(self._drain_queue())
            return

        if isinstance(ev, ChatError):
            if self._current_bubble:
                self._current_bubble.mark_streaming(False)
            transcript.add_error(f"error: {ev.message}")
            self._discard_skill_notice(ev.run_id)
            self._current_bubble = None
            self._current_run = None
            self._set_state("error")
            asyncio.create_task(self._drain_queue())
            return

        if isinstance(ev, ApprovalRequest):
            # First approval — surface the keyboard shortcuts in the
            # transcript so the user has a hint in front of them while
            # focus moves to the inline approval list.
            self._fire_hint(HINT_FIRST_APPROVAL)
            self._enqueue_approval(ev)
            return

        if isinstance(ev, ArtifactEvent):
            artifact_id = str(ev.artifact.get("id") or "")
            composer = self.query_one(Composer)
            if ev.action == "deleted":
                if artifact_id:
                    composer.remove_artifact(artifact_id)
                return
            session_key = str(ev.artifact.get("session_key") or "")
            if session_key != self._session_key:
                return
            composer.upsert_artifact(ev.artifact)
            return

        if isinstance(ev, ToolStart):
            if ev.session_key and ev.session_key != self._session_key:
                return
            if not self._current_bubble:
                self._current_bubble = transcript.start_assistant()
                self._current_bubble.mark_streaming(True)
            transcript.add_tool(
                ev.tool_call_id,
                ev.name,
                ev.args,
                bubble=self._current_bubble,
            )
            # First tool run — most users expect Ctrl+C to quit the
            # TUI, point out that it actually aborts the turn.
            self._fire_hint(HINT_FIRST_TOOL)
            return

        if isinstance(ev, ToolComplete):
            if ev.session_key and ev.session_key != self._session_key:
                return
            self._refresh_command_palette_after_skill_write(ev)
            line = transcript.find_tool(ev.tool_call_id)
            if line:
                line.complete(ev.success, ev.duration_ms, ev.preview)
            else:
                # Tool already gc'd or never seen start; render a one-off line
                line = transcript.add_tool(ev.tool_call_id, ev.name, {})
                line.complete(ev.success, ev.duration_ms, ev.preview)
            return

        if isinstance(ev, SubagentStarted):
            self.query_one(SubagentPane).add_started(
                {"runId": ev.run_id, "label": ev.label, "task": ev.task, "model": ev.model}
            )
            status.bg_count += 1
            return

        if isinstance(ev, SubagentCompleted):
            self.query_one(SubagentPane).mark_completed(
                {"runId": ev.run_id, "status": ev.status, "error": ev.error}
            )
            status.bg_count = max(0, status.bg_count - 1)
            return

        if isinstance(ev, CompactionEvent):
            transcript.add_system(
                f"⚡ context compacted · {ev.before_messages}→{ev.after_messages} msgs"
                + (
                    f" · {ev.before_tokens:,}→{ev.after_tokens:,} tokens"
                    if ev.before_tokens
                    else ""
                )
            )
            status.cmp_count += 1
            return

        if isinstance(ev, Reconnecting):
            self._set_state("reconnecting")
            status.hint = (
                f"reconnect attempt {ev.attempt} · waiting {ev.delay_s:.0f}s · "
                f"last: {ev.last_error[:60]}"
            )
            return

        if isinstance(ev, Reconnected):
            self._set_state("idle")
            self._refresh_busy_hint(queued=self.query_one(Composer).queue_size())
            transcript.add_system(f"✓ reconnected (attempt {ev.attempt})")
            # Re-sync ephemeral state that the gateway may have forgotten.
            asyncio.create_task(self._refresh_command_palette())
            asyncio.create_task(self._poll_badges())
            # Re-read model + provider from disk in case another process
            # (Desktop's Save flow, an external editor, etc.) changed the
            # config while we were disconnected. The gateway picks up the
            # new config on its restart, but the StatusBar shows whatever
            # value was loaded at TUI startup until something nudges it —
            # so a Desktop "save model" round-trip used to require killing
            # and restarting the TUI to see the new label.
            asyncio.create_task(self._resync_config_after_reconnect())
            return

        if isinstance(ev, ConnectionLost):
            self._set_state("offline")
            transcript.add_error(
                f"⚠ gateway unreachable: [dim]{ev.reason}[/dim]\n\n"
                f"Start it with: [b]flowly gateway[/b] in another terminal."
            )
            self._current_bubble = None
            self._current_run = None
            self.query_one(Composer).clear_queue()
            self._refresh_busy_hint(queued=0)
            return

    def _enqueue_approval(self, req: ApprovalRequest) -> None:
        self._approval_queue.append(req)
        if not self._approval_active:
            self._approval_active = True
            self._drain_approval_queue()

    @work
    async def _drain_approval_queue(self) -> None:
        try:
            while self._approval_queue:
                req = self._approval_queue.pop(0)
                decision = await self._show_inline_approval(req)
                await self._client.approval_resolve(
                    req.request_id,
                    decision,
                    remember=(decision == "allow-always"),
                )
        finally:
            self._approval_active = False

    async def _show_inline_approval(self, req: ApprovalRequest) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._approval_choice_future = fut
        composer = self.query_one(Composer)
        composer.show_approval(
            ApprovalPromptRequest(
                request_id=req.request_id,
                command=req.command,
                reasons=req.reasons,
                session_key=req.session_key,
                expires_at=req.expires_at,
                cwd=req.cwd,
                resolved_path=req.resolved_path,
                supports_always=req.supports_always,
            )
        )
        try:
            return await fut
        except asyncio.CancelledError:
            return "deny"
        finally:
            self._approval_choice_future = None
            composer.clear_approval()

    @on(ApprovalPrompt.Decision)
    def _on_approval_decision(self, event: ApprovalPrompt.Decision) -> None:
        event.stop()
        fut = self._approval_choice_future
        if fut is not None and not fut.done():
            fut.set_result(event.decision)

    # --- inline memory review queue --------------------------------

    async def _maybe_show_memory_review(self) -> None:
        """Fetch the bot's review queue and surface it inline if non-empty.
        Silent on any error (old gateway / offline) — never blocks the open."""
        if self._memory_review_dismissed:
            return
        try:
            items = await self._client.memory_review()
        except Exception:
            return
        items = [i for i in items if isinstance(i, dict) and i.get("id")]
        if not items:
            return
        self._memory_review_items = items
        self._memory_review_idx = 0
        try:
            self.query_one(Composer).show_memory_review(items[0], 0, len(items))
        except Exception:
            pass

    async def _open_memory_review_or_note(self) -> None:
        """`/memory` — open the review panel, or note an empty queue to the user."""
        try:
            items = await self._client.memory_review()
        except Exception as exc:
            self.query_one(TranscriptPane).add_error(f"memory.review failed: {exc}")
            return
        items = [i for i in items if isinstance(i, dict) and i.get("id")]
        if not items:
            self.query_one(TranscriptPane).add_system("memory review queue is empty")
            return
        self._memory_review_items = items
        self._memory_review_idx = 0
        try:
            self.query_one(Composer).show_memory_review(items[0], 0, len(items))
        except Exception:
            pass

    @on(MemoryReviewPanel.Decision)
    async def _on_memory_review_decision(self, event: MemoryReviewPanel.Decision) -> None:
        event.stop()
        composer = self.query_one(Composer)
        if event.action == "close":
            # Esc → stop auto-popping for the rest of this session.
            self._memory_review_dismissed = True
            composer.clear_memory_review()
            return
        items, idx = self._memory_review_items, self._memory_review_idx
        if idx >= len(items):
            composer.clear_memory_review()
            return
        item = items[idx]
        if event.action in ("keep", "discard"):
            try:
                if event.action == "keep":
                    await self._client.memory_accept(str(item.get("id") or ""))
                else:
                    await self._client.memory_reject(str(item.get("id") or ""))
            except Exception as exc:
                self.query_one(TranscriptPane).add_error(f"memory {event.action} failed: {exc}")
        # keep / discard / skip all advance to the next pending item.
        self._memory_review_idx += 1
        nxt = self._memory_review_idx
        if nxt >= len(items):
            composer.clear_memory_review()
            self.query_one(TranscriptPane).add_system("✓ memory review done")
            return
        composer.show_memory_review(items[nxt], nxt, len(items))

    async def _show_inline_secret(self, req: InlineSecretPromptRequest) -> str | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()
        self._inline_secret_future = fut
        composer = self.query_one(Composer)
        composer.show_secret_prompt(req)
        try:
            return await fut
        except asyncio.CancelledError:
            return None
        finally:
            self._inline_secret_future = None
            composer.clear_secret_prompt()

    @on(InlineSecretPrompt.Submitted)
    def _on_inline_secret_submitted(self, event: InlineSecretPrompt.Submitted) -> None:
        event.stop()
        fut = self._inline_secret_future
        if fut is not None and not fut.done():
            fut.set_result(event.value)

    @on(InlineSecretPrompt.Cancelled)
    def _on_inline_secret_cancelled(self, event: InlineSecretPrompt.Cancelled) -> None:
        event.stop()
        fut = self._inline_secret_future
        if fut is not None and not fut.done():
            fut.set_result(None)

    async def _show_inline_setup(
        self,
        req: InlineSetupPromptRequest,
    ) -> dict[str, object] | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, object] | None] = loop.create_future()
        self._inline_setup_future = fut
        composer = self.query_one(Composer)
        composer.show_setup_prompt(req)
        try:
            return await fut
        except asyncio.CancelledError:
            return None
        finally:
            self._inline_setup_future = None
            composer.clear_setup_prompt()

    @on(InlineSetupPrompt.Submitted)
    def _on_inline_setup_submitted(self, event: InlineSetupPrompt.Submitted) -> None:
        event.stop()
        fut = self._inline_setup_future
        if fut is not None and not fut.done():
            fut.set_result(event.values)

    @on(InlineSetupPrompt.Cancelled)
    def _on_inline_setup_cancelled(self, event: InlineSetupPrompt.Cancelled) -> None:
        event.stop()
        fut = self._inline_setup_future
        if fut is not None and not fut.done():
            fut.set_result(None)

    @on(UsagePanel.Dismissed)
    def _on_usage_dismissed(self, event: UsagePanel.Dismissed) -> None:
        event.stop()
        try:
            self.query_one(Composer).clear_usage()
        except Exception:
            pass

    @on(SessionStatusPanel.Dismissed)
    def _on_status_panel_dismissed(self, event: SessionStatusPanel.Dismissed) -> None:
        event.stop()
        try:
            self.query_one(Composer).clear_status()
        except Exception:
            pass

    async def _show_composer_picker(self, picker: Any, *, inline: bool = False) -> Any:
        prev = self._composer_picker_future
        if prev is not None and not prev.done():
            prev.set_result(None)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._composer_picker_future = fut
        composer = self.query_one(Composer)
        await composer.show_picker(picker, inline=inline)
        try:
            return await fut
        except asyncio.CancelledError:
            return None
        finally:
            if self._composer_picker_future is fut:
                self._composer_picker_future = None
                await composer.clear_picker()

    def _finish_composer_picker(self, result: Any) -> None:
        fut = self._composer_picker_future
        if fut is not None and not fut.done():
            fut.set_result(result)

    @on(ProviderPickerPanel.Dismissed)
    def _on_provider_picker_dismissed(self, event: ProviderPickerPanel.Dismissed) -> None:
        event.stop()
        self._finish_composer_picker(event.result)

    @on(ModelPickerPanel.Dismissed)
    def _on_model_picker_dismissed(self, event: ModelPickerPanel.Dismissed) -> None:
        event.stop()
        self._finish_composer_picker(event.result)

    @on(IntegrationsPanel.Dismissed)
    def _on_integrations_panel_dismissed(self, event: IntegrationsPanel.Dismissed) -> None:
        event.stop()
        self._finish_composer_picker(event.result)

    async def _show_inline_screen(self, screen: Any) -> Any:
        # Keep Textual's screen stack for focus, Esc bindings, OptionList
        # navigation, and push_screen_wait results. Runtime CSS renders these
        # screens as composer-adjacent bottom sheets instead of centered modals.
        return await self.push_screen_wait(screen)

    def _safe_transcript_system(self, text: str) -> None:
        try:
            transcript = self.query_one(TranscriptPane)
            if getattr(transcript, "is_attached", False):
                transcript.add_system(text)
        except Exception:
            pass

    def _safe_transcript_error(self, text: str) -> None:
        try:
            transcript = self.query_one(TranscriptPane)
            if getattr(transcript, "is_attached", False):
                transcript.add_error(text)
        except Exception:
            pass

    # --- composer handlers ----------------------------------------

    @on(Composer.ArtifactOpen)
    def _on_artifact_open(self, event: Composer.ArtifactOpen) -> None:
        # push_screen_wait below requires a worker context (Textual raises
        # NoActiveWorker from a plain message handler), so delegate.
        self._open_artifact_screen(dict(event.artifact))

    @work
    async def _open_artifact_screen(self, artifact_ref: dict) -> None:
        artifact_id = str(artifact_ref.get("id") or "")
        if not artifact_id:
            return
        transcript = self.query_one(TranscriptPane)
        try:
            artifact = await self._client.artifacts_get(artifact_id)
        except Exception as exc:
            transcript.add_error(f"artifact fetch failed: {exc}")
            return
        if not artifact:
            transcript.add_error("artifact not found")
            return

        artifact_type = str(artifact.get("type") or "").lower()
        if is_external_artifact_type(artifact_type):
            result = await asyncio.to_thread(open_artifact_external, artifact)
            if result.status == "opened":
                self.notify(
                    f"opened {artifact_type} artifact in the default app",
                    severity="information",
                    timeout=3,
                )
            elif result.status == "headless":
                message = (
                    f"no graphical session; cannot open {artifact_type}. "
                    "Press F4 for source preview."
                )
                self.notify(message, severity="warning", timeout=6)
                transcript.add_system(message)
            else:
                detail = f" ({result.detail})" if result.detail else ""
                message = (
                    f"could not open {artifact_type} artifact{detail}; "
                    "press F4 for source preview"
                )
                self.notify(message, severity="warning", timeout=6)
                transcript.add_system(message)
            self.query_one(Composer).focus_input_safely()
            return

        # Open with the whole chat's artifacts so ←/→ can move between them;
        # siblings are summaries and the modal lazy-loads their content.
        siblings = self.query_one(Composer).session_artifacts()
        initial = next(
            (
                i
                for i, item in enumerate(siblings)
                if str(item.get("id") or "") == artifact_id
            ),
            None,
        )
        if initial is None:
            siblings, initial = [artifact], 0
        else:
            siblings[initial] = dict(artifact)
        await self._show_inline_screen(
            ArtifactsModal(
                siblings,
                initial_index=initial,
                fetcher=self._client.artifacts_get,
            )
        )
        self.query_one(Composer).focus_input_safely()

    @on(Composer.Submitted)
    async def _on_send(self, event: Composer.Submitted) -> None:
        self._session_used = True
        # Non-blocking queue: if a turn is already running, push onto the
        # queue and let _drain_queue() pick it up when the current turn
        # ends. Input is NEVER disabled — user can keep typing or queue
        # several follow-ups in a row.
        if self._current_run:
            composer = self.query_one(Composer)
            n = composer.enqueue(event.text, event.attachments)
            self._refresh_busy_hint(queued=n)
            return
        await self._dispatch(event.text, event.attachments)

    async def _dispatch(
        self,
        text: str,
        attachments=None,
        *,
        skill_notice: str | None = None,
    ) -> None:
        """Single source of truth for sending a user message."""
        attachment_paths = list(attachments or [])
        transcript = self.query_one(TranscriptPane)
        status = self.query_one(StatusBar)
        try:
            outbound_attachments = await self._prepare_outbound_attachments(attachment_paths)
        except AttachmentPreparationError as exc:
            transcript.add_error(str(exc))
            status.state = "error"
            return

        transcript.add_user(render_message_with_attachments(text, attachment_paths))
        status.state = "busy"
        # Pre-stream placeholder bubble — shows blinking ▌ until the
        # first delta arrives. Replaced/promoted by StreamDelta handler.
        self._current_bubble = transcript.start_assistant()
        self._current_bubble.mark_streaming(True)
        try:
            run_id = await self._client.chat_send(
                text,
                session_key=self._session_key,
                attachments=outbound_attachments,
            )
            self._current_run = run_id
            if skill_notice:
                self._skill_notice_by_run[run_id] = skill_notice
        except Exception as exc:
            if self._current_bubble:
                self._current_bubble.mark_streaming(False)
            transcript.add_error(f"send failed: {exc}")
            status.state = "error"
            self._current_bubble = None
            self._current_run = None

    async def _prepare_outbound_attachments(self, attachment_paths: list) -> list[dict[str, Any]]:
        if not attachment_paths:
            return []

        account = self._account
        if any(is_video_path(path) for path in attachment_paths):
            from flowly.account.auth import load_account_refreshing

            account = await load_account_refreshing()
            self._account = account
            if getattr(self, "_account_ref", None):
                self._account_ref[0] = account

        def on_upload_start(path) -> None:
            self.notify(f"uploading video: {path.name}", timeout=3)

        return await prepare_media_attachments(
            attachment_paths,
            account=account,
            conversation_id=self._session_key,
            on_upload_start=on_upload_start,
        )

    async def _drain_queue(self) -> None:
        """Called whenever a turn ends — auto-send the next queued message."""
        composer = self.query_one(Composer)
        item = composer.dequeue()
        if not item:
            self._refresh_busy_hint(queued=0)
            return
        self._refresh_busy_hint(queued=composer.queue_size())
        await self._dispatch(item.text, item.attachments, skill_notice=item.skill_notice)

    def _refresh_busy_hint(self, *, queued: int) -> None:
        status = self.query_one(StatusBar)
        base = "ctrl+c abort · ctrl+l clear · ctrl+d quit · / commands"
        if queued > 0:
            status.hint = f"{queued} queued · {base}"
        else:
            status.hint = base

    @on(Composer.Shell)
    async def _on_shell(self, event: Composer.Shell) -> None:
        """Run an inline shell command (`!cmd`) and show the output locally.

        Never sent to the LLM. Useful for quick filesystem checks while
        chatting (ls, pwd, git status). Output capped at 4000 chars.
        """
        cmd = event.command
        transcript = self.query_one(TranscriptPane)
        transcript.add_system(f"$ {cmd}")
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                close_fds=True,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            text = stdout.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            transcript.add_error("shell command timed out after 30s")
            return
        except Exception as exc:
            transcript.add_error(f"shell error: {exc}")
            return
        if len(text) > 4000:
            text = text[:4000] + "\n…[truncated]"
        body = text.rstrip() or "(no output)"
        transcript.add_assistant(f"```\n{body}\n```")

    @on(Composer.Slash)
    async def _on_slash(self, event: Composer.Slash) -> None:
        cmd = event.command.strip()
        # Echo the command into the transcript so there's a visible audit
        # trail of what was actually invoked (dim italic muted style).
        self.query_one(TranscriptPane).add_slash_echo(cmd)
        head = cmd.split(maxsplit=1)[0].lower()
        rest = cmd[len(head):].strip()

        if head == "/quit":
            self.exit()
            return
        if head == "/abort":
            await self.action_abort_or_quit()
            return
        if head == "/help":
            self.action_open_help()
            return
        if head == "/status":
            s = self.query_one(StatusBar)
            composer = self.query_one(Composer)
            provider, provider_source = self._active_provider_display()
            composer.show_status(
                session=s.session or self._session_key,
                provider=provider,
                provider_source=provider_source,
                model=s.model or "",
                state=s.state,
                tokens_in=int(s.tokens_in),
                tokens_out=int(s.tokens_out),
                cost_usd=float(s.cost_usd),
                queued=len(getattr(composer, "_queue", [])),
            )
            return
        if head == "/usage":
            self.action_open_usage()
            return

        # gateway-side commands
        if head == "/clear":
            # ``--yes``, ``-y``, or ``now`` skip the prompt (useful in
            # scripts driving the TUI; matches the convention).
            skip_confirm = rest.strip().lower() in ("--yes", "-y", "now")
            # ``_do_clear`` is ``@work`` — don't await, it returns a
            # Worker handle that runs in the background.
            self._do_clear(skip_confirm=skip_confirm)
            return
        if head == "/new":
            # Start a *fresh* session and switch to it. The previous session
            # and its on-disk messages are left untouched — use ``/clear`` to
            # wipe the current one. (Switching with a brand-new key just shows
            # an empty transcript; nothing is deleted.)
            new_key = fresh_session_key()
            await self._switch_session(new_key)
            self.query_one(TranscriptPane).add_system(f"new session · {new_key}")
            return
        if head == "/retry":
            await self._do_retry()
            return
        if head == "/undo":
            await self._do_undo()
            return
        if head == "/compact":
            await self._do_compact(rest or None)
            return
        if head == "/sessions":
            self.action_open_sessions()
            return
        if head in ("/assistants", "/persona"):
            # ``/model`` used to alias here (assistant picker), but it's
            # now bound to the LLM-model picker (see below). Persona /
            # assistant switching lives under ``/assistants`` and
            # ``/persona`` so the two pickers don't collide.
            self.action_open_assistants()
            return
        if head == "/login":
            self.action_login()
            return
        if head == "/logout":
            await self.action_logout()
            return
        if head == "/whoami":
            await self.action_whoami()
            return
        if head == "/integrations":
            self.action_open_integrations()
            return
        if head == "/channels":
            self.action_open_channels()
            return
        if head == "/provider":
            # action_provider is ``@work`` decorated → returns a Worker,
            # not a coroutine. Don't await — let it run in the background.
            self.action_provider(rest)
            return
        if head == "/model":
            self.action_model(rest)
            return
        if head == "/theme":
            self.action_theme(rest)
            return
        if head == "/remote":
            # TUI-local on purpose: prints the remote-access TOKEN, which must
            # only ever land on this terminal — never in a chat channel.
            self.action_remote(rest)
            return
        if head == "/browser":
            self.action_browser()
            return
        if head == "/plugins":
            self.action_plugins()
            return
        if head == "/mcp":
            self.action_mcp()
            return
        if head == "/activity":
            self.action_open_activity()
            return
        if head == "/approvals":
            # `/approvals permissions` jumps straight to the permissions
            # editor; bare `/approvals` opens the pending-request queue.
            if rest.strip().lower() in ("permissions", "policy"):
                self.action_open_policy()
            else:
                self.action_open_approvals()
            return
        if head in ("/permissions", "/policy"):  # /policy = silent alias
            self.action_open_policy()
            return
        if head == "/artifacts":
            self.action_open_artifacts()
            return
        if head in ("/memory", "/review"):
            transcript = self.query_one(TranscriptPane)
            transcript.add_system("checking memory review queue…")
            asyncio.create_task(self._open_memory_review_or_note())
            return
        if head in ("/subagents", "/subs"):
            sub = rest.strip()
            if not sub:
                # bare → toggle the running-tasks panel (unchanged).
                self.action_toggle_subagents()
            elif sub.lower() == "models":
                # `/subagents models` → per-specialist model editor.
                self.action_subagent_models()
            else:
                # `/subagents <task>` → launch a manual background subagent.
                self.action_spawn_subagent(sub)
            return

        if head in ("/board", "/kanban"):
            # Inline board (not a modal): view + run/del/done/cancel/add.
            self.query_one(TranscriptPane).add_slash_echo(cmd)
            self._board_command(rest)
            return

        if head == "/computer":
            # Informational only: computer use (desktop control) is a Flowly
            # Desktop feature — the terminal can't hold the macOS permissions it
            # needs. Tell the user where to get it; a TUI-native path is planned.
            self.query_one(TranscriptPane).add_system(_COMPUTER_INFO)
            return

        skill_name = self._skill_commands.get(head)
        skill_notice = f"⚡ loading skill: {skill_name}" if skill_name else None

        # fallback: forward unknown slash commands to gateway as plain message
        # (the gateway might own a plugin-defined command)
        if not skill_name:
            self.query_one(TranscriptPane).add_system(
                f"command '{head}' not handled locally; sending as message"
            )
        await self._send_as_message(cmd, skill_notice=skill_notice)

    # legacy inline help removed in favor of HelpModal (Ctrl+? / /help / F1)

    @work
    async def _board_command(self, rest: str) -> None:
        """Handle /board [list | add <title> | run|done|cancel|del <id> | help]."""
        transcript = self.query_one(TranscriptPane)
        parts = rest.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("", "list", "ls"):
            await self._board_render(transcript)
            return
        if sub in ("help", "?"):
            transcript.add_system(_BOARD_HELP)
            return
        if sub == "add":
            if not arg:
                transcript.add_error("usage: /board add <title>")
                return
            await self._board_do(
                transcript,
                {"action": "add", "title": arg, "originChannel": "cli", "originChatId": self._session_key},
                f"added · {arg}",
            )
            return
        if sub == "clear":
            status = (arg.strip().lower() or "done")
            await self._board_do(
                transcript, {"action": "clear_done", "status": status},
                f"cleared {status} cards",
            )
            return
        if sub in ("run", "del", "delete", "rm", "remove", "done", "cancel", "stop"):
            if not arg:
                transcript.add_error(f"usage: /board {sub} <card id>")
                return
            cid = await self._board_resolve_id(arg)
            if cid is None:
                transcript.add_error(f"no single card matching '{arg}'")
                return
            if sub == "run":
                await self._board_do(transcript, {"action": "run", "cardId": cid}, f"running {cid} …")
            elif sub in ("del", "delete", "rm", "remove"):
                await self._board_do(transcript, {"action": "delete", "cardId": cid}, f"deleted {cid}")
            elif sub == "done":
                await self._board_do(transcript, {"action": "move", "cardId": cid, "status": "done"}, f"{cid} → done")
            else:  # cancel / stop
                await self._board_do(transcript, {"action": "cancel", "cardId": cid}, f"cancelled {cid}")
            return
        transcript.add_error(f"unknown /board subcommand: {sub} · try /board help")

    async def _board_render(self, transcript: "TranscriptPane") -> None:
        try:
            snap = await self._client.board_snapshot()
        except Exception as exc:
            transcript.add_error(f"board unavailable: {exc}")
            return
        if snap is None:
            transcript.add_system("Board is not available on this gateway.")
            return
        transcript.add_assistant(_format_board(snap))

    async def _board_resolve_id(self, arg: str) -> str | None:
        """Resolve a full or partial card id to a unique id, else None."""
        try:
            snap = await self._client.board_snapshot()
        except Exception:
            return None
        if not snap:
            return None
        ids = [c.get("id", "") for col in snap.get("columns", []) for c in col.get("cards", [])]
        if arg in ids:
            return arg
        matches = [i for i in ids if i.startswith(arg) or i.endswith(arg)]
        return matches[0] if len(matches) == 1 else None

    async def _board_do(self, transcript: "TranscriptPane", payload: dict, ok_msg: str) -> None:
        try:
            res = await self._client.board_action(payload)
        except Exception as exc:
            transcript.add_error(f"board: {exc}")
            return
        if res.get("ok") is False:
            transcript.add_error(f"board: {res.get('error', 'failed')}")
            return
        transcript.add_system(ok_msg)
        await self._board_render(transcript)

    @work
    async def _do_clear(self, *, skip_confirm: bool = False) -> None:
        # ``@work`` is mandatory because Textual now requires
        # ``push_screen_wait`` to run inside a worker — calling it
        # from a regular ``async`` method (or the bare slash dispatch
        # path) raises ``NoActiveWorker``. Callers should NOT ``await``
        # this; the decorator returns a Worker that runs in the
        # background, which is the right shape for fire-and-forget
        # UI side effects.
        transcript = self.query_one(TranscriptPane)
        # Count what's about to disappear so the confirmation message
        # is concrete instead of generic "clear session?".
        msg_count = 0
        try:
            history = await self._client.chat_history(
                self._session_key, limit=10_000,
            )
            msg_count = len(history)
        except Exception:
            # Read failure isn't a blocker — fall through to confirm
            # with the count unknown.
            pass

        if not skip_confirm and msg_count > 0:
            confirmed = await self._show_inline_screen(
                ConfirmModal(
                    title="Clear session?",
                    body=(
                        f"This will discard {msg_count} message(s) from "
                        f"[b]{self._session_key}[/].\n"
                        "On-disk session file will be reset.\n\n"
                        "Pass [cyan]/clear --yes[/] to skip this prompt."
                    ),
                    confirm_label="Clear",
                )
            )
            if not confirmed:
                transcript.add_system("clear cancelled")
                return
        try:
            await self._client.chat_clear(self._session_key)
        except Exception as exc:
            transcript.add_error(f"clear failed: {exc}")
            return
        for child in list(transcript.children):
            child.remove()
        # Drop dangling references — bubble widgets just got removed.
        self._current_bubble = None
        self._current_run = None
        self._reset_context_usage()
        transcript.add_system(f"session cleared · {self._session_key}")
        # First /clear — let the user know `--yes` skips the prompt
        # next time, helpful for scripted callers and power users who
        # know they don't want a confirm.
        self._fire_hint(HINT_FIRST_CLEAR)

    async def _do_retry(self) -> None:
        """Re-submit the last user message after dropping the stale reply."""
        transcript = self.query_one(TranscriptPane)
        # Use the StatusBar state (set/cleared atomically with the
        # streaming bubble) as the source of truth for "is there an
        # active turn?". ``_current_run`` is a back-channel cache and
        # can lag — a late delta or duplicate final event has been seen
        # to leave it populated after the visible turn has clearly
        # ended. The status bar reflects what the user sees.
        try:
            status_state = self.query_one(StatusBar).state
        except Exception:
            status_state = "idle"
        if status_state == "busy":
            transcript.add_system(
                "can't retry mid-turn — /abort the current run first"
            )
            return
        # Defensive: if we got here with a stale ``_current_run`` (status
        # is idle but the cache wasn't cleared), drop it now — the next
        # ``chat_send`` would otherwise short-circuit through the queue
        # path instead of dispatching.
        self._current_run = None
        try:
            result = await self._client.chat_retry(self._session_key)
        except Exception as exc:
            transcript.add_error(f"retry failed: {exc}")
            return
        if not result.get("ok"):
            reason = result.get("reason") or "nothing to retry"
            transcript.add_system(f"retry: {reason}")
            return
        text = result.get("text") or ""
        removed = int(result.get("removed", 0))
        # Rebuild the transcript from the server's authoritative history
        # — simpler than surgically removing the last bubble (which
        # could be a streaming bubble + N tool lines).
        await self._preload_history()
        transcript.add_system(
            f"↻ retrying last prompt [dim](dropped {removed} message(s))[/]"
        )
        await self._send_as_message(text)

    async def _do_undo(self) -> None:
        """Pop the last user+assistant turn; pre-fill composer for edit."""
        transcript = self.query_one(TranscriptPane)
        try:
            status_state = self.query_one(StatusBar).state
        except Exception:
            status_state = "idle"
        if status_state == "busy":
            transcript.add_system(
                "can't undo mid-turn — /abort the current run first"
            )
            return
        # Defensive: clear stale ``_current_run`` — see _do_retry for context.
        self._current_run = None
        try:
            result = await self._client.chat_undo(self._session_key)
        except Exception as exc:
            transcript.add_error(f"undo failed: {exc}")
            return
        if not result.get("ok"):
            reason = result.get("reason") or "nothing to undo"
            transcript.add_system(f"undo: {reason}")
            return
        text = result.get("text") or ""
        removed = int(result.get("removed", 0))
        await self._preload_history()
        transcript.add_system(
            f"⤺ undone [dim](removed {removed} message(s) · "
            "previous prompt restored to composer)[/]"
        )
        # Pre-fill the composer so the user can edit and resubmit.
        # Best-effort — composer might not be ready in an exotic
        # teardown sequence.
        if text:
            try:
                composer = self.query_one(Composer)
                ed = composer.query_one("#composer-input")
                ed.text = text  # type: ignore[attr-defined]
                composer.focus_input()
            except Exception:
                pass

    async def _do_compact(self, instructions: str | None) -> None:
        """Trigger gateway-side context compaction.

        Compaction asks the LLM to summarise the running conversation
        into a short note, then replaces the oldest N messages with
        that summary. Frees prompt_tokens so the next turn has room.
        Optional ``instructions`` (everything after ``/compact``) guides
        the summariser — e.g. ``/compact keep the SQL schema verbatim``.
        """
        transcript = self.query_one(TranscriptPane)
        status = self.query_one(StatusBar)
        prior_state = status.state
        prior_hint = status.hint
        self._set_state("busy")
        status.hint = (
            f"compacting context…  {instructions!r}" if instructions
            else "compacting context…"
        )
        try:
            result = await self._client.chat_compact(self._session_key, instructions)
        except Exception as exc:
            transcript.add_error(f"compact failed: {exc}")
            self._set_state("error")
            status.hint = prior_hint
            return
        before = result.get("beforeMessages") or result.get("before_messages")
        after = result.get("afterMessages") or result.get("after_messages")
        before_tokens = result.get("beforeTokens") or result.get("before_tokens")
        after_tokens = result.get("afterTokens") or result.get("after_tokens")
        bits: list[str] = []
        if before is not None and after is not None:
            bits.append(f"{before} → {after} messages")
        if before_tokens and after_tokens:
            bits.append(f"{before_tokens:,} → {after_tokens:,} tokens")
        suffix = "  ·  " + " · ".join(bits) if bits else ""
        transcript.add_system(f"✓ context compacted{suffix}")
        # Compaction shrinks the prompt → reset the running token tally
        # so the status bar reflects the new (smaller) context use on
        # the NEXT turn's usage event. Without this the bar would stay
        # frozen at the pre-compact total until the model replies.
        if after_tokens is not None:
            try:
                status.tokens_in = int(after_tokens)
                status.tokens_out = 0
            except (TypeError, ValueError):
                pass
        self._set_state(prior_state if prior_state != "busy" else "idle")
        status.hint = prior_hint

    async def _switch_session(self, new_key: str) -> None:
        """Switch transcript to a different saved session and reload history."""
        if new_key == self._session_key:
            return
        self._session_key = new_key
        self.sub_title = new_key
        status = self.query_one(StatusBar)
        status.session = new_key
        transcript = self.query_one(TranscriptPane)
        for child in list(transcript.children):
            child.remove()
        self._current_bubble = None
        self._current_run = None
        self.query_one(Composer).set_artifacts([])
        # Persist immediately so a Ctrl+D right after switch still resumes here.
        state = load_state()
        state["last_session_key"] = new_key
        save_state(state)
        await self._preload_history()
        await self._refresh_session_artifacts()

    def _discard_skill_notice(self, run_id: str | None) -> None:
        if run_id:
            self._skill_notice_by_run.pop(run_id, None)
        if self._current_run:
            self._skill_notice_by_run.pop(self._current_run, None)

    def _finish_skill_notice(self, run_id: str | None) -> None:
        notice = None
        if run_id:
            notice = self._skill_notice_by_run.pop(run_id, None)
        if notice is None and self._current_run:
            notice = self._skill_notice_by_run.pop(self._current_run, None)
        if notice:
            self.query_one(TranscriptPane).add_system(notice)

    async def _send_as_message(
        self,
        text: str,
        *,
        skill_notice: str | None = None,
    ) -> None:
        if self._current_run:
            n = self.query_one(Composer).enqueue(text, skill_notice=skill_notice)
            self._refresh_busy_hint(queued=n)
            return
        await self._dispatch(text, skill_notice=skill_notice)

    # --- actions ---------------------------------------------------

    async def action_abort_or_quit(self) -> None:
        fut = self._approval_choice_future
        if fut is not None and not fut.done():
            fut.set_result("deny")
            return
        secret_fut = self._inline_secret_future
        if secret_fut is not None and not secret_fut.done():
            secret_fut.set_result(None)
            return
        setup_fut = self._inline_setup_future
        if setup_fut is not None and not setup_fut.done():
            setup_fut.set_result(None)
            return
        picker_fut = self._composer_picker_future
        if picker_fut is not None and not picker_fut.done():
            picker_fut.set_result(None)
            return
        if self._current_run:
            await self._client.chat_abort(self._current_run)
        else:
            self.exit()

    async def action_clear_session(self) -> None:
        # ``_do_clear`` is ``@work`` — see note there. Worker runs in
        # the background; key binding (Ctrl+L) returns immediately.
        self._do_clear()

    def action_toggle_subagents(self) -> None:
        self.query_one(SubagentPane).toggle()

    @work
    async def action_subagent_models(self) -> None:
        """``/subagents models`` — open the per-specialist model editor."""
        from flowly.tui.panes.subagent_models import SubagentModelsModal
        await self._show_inline_screen(SubagentModelsModal(self._client))

    @work
    async def action_spawn_subagent(self, task: str) -> None:
        """``/subagents <task>`` — launch a manual background subagent whose
        result is announced back into this session when it finishes."""
        transcript = self.query_one(TranscriptPane)
        task = (task or "").strip()
        if not task:
            transcript.add_error("/subagents: give a task, e.g. `/subagents research X`")
            return
        try:
            res = await self._client.subagents_spawn(task, session_key=self._session_key)
        except Exception as exc:
            transcript.add_error(f"/subagents: {exc}")
            return
        name = str(res.get("displayName") or task)[:60]
        transcript.add_system(
            f"🧵 subagent started · [b]{name}[/b] — the result will arrive here "
            "when it's done."
        )

    @work
    async def action_open_help(self) -> None:
        await self._show_inline_screen(HelpModal())

    # ── Connection catalogs ──────────────────────────────────────

    @work
    async def action_open_integrations(self) -> None:
        """Open external service integrations.

        Channels and LLM providers have their own surfaces: ``/channels``,
        ``/provider`` and ``/model``.
        """
        await self._open_card_catalog(
            categories=("tool", "web_search", "media", "voice"),
            title="Integrations",
            item_label="integration",
        )

    @work
    async def action_open_channels(self) -> None:
        """Open messaging channels separately from service integrations."""
        await self._open_card_catalog(
            categories=("channel",),
            title="Channels",
            item_label="channel",
        )

    async def _open_card_catalog(
        self,
        *,
        categories: tuple[str, ...],
        title: str,
        item_label: str,
    ) -> None:
        """Open a filtered connection catalog and then the selected setup form."""
        from flowly.integrations import get_card

        while True:
            result = await self._show_composer_picker(
                IntegrationsPanel(
                    categories=categories,
                    title=title,
                    item_label=item_label,
                ),
                inline=True,
            )
            if not result or result.get("action") != "opened":
                return
            key = str(result.get("key") or "")
            card = get_card(key)
            if card is None:
                return
            # Special case: /login-driven cards open the login modal
            # so the UX matches the system pairing flow rather than a form.
            if card.custom_action == "login":
                await self.action_login()
                continue
            await self._configure_card_inline(card)
            # Loop back to catalog so the user can configure another card.

    # ── Flowly account integration ───────────────────────────────

    @work
    async def action_login(self) -> None:
        from flowly.account.auth import (
            background_refresh_loop,
            load_account_sync,
        )
        transcript = self.query_one(TranscriptPane)
        existing = load_account_sync()
        if existing:
            transcript.add_system(
                f"already signed in as [b]{existing.email or existing.user_id}[/b] "
                f"— run /logout first to switch"
            )
            return
        result = await self._show_inline_screen(LoginModal())
        if not result:
            transcript.add_system("login cancelled")
            return
        self._account = result
        # Spin up background refresh so the token never expires under us.
        if not getattr(self, "_account_refresh_task", None):
            self._account_ref = [result]
            self._account_refresh_task = asyncio.create_task(
                background_refresh_loop(self._account_ref)
            )
        self._refresh_active_provider_status()
        # Itemise exactly what login mutated so the user has a clear audit
        # trail — never silently mutate config.
        mutations: list[str] = [
            "credentials → keychain",
            f"server registered → [cyan]{result.server_name or result.server_id or '?'}[/]",
        ]
        if result.server_id:
            mutations.append("channels.web wired → iOS pairing enabled")
        # If login auto-promoted Flowly to the default provider, surface it
        # here too (the login modal already showed it inline, but the
        # transcript is the durable record the user can scroll back to).
        try:
            from flowly.config.loader import load_config
            from flowly.integrations.active_provider import resolve_active_provider
            active = resolve_active_provider(load_config())
            if active and active.key == "flowly":
                mutations.append("[yellow]★[/] default LLM → Flowly hosted")
        except Exception:
            pass
        bullets = "\n".join(f"  • {m}" for m in mutations)
        transcript.add_system(
            f"✓ signed in as [b]{result.email or result.user_id}[/b] · "
            f"device [dim]{result.machine_name}[/dim]\n"
            f"{bullets}\n"
            f"[dim]restart [b]flowly gateway[/b] for pairing + provider changes "
            f"to take effect.[/dim]"
        )

    async def action_logout(self) -> None:
        from flowly.account.auth import clear_account, load_account_sync
        from flowly.account.relay_config import clear_relay_credentials
        transcript = self.query_one(TranscriptPane)
        existing = load_account_sync()
        if not existing:
            transcript.add_system("not signed in")
            return
        clear_account()
        # Disable the gateway's web channel too — otherwise it would keep
        # trying to authenticate to the relay with revoked credentials and
        # iOS would still appear "paired" from the server side.
        clear_relay_credentials()
        # If Flowly was the explicit default LLM provider, clear that
        # pointer so the gateway falls back to the BYOK cascade instead
        # of refusing to boot on missing credentials.
        try:
            from flowly.integrations.active_provider import clear_active_if_matches
            clear_active_if_matches("flowly")
        except Exception:
            pass
        self._account = None
        if getattr(self, "_account_refresh_task", None):
            self._account_refresh_task.cancel()
            self._account_refresh_task = None
        self._refresh_active_provider_status()
        transcript.add_system(
            f"✓ signed out [dim]({existing.email or existing.user_id})[/dim] · "
            f"iOS pairing disabled [dim](restart gateway to apply)[/dim]"
        )

    @work
    async def action_remote(self, rest: str) -> None:
        """``/remote`` — expose this bot for remote desktop access.

        Bare ``/remote`` (or ``on``) persists ``gateway.host=0.0.0.0``, makes
        sure a remote-access token exists (generated when missing), and prints
        the connection block the user types into the desktop app under
        Settings → Connections. ``/remote status`` re-prints it; ``/remote
        off`` reverts to local-only (the token is kept so already-configured
        desktops survive a later re-enable).

        TUI-local on purpose — the token is a secret and must only ever be
        printed on this terminal, never into a chat channel.
        """
        import asyncio as _asyncio

        from flowly.gateway.remote_info import (
            disable_remote_access,
            enable_remote_access,
            remote_access_status,
        )
        from flowly.gateway.remote_qr import remote_qr_markup

        transcript = self.query_one(TranscriptPane)
        arg = (rest or "").strip().lower()

        if arg == "off":
            r = await _asyncio.to_thread(disable_remote_access)
            if r.get("changed"):
                transcript.add_system(
                    "✓ remote access [b]disabled[/b] — gateway will bind "
                    "[b]127.0.0.1[/b] (local-only) · restart to apply: "
                    "[cyan]flowly service restart[/cyan] [dim]or restart the gateway[/dim]"
                )
            else:
                transcript.add_system("remote access is already off (loopback bind)")
            return

        if arg == "status":
            st = await _asyncio.to_thread(remote_access_status)
            if not st["enabled"]:
                transcript.add_system(
                    "remote access: [b]off[/b] [dim](gateway binds loopback)[/dim] — "
                    "run [cyan]/remote[/cyan] to enable"
                )
                return
            # fall through to the info block below via enable (idempotent —
            # nothing changes when already enabled + token present)

        # ``/remote`` / ``on`` / ``status``(enabled) → ensure + print the block.
        r = await _asyncio.to_thread(enable_remote_access)
        lan = r.get("lan_ip") or ""
        pub = r.get("public_ip") or ""
        changed = r["host_changed"] or r["token_changed"]
        lines = [
            "[b]Remote access[/b] — enter in the app: [b]Settings → Connections[/b]",
            "",
        ]
        # LAN IP first: same-Wi-Fi is the common case, and the public IP only
        # works from outside the network with a router port-forward.
        if lan:
            lines.append(f"  Same Wi-Fi (most common) → Host/IP : [b]{lan}[/b]")
        if pub:
            lines.append(f"  Over the internet (needs router port-forward) → Host/IP : [b]{pub}[/b]")
        if not lan and not pub:
            lines.append("  Host/IP : [b]<this machine's IP>[/b]")
        lines += [
            f"  Port    : [b]{r['port']}[/b]",
            f"  Token   : [b]{r['token']}[/b]",
            "  TLS     : [b]off[/b]  [dim](the gateway serves plain ws:// — leave 'Use TLS' off)[/dim]",
            "",
            "  [dim]Phone on the same Wi-Fi → use the first IP. Make sure the OS",
            "  firewall allows inbound on this port.[/dim]",
        ]
        if changed:
            lines.append(
                "  [yellow]Apply:[/yellow] [cyan]flowly service restart[/cyan] "
                "[dim]— or restart the gateway (bind/token changed)[/dim]"
            )
        # Append the scannable code into the SAME block, below the typed values
        # — same four fields, just point the app's camera at it. LAN IP first
        # (the same-Wi-Fi common case); the token rides inside, so this stays
        # TUI-local like the values above.
        primary = lan or pub
        qr = remote_qr_markup(primary, r["port"], r["token"]) if primary else None
        if qr:
            where = "same Wi-Fi" if lan else "this host"
            lines += [
                "",
                f"  [b]Or scan with the Flowly app[/b] [dim]({where} · {primary}:{r['port']})[/dim]",
                "",
                qr,
            ]
        # Don't collapse when a QR is present — the code must render in full.
        transcript.add_system("\n".join(lines), collapse_long=qr is None)

    async def _configure_provider_inline(
        self,
        card: IntegrationCard,
        *,
        make_active: bool = True,
    ) -> bool:
        """Collect the primary provider API key in the composer tray.

        This is the fast path for BYOK providers. The full
        IntegrationSetupModal remains available from the picker with ``E`` for
        fallback keys or other advanced edits.
        """
        field = _inline_provider_key_field(card)
        if field is None:
            return False

        value = await self._show_inline_secret(
            InlineSecretPromptRequest(
                title=f"Configure {card.label}",
                label=f"{field.label} for {card.label}",
                placeholder=field.placeholder,
                help=field.help or "Saved to ~/.flowly/config.json.",
                required=field.required,
                password=True,
            )
        )
        if value is None:
            self._safe_transcript_system(f"{card.label} setup cancelled")
            return False
        api_key = value.strip()
        if field.required and not api_key:
            self._safe_transcript_error(f"{card.label}: API key is required")
            return False

        try:
            from flowly.integrations import apply_card_values, read_card_values
            values = await asyncio.to_thread(read_card_values, card)
        except Exception:
            values = {}
        values[field.key] = api_key

        probe_note = ""
        if card.probe is not None:
            from flowly.integrations.probes import run_with_timeout
            result = await run_with_timeout(card.probe(values))
            if result.status in {"auth_failed", "not_configured"}:
                detail = f" — {result.detail}" if result.detail else ""
                self._safe_transcript_error(
                    f"{card.label}: credentials rejected{detail}"
                )
                return False
            if result.status == "down":
                probe_note = (
                    f" · [yellow]probe unavailable"
                    f"{': ' + result.detail if result.detail else ''}[/yellow]"
                )

        try:
            await asyncio.to_thread(apply_card_values, card, values)
        except Exception as exc:
            self._safe_transcript_error(f"{card.label}: save failed: {exc}")
            return False

        if make_active:
            from flowly.integrations.active_provider import set_active_provider
            try:
                model_changed = await asyncio.to_thread(set_active_provider, card.key)
                reload_msg = await self._reload_gateway_provider()
            except Exception as exc:
                self._safe_transcript_error(
                    f"{card.label}: default switch failed: {exc}"
                )
                return False
            self._refresh_active_provider_status()
            model_note = f" · model → [b]{model_changed}[/b]" if model_changed else ""
            self._safe_transcript_system(
                f"✓ {card.label} key saved · default provider → [b]{card.label}[/b]"
                f"{model_note} · {reload_msg}{probe_note}"
            )
        else:
            self._safe_transcript_system(f"✓ {card.label} key saved{probe_note}")
        return True

    async def _configure_card_inline(self, card: IntegrationCard) -> bool:
        """Configure a non-provider integration in the composer tray."""
        if not card.fields:
            self._safe_transcript_system(
                f"{card.label} has no editable fields."
            )
            return False

        try:
            from flowly.integrations import apply_card_values, read_card_values
            current = await asyncio.to_thread(read_card_values, card)
        except Exception:
            current = {}

        fields = [
            _inline_setup_field(field, current.get(field.key, field.default))
            for field in card.fields
        ]
        values = await self._show_inline_setup(
            InlineSetupPromptRequest(
                title=f"Configure {card.label}",
                subtitle=card.description,
                fields=fields,
            )
        )
        if values is None:
            self._safe_transcript_system(f"{card.label} setup cancelled")
            return False

        try:
            await asyncio.to_thread(apply_card_values, card, values)
        except Exception as exc:
            self._safe_transcript_error(f"{card.label}: save failed: {exc}")
            return False

        if card.category == "provider":
            tail = await self._reload_gateway_provider()
        elif card.needs_gateway_restart:
            tail = await self._restart_gateway_after_setup()
        else:
            tail = "takes effect on next request"
        self._safe_transcript_system(
            f"✓ {card.label} configuration saved · {tail}"
        )
        return True

    async def _restart_gateway_after_setup(self) -> str:
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

    async def _install_mcp_with_inline_secrets(
        self,
        name: str,
        fields: list[Any],
    ) -> None:
        env_values: dict[str, str] = {}
        for field in fields:
            value = await self._show_inline_secret(
                InlineSecretPromptRequest(
                    title=f"Configure MCP: {name}",
                    label=str(getattr(field, "prompt", "") or getattr(field, "name", "")),
                    placeholder=str(getattr(field, "name", "")),
                    value=str(getattr(field, "default", "") or ""),
                    required=True,
                    password=bool(getattr(field, "secret", True)),
                    help="Stored in ~/.flowly/.env; config.json keeps a ${VAR} reference.",
                )
            )
            if value is None:
                self._safe_transcript_system(f"{name} MCP install cancelled")
                return
            env_values[str(getattr(field, "name", ""))] = value

        from flowly.integrations.mcp_io import install_catalog_server
        try:
            ok, msg = await asyncio.to_thread(
                install_catalog_server,
                name,
                env_values,
            )
        except Exception as exc:
            self._safe_transcript_error(f"{name}: MCP install failed: {exc}")
            return
        if not ok:
            self._safe_transcript_error(f"{name}: {msg}")
            return
        tail = await self._restart_gateway_after_setup()
        self._safe_transcript_system(f"✓ {msg} · {tail}")

    @work
    async def action_provider(self, rest: str) -> None:
        """``/provider`` (no arg) opens the picker. ``/provider <key>``
        switches directly without a UI roundtrip (scriptable). ``off``
        clears the explicit default and resumes the cascade.

        After any switch we POST to ``/api/provider/reload`` so the
        running gateway swaps its LLM client in place — no manual
        restart needed.
        """
        from flowly.config.loader import load_config
        from flowly.integrations import get_card
        from flowly.integrations.active_provider import (
            _build_for,
            set_active_provider,
        )

        transcript = self.query_one(TranscriptPane)
        rest = (rest or "").strip()

        # No-arg → open the arrow-key picker.
        if not rest:
            result = await self._show_composer_picker(ProviderPickerPanel(), inline=True)
            if not result:
                return
            if result.get("action") == "switched":
                self._refresh_active_provider_status()
                transcript.add_system(
                    f"✓ default provider → [b]{result['key']}[/b]"
                )
            elif result.get("action") == "needs_login":
                # Enter on a not-yet-connected subscription-style provider →
                # start the dedicated setup flow directly (no detail form).
                if result.get("key") == "openai_codex":
                    self.action_codex_login()
                elif result.get("key") == "zai_coding":
                    await self.action_zai_coding_login()
                else:
                    self.action_xai_login()
            elif result.get("action") == "login":
                # Flowly account picked while signed out → browser sign-in
                # (LoginModal auto-provisions the key), not a paste form.
                await self.action_login()
            elif result.get("action") == "inline_setup":
                card = get_card(result["key"])
                if card is not None:
                    await self._configure_provider_inline(card, make_active=True)
            elif result.get("action") == "opened_setup":
                from flowly.tui.panes.integration_setup_modal import (
                    IntegrationSetupModal,
                )
                card = get_card(result["key"])
                if card is not None and card.custom_action == "xai_login":
                    # Browser OAuth instead of a pasted-credentials form.
                    # action_xai_login is @work → fire it (don't await the
                    # Worker); it drives the browser flow on its own and
                    # reports back to the transcript.
                    self.action_xai_login()
                elif card is not None and card.custom_action == "codex_login":
                    self.action_codex_login()
                elif card is not None and card.custom_action == "zai_coding_login":
                    await self.action_zai_coding_login()
                elif card is not None:
                    saved = await self._show_inline_screen(IntegrationSetupModal(card))
                    if saved and saved.get("action") == "saved":
                        self._refresh_active_provider_status()
            elif result.get("action") == "disconnect":
                if result.get("key") == "openai_codex":
                    await self.action_codex_logout()
                elif result.get("key") == "zai_coding":
                    await self.action_zai_coding_logout()
                else:
                    await self.action_xai_logout()
            return

        # Direct switch / off — keeps the slash usable as a one-liner.
        if rest.lower() in ("off", "clear", "none", "auto"):
            try:
                await asyncio.to_thread(set_active_provider, "")
                await self._reload_gateway_provider()
            except Exception as exc:
                transcript.add_error(f"/provider: {exc}")
                return
            self._refresh_active_provider_status()
            transcript.add_system("✓ explicit default cleared — cascade resumes")
            return

        cfg = load_config()
        target = get_card(rest)
        if target is None or target.category != "provider":
            transcript.add_error(
                f"unknown provider '{rest}' — try `/provider` to list"
            )
            return
        if _build_for(cfg, rest) is None:
            if target.custom_action == "xai_login":
                # Not signed in yet — drive the browser OAuth flow directly.
                # @work method → fire-and-forget (Worker isn't awaitable).
                self.action_xai_login()
                return
            if target.custom_action == "codex_login":
                self.action_codex_login()
                return
            if target.custom_action == "zai_coding_login":
                await self.action_zai_coding_login()
                return
            if _inline_provider_key_field(target) is not None:
                await self._configure_provider_inline(target, make_active=True)
                return
            transcript.add_error(
                f"'{rest}' is not configured — open `/provider` to set it up first"
            )
            return
        try:
            await asyncio.to_thread(set_active_provider, rest)
            reload_msg = await self._reload_gateway_provider()
        except Exception as exc:
            transcript.add_error(f"/provider: {exc}")
            return
        self._refresh_active_provider_status()
        transcript.add_system(
            f"✓ default provider → [b]{target.label}[/b] · {reload_msg}"
        )

    async def action_zai_coding_login(self) -> None:
        """Connect a Z.AI GLM Coding Plan key from the TUI.

        Reuses an existing Flowly/OpenCode/env credential when available;
        otherwise prompts for a plan key in the composer tray and stores it in
        Flowly's credential store.
        """
        from flowly.auth import zai_coding
        from flowly.config.loader import load_config, save_config
        from flowly.integrations.active_provider import set_active_provider

        transcript = self.query_one(TranscriptPane)

        def _enable_slot() -> None:
            cfg = load_config()
            cfg.providers.zai_coding.enabled = True
            cfg.providers.zai_coding.api_base = zai_coding.DEFAULT_ZAI_CODING_BASE_URL
            save_config(cfg)

        payload = await asyncio.to_thread(zai_coding.load_token_payload)
        if payload is not None and payload.api_key:
            await asyncio.to_thread(_enable_slot)
            changed = await asyncio.to_thread(set_active_provider, "zai_coding")
            tail = await self._reload_gateway_provider()
            self._refresh_active_provider_status()
            note = f" · model → [b]{changed}[/b]" if changed else ""
            source = payload.source or "stored"
            if payload.source == "opencode" and payload.provider_id:
                source = f"OpenCode ({payload.provider_id})"
            transcript.add_system(
                f"✓ Z.AI GLM Coding Plan already connected via {source} — "
                f"set as default{note} · {tail}"
            )
            return

        value = await self._show_inline_secret(
            InlineSecretPromptRequest(
                title="Configure Z.AI GLM Coding Plan",
                label="GLM Coding Plan API key",
                placeholder="Paste your Z.AI coding-plan key",
                help=(
                    "Saved to Flowly's credential store. If OpenCode already "
                    "has a Z.AI key, Flowly reuses it automatically."
                ),
                required=True,
                password=True,
            )
        )
        if value is None:
            transcript.add_system("Z.AI GLM Coding Plan setup cancelled")
            return
        api_key = value.strip()
        if not api_key:
            transcript.add_error("Z.AI GLM Coding Plan: API key is required")
            return

        try:
            backend = await asyncio.to_thread(zai_coding.save_api_key, api_key)
            await asyncio.to_thread(_enable_slot)
            changed = await asyncio.to_thread(set_active_provider, "zai_coding")
        except Exception as exc:
            transcript.add_error(f"Z.AI GLM Coding Plan setup failed: {exc}")
            return

        tail = await self._reload_gateway_provider()
        self._refresh_active_provider_status()
        note = f" · model → [b]{changed}[/b]" if changed else ""
        transcript.add_system(
            f"✓ Z.AI GLM Coding Plan key saved · storage [dim]{backend}[/] "
            f"· default provider → [b]Z.AI GLM Coding Plan[/b]{note} · {tail}"
        )

    async def action_zai_coding_logout(self) -> None:
        """Remove Flowly's stored GLM Coding key.

        OpenCode credentials are read-only fallbacks and are left untouched.
        """
        from flowly.auth import zai_coding

        transcript = self.query_one(TranscriptPane)
        payload = await asyncio.to_thread(zai_coding.load_token_payload)
        if payload is None:
            transcript.add_system("Z.AI GLM Coding Plan is not connected")
            return
        await asyncio.to_thread(zai_coding.clear_token_payload)
        try:
            from flowly.integrations.active_provider import clear_active_if_matches
            await asyncio.to_thread(clear_active_if_matches, "zai_coding")
        except Exception:
            pass
        tail = await self._reload_gateway_provider()
        self._refresh_active_provider_status()
        if payload.source != "flowly":
            transcript.add_system(
                "✓ Flowly GLM Coding key cleared · external OpenCode/env key "
                f"left untouched · {tail}"
            )
        else:
            transcript.add_system(f"✓ Z.AI GLM Coding Plan key removed · {tail}")

    @work(exclusive=True, group="xai_login")
    async def action_xai_login(self) -> None:
        """Browser OAuth login for an xAI Grok subscription, from the TUI.

        Mirrors ``flowly xai login`` without leaving the terminal UI:
        prints the authorize URL into the transcript and opens the browser
        with the same detached spawner the account login uses (avoids
        Textual's fd-inheritance crash on ``webbrowser.open``). The
        blocking loopback wait runs in a worker thread so the UI stays
        responsive, then we activate the provider and hot-reload the
        gateway in place.
        """
        from flowly.auth import xai_oauth
        from flowly.config.loader import load_config, save_config
        from flowly.integrations.active_provider import set_active_provider
        from flowly.tui.panes.login_modal import _open_browser_detached

        transcript = self.query_one(TranscriptPane)
        cfg = load_config()

        # Already have a token → just make it the active provider; don't
        # force the user through the browser dance again.
        if xai_oauth.load_token_payload() is not None:
            await asyncio.to_thread(set_active_provider, "xai_oauth")
            tail = await self._reload_gateway_provider()
            self._refresh_active_provider_status()
            transcript.add_system(
                f"✓ xAI Grok OAuth already connected — set as default · {tail}"
            )
            return

        try:
            client_id = xai_oauth.require_client_id(cfg)
        except xai_oauth.XAIClientIDMissingError as exc:
            transcript.add_error(str(exc))
            return

        transcript.add_system(
            "Starting xAI Grok sign-in… approve in your browser, then return here."
        )

        def _on_url(url: str) -> None:
            # Runs in the worker thread once the authorize URL is built.
            opened = _open_browser_detached(url)
            try:
                from flowly.tui.osc52 import copy_to_clipboard
                copy_to_clipboard(url)
            except Exception:
                pass
            lead = (
                "Opened your browser. If it didn't open, use the full URL below:"
                if opened else "Open the full URL below to sign in:"
            )
            self.call_from_thread(
                transcript.add_system,
                f"{lead}\n{url}",
                collapse_long=False,
            )

        try:
            payload = await asyncio.to_thread(
                xai_oauth.login_with_loopback,
                client_id=client_id,
                no_browser=True,          # we open the browser ourselves (detached)
                timeout_seconds=300,
                on_authorize_url=_on_url,
            )
        except xai_oauth.XAIEntitlementError as exc:
            transcript.add_error(f"Authenticated, but not entitled: {exc}")
            return
        except Exception as exc:
            transcript.add_error(f"xAI sign-in failed: {exc}")
            return

        backend = await asyncio.to_thread(xai_oauth.save_token_payload, payload)
        await asyncio.to_thread(set_active_provider, "xai_oauth")

        # Promote a Grok default model if the current one isn't xAI-shaped,
        # so the very next turn actually hits Grok.
        try:
            from flowly.providers.xai_responses_provider import (
                DEFAULT_XAI_RESPONSES_MODEL,
            )
            fresh = load_config()
            cur = (fresh.agents.defaults.model or "").strip()
            if "/" in cur or not cur.lower().startswith("grok"):
                fresh.agents.defaults.model = DEFAULT_XAI_RESPONSES_MODEL
                await asyncio.to_thread(save_config, fresh)
        except Exception:
            pass

        tail = await self._reload_gateway_provider()
        self._refresh_active_provider_status()
        acct = f" ([cyan]{payload.email}[/])" if payload.email else ""
        transcript.add_system(
            f"✓ xAI Grok OAuth connected{acct} · storage [dim]{backend}[/] · {tail}"
        )

    async def action_xai_logout(self) -> None:
        """Disconnect the stored xAI Grok OAuth token from the TUI.

        Mirrors ``flowly xai logout``: clears the token, drops the
        explicit default if it pointed at xai_oauth (so the gateway falls
        back to the cascade instead of refusing to boot), and hot-reloads.
        """
        from flowly.auth import xai_oauth

        transcript = self.query_one(TranscriptPane)
        payload = xai_oauth.load_token_payload()
        if payload is None:
            transcript.add_system("xAI Grok OAuth is not connected")
            return
        await asyncio.to_thread(xai_oauth.clear_token_payload)
        try:
            from flowly.integrations.active_provider import clear_active_if_matches
            clear_active_if_matches("xai_oauth")
        except Exception:
            pass
        tail = await self._reload_gateway_provider()
        self._refresh_active_provider_status()
        acct = f" ([dim]{payload.email}[/])" if payload.email else ""
        transcript.add_system(f"✓ xAI Grok OAuth signed out{acct} · {tail}")

    @work(exclusive=True, group="codex_login")
    async def action_codex_login(self) -> None:
        """Browser OAuth login for a ChatGPT subscription, from the TUI.

        Mirrors ``flowly codex login``: prints the authorize URL into the
        transcript, opens the browser with the detached spawner (avoids
        Textual's fd-inheritance crash on ``webbrowser.open``), runs the
        blocking loopback wait in a worker thread, then activates the
        provider and hot-reloads the gateway in place.
        """
        from flowly.auth import openai_codex
        from flowly.integrations.active_provider import set_active_provider
        from flowly.tui.panes.login_modal import _open_browser_detached

        transcript = self.query_one(TranscriptPane)

        # Already have a token (Flowly store OR ~/.codex/auth.json) → just make
        # it the active provider; don't force the browser dance again.
        if openai_codex.load_token_payload() is not None:
            changed = await asyncio.to_thread(set_active_provider, "openai_codex")
            tail = await self._reload_gateway_provider()
            self._refresh_active_provider_status()
            note = f" · model → [b]{changed}[/b]" if changed else ""
            transcript.add_system(
                f"✓ ChatGPT subscription already connected — set as default{note} · {tail}"
            )
            return

        client_id = openai_codex.require_client_id()
        transcript.add_system(
            "Starting ChatGPT sign-in… approve in your browser, then return here."
        )

        def _on_url(url: str) -> None:
            opened = _open_browser_detached(url)
            try:
                from flowly.tui.osc52 import copy_to_clipboard
                copy_to_clipboard(url)
            except Exception:
                pass
            lead = (
                "Opened your browser. If it didn't open, use the full URL below:"
                if opened else "Open the full URL below to sign in:"
            )
            self.call_from_thread(
                transcript.add_system, f"{lead}\n{url}", collapse_long=False
            )

        try:
            payload = await asyncio.to_thread(
                openai_codex.login_with_loopback,
                client_id=client_id,
                no_browser=True,          # we open the browser ourselves (detached)
                timeout_seconds=300,
                on_authorize_url=_on_url,
            )
        except openai_codex.CodexEntitlementError as exc:
            transcript.add_error(f"Authenticated, but this plan can't use Codex: {exc}")
            return
        except Exception as exc:
            transcript.add_error(f"ChatGPT sign-in failed: {exc}")
            return

        changed = await asyncio.to_thread(set_active_provider, "openai_codex")
        tail = await self._reload_gateway_provider()
        self._refresh_active_provider_status()
        acct = f" ([cyan]{payload.email}[/])" if payload.email else ""
        note = f" · model → [b]{changed}[/b]" if changed else ""
        transcript.add_system(
            f"✓ ChatGPT subscription connected{acct}{note} · {tail}"
        )

    async def action_codex_logout(self) -> None:
        """Disconnect the stored ChatGPT subscription token from the TUI.

        Clears Flowly's own token store, drops the explicit default if it
        pointed at openai_codex (so the gateway falls back to the cascade),
        and hot-reloads. A ``codex login`` session in ~/.codex/auth.json is
        left untouched.
        """
        from flowly.auth import openai_codex

        transcript = self.query_one(TranscriptPane)
        payload = openai_codex.load_token_payload()
        if payload is None:
            transcript.add_system("ChatGPT subscription is not connected")
            return
        await asyncio.to_thread(openai_codex.clear_token_payload)
        try:
            from flowly.integrations.active_provider import clear_active_if_matches
            clear_active_if_matches("openai_codex")
        except Exception:
            pass
        tail = await self._reload_gateway_provider()
        self._refresh_active_provider_status()
        acct = f" ([dim]{payload.email}[/])" if payload.email else ""
        transcript.add_system(f"✓ ChatGPT subscription signed out{acct} · {tail}")

    @work
    async def action_plugins(self) -> None:
        """Open the plugins modal — list bundled + user plugins, toggle
        enabled state, surface manifest errors. Mirrors desktop's plugin
        tab; gateway restart is auto-triggered after every toggle so
        the tool registry picks up the change without a manual reload."""
        from flowly.tui.panes.plugins_modal import PluginsModal
        result = await self._show_inline_screen(PluginsModal())
        transcript = self.query_one(TranscriptPane)
        if result and result.get("action") == "changed":
            n = int(result.get("count") or 0)
            transcript.add_system(
                f"✓ {n} plugin{'s' if n != 1 else ''} toggled · "
                f"gateway restarted"
            )

    @work
    async def action_mcp(self) -> None:
        """Open the MCP modal — list configured MCP servers + catalog
        entries, toggle enable/disable, install no-secret catalog servers,
        remove servers. MCP tools register at agent boot, so the gateway
        is auto-restarted after each change (same as plugins)."""
        from flowly.tui.panes.mcp_modal import MCPModal
        result = await self._show_inline_screen(MCPModal())
        transcript = self.query_one(TranscriptPane)
        if result and result.get("action") == "changed":
            n = int(result.get("count") or 0)
            transcript.add_system(
                f"✓ {n} MCP change{'s' if n != 1 else ''} applied · "
                f"gateway restarted"
            )
        elif result and result.get("action") == "install_secret":
            await self._install_mcp_with_inline_secrets(
                str(result.get("name") or ""),
                list(result.get("fields") or []),
            )

    @work
    async def action_browser(self) -> None:
        """Open the Browser Use modal — toggle ``browser_tab`` enable
        flag, see live extension-connection status, and open the Chrome
        Web Store if the extension isn't installed yet."""
        from flowly.tui.panes.browser_modal import BrowserModal
        result = await self._show_inline_screen(BrowserModal())
        transcript = self.query_one(TranscriptPane)
        if result and result.get("action") == "saved":
            state = "enabled" if result.get("enabled") else "disabled"
            transcript.add_system(
                f"✓ browser_tab [b]{state}[/b] · gateway restarted"
            )

    @work
    async def action_model(self, rest: str) -> None:
        """Open the model picker for the currently active provider, or
        switch directly via ``/model <id>``. The picker loads the
        provider's catalog (OpenRouter for the MVP) and lets the user
        filter + select."""
        from flowly.config.loader import load_config
        from flowly.integrations import get_card
        from flowly.integrations.active_provider import resolve_active_provider

        transcript = self.query_one(TranscriptPane)
        cfg = load_config()
        active = resolve_active_provider(cfg)
        if active is None:
            transcript.add_error(
                "no active LLM provider — run `/provider` first"
            )
            return

        # Direct switch path — write + reload, no picker.
        rest = (rest or "").strip()
        if rest:
            from flowly.tui.panes.model_picker import _set_default_model
            try:
                await asyncio.to_thread(_set_default_model, rest)
                tail = await self._reload_gateway_provider()
            except Exception as exc:
                transcript.add_error(f"/model: {exc}")
                return
            transcript.add_system(f"✓ model → [b]{rest}[/b] · {tail}")
            return

        # Picker path.
        card = get_card(active.key)
        result = await self._show_composer_picker(
            ModelPickerPanel(
                provider_key=active.key,
                provider_label=card.label if card else active.key,
                current_model=cfg.agents.defaults.model or "",
                docs_url=card.docs_url if card else "",
            ),
            inline=True,
        )
        if result and result.get("action") == "switched":
            transcript.add_system(f"✓ model → [b]{result['model']}[/b]")

    @work
    async def action_theme(self, rest: str) -> None:
        """Switch TUI color theme via picker or direct ``/theme <name>``."""
        from flowly.tui.panes.theme_picker import ThemePicker

        transcript = self.query_one(TranscriptPane)
        name = (rest or "").strip()

        if not name:
            original_theme = self._theme_name
            result = await self._show_inline_screen(ThemePicker(self._theme_name))
            if not result:
                self._apply_theme(original_theme, persist=False)
                return
            name = result

        palette = get_theme(name)
        if palette is None:
            available = ", ".join(theme.name for theme in list_themes())
            transcript.add_error(
                f"unknown theme '{name}' — available: {available}"
            )
            return

        self._apply_theme(palette.name, persist=True)
        transcript.add_system(
            f"✓ theme → [b]{palette.label}[/b] [dim]({palette.name})[/dim]"
        )

    def preview_theme(self, theme_name: str) -> None:
        """Temporarily apply a theme while the picker highlight moves."""
        self._apply_theme(theme_name, persist=False, preview=True)

    def _apply_theme(
        self,
        theme_name: str,
        *,
        persist: bool = False,
        preview: bool = False,
    ) -> None:
        palette = set_active_theme(theme_name)
        if not preview:
            self._theme_name = palette.name
        self._palette = palette
        self.stylesheet.add_source(
            css_for(palette),
            read_from=("flowly-tui", "runtime-theme"),
            tie_breaker=100,
        )
        self.refresh_css(animate=False)
        if persist:
            state = load_state()
            state["theme"] = palette.name
            save_state(state)

    async def _reload_gateway_provider(self) -> str:
        """Shared helper: POST to /api/provider/reload, return a short
        status string. Also pushes the newly-active model into the
        StatusBar so the user sees the change without restarting."""
        from flowly.tui.gateway_reload import post_provider_reload
        try:
            r = await post_provider_reload(timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                new_model = str(data.get("model") or "")
                if new_model:
                    try:
                        self.query_one(StatusBar).model = new_model
                    except Exception:
                        pass
                self._refresh_active_provider_status()
                return f"gateway → {data.get('source') or data.get('key') or '?'}"
            if r.status_code == 422:
                err = (r.json() or {}).get("error", "no usable provider")
                return f"[yellow]reload rejected: {err}[/yellow]"
            return f"[yellow]reload HTTP {r.status_code}[/yellow]"
        except Exception:
            return "[dim]gateway offline — restart to apply[/dim]"

    async def action_whoami(self) -> None:
        import time

        from flowly.account.auth import (
            credential_storage_status,
            load_account_refreshing,
        )
        transcript = self.query_one(TranscriptPane)
        account = await load_account_refreshing()
        if account is None:
            transcript.add_system("not signed in — run [b]/login[/b] to connect")
            return
        secs_left = max(0, int(account.expires_at - time.time()))
        fresh = (
            f"fresh ({secs_left // 60} min)" if secs_left > 600
            else f"expires in {secs_left}s" if secs_left > 0
            else "expired"
        )
        server_line = (
            f"- server:  `{account.server_id}` ({account.server_name})"
            if account.server_id
            else "- server:  [yellow]not registered[/yellow] — will retry on next /login"
        )
        # Surface whether the gateway is wired for relay sync, plus a
        # one-line scope note so the user is never confused about where
        # this CLI session's messages persist (always local) vs where
        # their iOS/desktop/Android chats persist (relay → Firestore).
        try:
            from flowly.config.loader import load_config
            web_cfg = load_config().channels.web
            if web_cfg.enabled and web_cfg.server_id == account.server_id:
                relay_line = (
                    f"- relay:   [green]wired[/green] · {web_cfg.relay_url}"
                )
            elif web_cfg.enabled:
                relay_line = (
                    f"- relay:   [yellow]wired to different server[/yellow] "
                    f"`{web_cfg.server_id}` — re-run /login to re-wire"
                )
            else:
                relay_line = "- relay:   [yellow]disabled[/yellow] (web channel off)"
        except Exception:
            relay_line = "- relay:   [dim]config not readable[/dim]"
        scope_line = (
            "- sync:    iOS/desktop/Android chats → cloud (via relay)\n"
            "           this CLI session → [b]local only[/b]"
        )
        transcript.add_assistant(
            f"**Signed in as:** `{account.email or account.user_id}`\n\n"
            f"- user_id: `{account.user_id}`\n"
            f"- device:  `{account.machine_name}`\n"
            f"- machine: `{account.machine_id}`\n"
            f"{server_line}\n"
            f"{relay_line}\n"
            f"{scope_line}\n"
            f"- token:   {fresh}\n"
            f"- storage: {credential_storage_status()}"
        )

    @work
    async def action_open_usage(self) -> None:
        """Open the /usage screen — this machine's own token & cost tally for
        the active provider (any provider, local or remote gateway)."""
        from flowly.tui.panes.status import _model_budget

        s = self.query_one(StatusBar)
        provider, _src = self._active_provider_display()
        ctx_used = int(s.tokens_in) + int(s.tokens_out)
        ctx_budget = _model_budget(s.model or "")
        composer = self.query_one(Composer)
        account = self._account
        email = (
            (getattr(account, "email", None) or getattr(account, "user_id", None))
            if account else None
        )

        def _show(credits: dict | None) -> None:
            composer.show_usage(
                totals=dict(self._usage_totals),
                model=s.model or "",
                provider=provider or "",
                ctx_used=ctx_used,
                ctx_budget=ctx_budget or 0,
                elapsed=time.monotonic() - self._session_started,
                account_email=email,
                credits=credits,
            )

        # Show instantly with local token/cost; the input row is replaced by the
        # inline panel (no modal overlay). Then, if signed in, fill in the same
        # live credit balance Desktop shows (best-effort — never blocks the open).
        _show(None)
        if account is not None:
            try:
                from flowly.account.billing import fetch_account_credits
                credits = await fetch_account_credits(account)
            except Exception:
                credits = None
            if credits is not None and composer.has_class("usage-open"):
                _show(credits)

    @work
    async def action_open_activity(self) -> None:
        transcript = self.query_one(TranscriptPane)
        try:
            entries = await self._client.audit_list(limit=100)
            stats = await self._client.audit_stats()
        except Exception as exc:
            transcript.add_error(f"audit fetch failed: {exc}")
            return
        await self._show_inline_screen(ActivityModal(entries, stats))

    @work
    async def action_open_approvals(self) -> None:
        transcript = self.query_one(TranscriptPane)
        try:
            pending = await self._client.approval_list()
        except Exception as exc:
            transcript.add_error(f"approvals fetch failed: {exc}")
            return
        result = await self._show_inline_screen(ApprovalsModal(pending))
        if not result:
            return
        aid = result.get("id", "")
        decision = result.get("decision", "deny")
        try:
            await self._client.approval_resolve(
                aid, decision, remember=(decision == "allow-always")
            )
            transcript.add_system(f"approval {aid[:8]} → {decision}")
        except Exception as exc:
            transcript.add_error(f"approval.resolve failed: {exc}")
        await self._poll_badges()

    @work
    async def action_open_policy(self) -> None:
        """Edit command permissions (security/ask/allowlist).

        The modal stays open and applies each change live through this
        callback; it closes on Esc / Close.
        """
        transcript = self.query_one(TranscriptPane)
        try:
            policy = await self._client.exec_policy_get()
        except Exception as exc:
            transcript.add_error(f"permissions fetch failed: {exc}")
            return

        async def apply(action: dict):
            try:
                if action.get("action") == "set":
                    updated = await self._client.exec_policy_set(
                        security=action.get("security"),
                        ask=action.get("ask"),
                    )
                    transcript.add_system(
                        f"permissions → security={updated.get('security')} "
                        f"ask={updated.get('ask')}"
                    )
                    return updated
                if action.get("action") == "remove":
                    pattern = action.get("pattern", "")
                    updated = await self._client.exec_policy_allowlist_remove(pattern)
                    transcript.add_system(f"allowlist − {pattern}")
                    return updated
            except Exception as exc:
                transcript.add_error(f"permissions update failed: {exc}")
            return None

        await self._show_inline_screen(PolicyModal(policy, apply))
        await self._poll_badges()

    async def action_cycle_permission(self) -> None:
        """Cycle the standing permission level (Ask → Auto → YOLO) and apply it
        LIVE to both the exec tool and the codex_session runtime over RPC.

        Bound to Shift+Tab (app-level). The change takes effect on the next
        command / codex turn with no gateway restart; the current level shows as
        the colored badge at the left of the status bar. A slow poll
        (_sync_permission_badge) keeps that badge in sync when the mode is
        changed elsewhere — the Desktop app or another client.
        """
        idx = getattr(self, "_perm_level_idx", None)
        if idx is None or idx < 0:
            # Sync the starting point from the live policy so the first press
            # advances from where we actually are (or from -1 → Ask).
            try:
                idx = _match_permission_level(await self._client.exec_policy_get())
            except Exception:
                idx = -1
        idx = (idx + 1) % len(_PERMISSION_LEVELS)

        key, _label, (security, ask), (approval, sandbox) = _PERMISSION_LEVELS[idx]
        # Mute the sync poll while we apply, so it can't read a half-written
        # store and clobber the badge mid-cycle.
        self._perm_cycling = True
        try:
            await self._client.exec_policy_set(security=security, ask=ask)
            await self._client.codex_policy_set(
                approval_policy=approval, sandbox=sandbox
            )
        except Exception as exc:
            self.query_one(TranscriptPane).add_error(f"permission cycle failed: {exc}")
            return
        finally:
            self._perm_cycling = False

        self._perm_level_idx = idx
        self._set_permission_badge(key)

    def _set_permission_badge(self, level_key: str) -> None:
        """Reflect the standing permission level in the status-bar badge."""
        try:
            self.query_one(StatusBar).permission = level_key
        except Exception:
            pass

    async def _sync_permission_badge(self) -> None:
        """Reflect the live exec policy in the badge — on mount AND on a slow
        poll, so a mode changed elsewhere (the Desktop app, another client)
        shows up here without a restart. Skips while a manual cycle is applying
        so it can't clobber it. A policy matching no preset hides the badge."""
        if getattr(self, "_perm_cycling", False):
            return
        try:
            idx = _match_permission_level(await self._client.exec_policy_get())
        except Exception:
            return
        self._perm_level_idx = idx
        self._set_permission_badge(_PERMISSION_LEVELS[idx][0] if idx >= 0 else "")

    def action_copy_last(self) -> None:
        """Copy the most recent assistant message to the system clipboard
        via OSC 52 — works through tmux/SSH/iTerm/kitty/etc.
        """
        from flowly.tui.osc52 import copy_to_clipboard
        transcript = self.query_one(TranscriptPane)
        # Walk children in reverse looking for the last assistant Bubble.
        last_text: str | None = None
        for child in reversed(list(transcript.children)):
            if isinstance(child, Bubble) and getattr(child, "_role", "") == "assistant":
                last_text = child._text
                break
        if not last_text:
            self.query_one(TranscriptPane).add_system("nothing to copy yet")
            return
        copy_to_clipboard(last_text)
        self.query_one(TranscriptPane).add_system(
            f"📋 copied last assistant reply ({len(last_text):,} chars)"
        )

    @work
    async def action_open_artifacts(self) -> None:
        transcript = self.query_one(TranscriptPane)
        try:
            arts = await self._client.artifacts_list(limit=200)
        except Exception as exc:
            transcript.add_error(f"artifacts fetch failed: {exc}")
            return
        await self._show_inline_screen(ArtifactsModal(arts))

    @work
    async def action_open_assistants(self) -> None:
        transcript = self.query_one(TranscriptPane)
        try:
            assistants = await self._client.assistants_list()
        except Exception as exc:
            transcript.add_error(f"assistants.list failed: {exc}")
            return
        if not assistants:
            transcript.add_system(
                "no assistants registered (registry not wired or empty)"
            )
            return
        picked = await self._show_inline_screen(AssistantPicker(assistants))
        if not picked:
            return
        name = picked["name"]
        new_key = f"tui:{name}"
        await self._switch_session(new_key)
        status = self.query_one(StatusBar)
        if picked.get("model"):
            status.model = picked["model"]
        transcript.add_system(
            f"switched to assistant [b]{name}[/b]"
            + (f" · model {picked['model']}" if picked.get("model") else "")
        )

    @work
    async def action_open_sessions(self) -> None:
        transcript = self.query_one(TranscriptPane)
        try:
            sessions = await self._client.sessions_list(limit=50)
        except Exception as exc:
            transcript.add_error(f"sessions.list failed: {exc}")
            return
        if not sessions:
            transcript.add_system("no saved sessions")
            return
        result = await self._show_inline_screen(SessionPicker(sessions, self._session_key))
        if not result:
            return
        action = result.get("action")
        key = str(result.get("sessionKey", ""))
        if not key:
            return
        if action == "switch":
            await self._switch_session(key)
        elif action == "delete":
            try:
                ok = await self._client.session_delete(key)
            except Exception as exc:
                transcript.add_error(f"sessions.delete failed: {exc}")
                return
            transcript.add_system(
                f"deleted {key}" if ok else f"could not delete {key}"
            )
            if key == self._session_key:
                # current session was wiped; start fresh
                for child in list(transcript.children):
                    child.remove()
                transcript.add_system(f"new empty session · {self._session_key}")


def _flatten_content(content: Any) -> str:
    """chat.history returns content as a list of typed parts; collapse to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            out.append(part.get("text", ""))
        elif part.get("type") in ("image_url", "input_image"):
            out.append("[image]")
    return "".join(out)
