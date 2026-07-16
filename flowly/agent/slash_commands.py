"""Central slash-command registry — the single source of truth.

Every consumer reads from ``COMMAND_REGISTRY`` instead of keeping its own list:

  * the gateway / relay ``commands.list`` RPC (via
    :func:`flowly.agent.skill_bundles.build_commands_catalogue`) returns the
    commands available to remote clients (desktop, iOS) — everything that is
    not ``cli_only``;
  * the TUI composer's autocomplete palette shows the commands available in
    the terminal — everything that is not ``gateway_only``.

One registry, each command tagged with where it applies, and clients filter
from it. Before this, the gateway catalogue (``BUILTIN_SLASH_COMMANDS``) and the
TUI's hard-coded ``LOCAL_SLASH_COMMANDS`` diverged — a command added to one
silently missed the other.

Adding a command here makes it show up in the right place(s) automatically.
This registry is the *display/discovery* catalogue; execution still lives in
each surface's dispatcher (the gateway's ``chat.*`` RPC handlers, the TUI's
``_on_slash``). Keep the names in sync with those dispatchers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandDef:
    """One slash command.

    ``cli_only`` — only meaningful in the TUI/CLI (e.g. ``/theme``); hidden from
    remote clients. ``gateway_only`` — only meaningful for gateway/messaging
    clients; hidden from the TUI. Neither flag set → universal (both).
    """

    name: str
    description: str
    category: str = "General"
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    subcommands: tuple[str, ...] = ()
    cli_only: bool = False
    gateway_only: bool = False


# ---------------------------------------------------------------------------
# The registry — single source of truth
# ---------------------------------------------------------------------------

COMMAND_REGISTRY: list[CommandDef] = [
    # ── Session / context — handled by the gateway (chat.* RPCs) AND the TUI ──
    CommandDef("help", "Show available commands", "Info"),
    CommandDef("compact", "Summarize conversation to save tokens", "Session",
               args_hint="[hint]"),
    CommandDef("clear", "Clear conversation history", "Session"),
    CommandDef("new", "Start a new conversation", "Session", aliases=("reset",)),
    CommandDef("retry", "Re-submit the last user message (drops the stale reply)", "Session"),
    CommandDef("undo", "Pop the last turn (pre-fills the removed prompt)", "Session"),
    CommandDef("status", "Session health summary", "Info"),
    # Plan mode — universal (works on every surface). Bare "/plan" (or on/off)
    # toggles the STANDING mode: every task is planned + approved before any
    # side effect runs. "/plan <task>" plans just that one task.
    CommandDef("plan", "Plan mode: propose a plan and wait for approval before acting",
               "Session", args_hint="[task | on | off | status]"),
    CommandDef("usage", "Token & cost this session (+ Flowly account credits)", "Info",
               cli_only=True),
    CommandDef("whoami", "Show user / server / conversation", "Info"),
    # TUI launcher (Ctrl+S) — opens the saved-session picker. cli_only: the
    # desktop manages sessions through its own UI, not a slash command.
    CommandDef("sessions", "Switch between saved sessions", "Session", cli_only=True),
    CommandDef("skills", "List available skills", "Tools & Skills"),
    CommandDef("learn", "Create or update a reusable skill from sources you describe", "Tools & Skills",
               args_hint="[--dry-run] <source|workflow|notes>"),
    CommandDef("codex", "Codex runtime: on / off / sandbox / cwd / tools", "Tools & Skills",
               args_hint="[on|off|sandbox|cwd|tools]"),

    # ── TUI-only — terminal UI launchers / actions with no remote equivalent ──
    # (The desktop has its own buttons/pages for these, or they're terminal
    # specific. They stay out of the gateway catalogue so a remote client never
    # shows a command it can't run. Flip a flag the day a desktop handler lands
    # — e.g. /model becomes universal once the desktop model picker ships.)
    CommandDef("model", "Pick model from the active provider's catalog", "Configuration",
               cli_only=True, args_hint="[id]"),
    CommandDef("provider", "Pick the LLM provider", "Configuration", cli_only=True),
    CommandDef("integrations", "Connect external services and tools", "Configuration",
               cli_only=True),
    CommandDef("channels", "Configure messaging channels", "Configuration", cli_only=True),
    CommandDef("theme", "Switch TUI color theme", "Configuration", cli_only=True),
    # cli_only on purpose: the printed token is a secret — it must only land on
    # the local terminal, never in a Telegram/Discord chat. Desktop users do
    # the same thing from Settings → Connections instead.
    CommandDef("remote", "Enable remote desktop access — prints IP / port / token",
               "Configuration", cli_only=True, args_hint="[off|status]"),
    CommandDef("permissions", "Edit command permissions (security · ask · allowlist)",
               "Configuration", cli_only=True),
    CommandDef("image", "Attach an image path (`/image clear` removes pending)", "Tools & Skills",
               cli_only=True, args_hint="<path>"),
    CommandDef("video", "Attach a video path for video_analyze", "Tools & Skills",
               cli_only=True, args_hint="<path>"),
    CommandDef("paste", "Attach an image from the clipboard", "Tools & Skills", cli_only=True),
    CommandDef("browser", "Toggle browser_tab · install Chrome extension", "Tools & Skills",
               cli_only=True),
    # Informational nudge: computer use (desktop control) is a Flowly Desktop
    # feature — the terminal can't hold the macOS permissions it needs. cli_only
    # because the desktop already runs it natively (no point showing the nudge
    # there); a TUI-native implementation is planned separately.
    CommandDef("computer", "Computer use (desktop control) — needs the Flowly Desktop app",
               "Tools & Skills", cli_only=True),
    CommandDef("plugins", "List / enable / disable installed plugins", "Tools & Skills",
               cli_only=True),
    CommandDef("board", "Task board · view + add/run/done/cancel/del", "Tools & Skills",
               cli_only=True, args_hint="[help]"),
    CommandDef("mcp", "Manage MCP servers · install from catalog", "Tools & Skills",
               cli_only=True),
    # TUI modal launchers (Ctrl/F-key shortcuts) — handled by _on_slash but were
    # missing from the palette, so they worked when typed yet never autocompleted.
    CommandDef("assistants", "Pick a persona / assistant", "Configuration", cli_only=True),
    CommandDef("subagents", "Toggle the subagent sidebar", "Tools & Skills",
               cli_only=True, aliases=("subs",)),
    CommandDef("activity", "Activity / audit log", "Info", cli_only=True),
    CommandDef("approvals", "Pending approvals queue", "Info", cli_only=True),
    CommandDef("artifacts", "Artifacts gallery", "Tools & Skills", cli_only=True),
    CommandDef("login", "Pair this machine for iOS access", "Info", cli_only=True),
    CommandDef("logout", "Unpair (disable iOS access)", "Info", cli_only=True),
    CommandDef("abort", "Cancel the in-flight turn", "Session", cli_only=True),
    CommandDef("quit", "Exit the TUI", "Exit", cli_only=True),
]


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------

def gateway_commands() -> list[CommandDef]:
    """Commands available to remote clients (desktop, iOS) — not ``cli_only``."""
    return [c for c in COMMAND_REGISTRY if not c.cli_only]


def cli_commands() -> list[CommandDef]:
    """Commands available in the TUI/CLI — not ``gateway_only``."""
    return [c for c in COMMAND_REGISTRY if not c.gateway_only]


_LOOKUP: dict[str, CommandDef] = {}
for _c in COMMAND_REGISTRY:
    _LOOKUP[_c.name] = _c
    for _a in _c.aliases:
        _LOOKUP[_a] = _c


def resolve_command(name: str | None) -> CommandDef | None:
    """Resolve a name or alias (with or without leading slash) to its def."""
    if not name:
        return None
    return _LOOKUP.get(name.lower().lstrip("/"))
