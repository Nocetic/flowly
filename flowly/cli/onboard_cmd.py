"""CLI commands — onboard_cmd."""

import asyncio
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from flowly import __version__, __logo__

console = Console()

# ============================================================================
# Onboard / Setup
# ============================================================================


def onboard():
    """Configure flowly — alias for ``flowly setup``.

    Runs the same unified first-run flow: seed the workspace, then choose a
    Flowly account or your own API key, and offer to start the gateway.
    Persona switching lives in the TUI (``/assistants`` / Ctrl+M).
    """
    run_onboarding()




def _builtin_workspace_dir() -> Path:
    """Locate the package's bundled ``workspace/`` templates.

    Shipped INTO the wheel at ``flowly/workspace`` (pyproject force-include); in
    a source / editable checkout the same files live at the repo root. Return
    the first that exists so the seed works for pip/uv installs AND source.
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "workspace",          # flowly/workspace (installed wheel)
        here.parent.parent.parent / "workspace",   # <repo>/workspace (source / editable)
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def _create_workspace_templates(workspace: Path):
    """Seed the workspace with every template the package ships.

    Copies ALL top-level ``*.md`` files from the package's ``workspace/``
    directory (AGENTS, SOUL, USER, TOOLS, HEARTBEAT, …) plus ``memory/MEMORY.md``
    — a directory scan, not a hardcoded list, so a template added to the package
    is seeded automatically and nothing the agent reads as context goes missing
    (``context.py`` expects AGENTS/SOUL/USER/TOOLS/IDENTITY). Existing files are
    never overwritten, so this is safe to re-run.
    """
    builtin_workspace = _builtin_workspace_dir()

    # Minimal fallbacks — used ONLY when the package didn't ship that file.
    fallbacks: dict[str, str] = {
        "AGENTS.md": (
            "# Agent Instructions\n\n"
            "You are a helpful AI assistant. Be concise, accurate, and friendly.\n\n"
            "## Guidelines\n\n"
            "- Always explain what you're doing before taking actions\n"
            "- Ask for clarification when the request is ambiguous\n"
            "- Use tools to help accomplish tasks\n"
            "- Remember important information in your memory files\n"
        ),
        "SOUL.md": "",
        "USER.md": "",
    }

    seeded: set[str] = set()

    # 1. Copy every top-level template the package ships.
    if builtin_workspace.is_dir():
        for src in sorted(builtin_workspace.glob("*.md")):
            seeded.add(src.name)
            dst = workspace / src.name
            if dst.exists():
                continue
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            console.print(f"  [dim]Created {src.name}[/dim]")

    # 2. Ensure the core files exist even if the package shipped none of them.
    for filename, content in fallbacks.items():
        if filename in seeded:
            continue
        dst = workspace / filename
        if dst.exists():
            continue
        dst.write_text(content, encoding="utf-8")
        console.print(f"  [dim]Created {filename}[/dim]")

    # 3. memory/MEMORY.md — prefer the package copy, fall back to a default.
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        src_mem = builtin_workspace / "memory" / "MEMORY.md"
        if src_mem.exists():
            memory_file.write_text(src_mem.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            memory_file.write_text(
                "# Long-term Memory\n\n"
                "This file stores important information that should persist across sessions.\n\n"
                "## User Information\n\n(Important facts about the user)\n\n"
                "## Preferences\n\n(User preferences learned over time)\n\n"
                "## Important Notes\n\n(Things to remember)\n",
                encoding="utf-8",
            )
        console.print("  [dim]Created memory/MEMORY.md[/dim]")


# ============================================================================
# Unified first-run onboarding
# ============================================================================
#
# One interactive flow that does everything a fresh install needs: seed the
# workspace, then let the user pick how to power Flowly — sign in with a Flowly
# account (managed, recommended) or bring their own API key (BYOK) — modelled on
# a single picker where the managed option sits at the top of the same list as
# the BYOK providers. Then offer to start the gateway.
#
# Nuitka / Flowly Desktop safety: the binary is spawned by Electron as a
# non-TTY subprocess. The interactive menu is gated on ``sys.stdin.isatty()`` so
# it NEVER prompts (and never hangs) there — it just seeds the workspace and
# returns. InquirerPy / prompt_toolkit are imported lazily INSIDE the TTY branch,
# so the non-interactive path never touches them either.

# BYOK providers offered in the picker: (slug, label, where-to-get-a-key hint).
_BYOK_PROVIDERS: list[tuple[str, str, str]] = [
    ("openrouter", "OpenRouter", "one key, many models · openrouter.ai/keys"),
    ("anthropic", "Anthropic (Claude)", "console.anthropic.com"),
    ("openai", "OpenAI", "platform.openai.com/api-keys"),
    ("gemini", "Google Gemini", "aistudio.google.com/apikey"),
    ("groq", "Groq", "console.groq.com/keys"),
    ("xai", "xAI (Grok) · API key", "console.x.ai"),
    ("zhipu", "Zhipu / GLM", "open.bigmodel.cn"),
    ("sakana", "Sakana Fugu", "console.sakana.ai"),
]


def seed_workspace() -> Path:
    """Create the workspace + template/persona files (idempotent, non-interactive).

    Pure file I/O — safe to call from any context (compiled binary included).
    Returns the workspace path.
    """
    from flowly.utils.helpers import get_workspace_path

    workspace = get_workspace_path()
    workspace.mkdir(parents=True, exist_ok=True)
    _create_workspace_templates(workspace)
    _install_persona_files(workspace)
    return workspace


def _already_configured() -> bool:
    """True when a provider is set up OR a Flowly account is signed in."""
    try:
        from flowly.config.loader import load_config
        from flowly.integrations.active_provider import resolve_active_provider

        if resolve_active_provider(load_config()) is not None:
            return True
    except Exception:
        pass
    try:
        from flowly.account.health import check_token_state

        if check_token_state().has_account:
            return True
    except Exception:
        pass
    return False


def _select_with_back(message: str, choices: list, default=None):
    """An InquirerPy select where Esc / ← (left) / Ctrl-C all mean "go back".

    Returns the chosen value, or ``None`` when the user backs out. ``skip`` is
    bound to Escape + Left so the left arrow walks back up the menu stack;
    ``mandatory=False`` makes a skipped prompt resolve to ``None`` instead of
    blocking. Lazy-imports InquirerPy so the non-interactive path never loads
    prompt_toolkit.
    """
    from InquirerPy import inquirer

    try:
        return inquirer.select(
            message=message,
            choices=choices,
            default=default,
            pointer="›",
            mandatory=False,
            keybindings={"skip": [{"key": "escape"}, {"key": "left"}]},
        ).execute()
    except KeyboardInterrupt:
        return None


def _onboarding_menu() -> str | None:
    """Provider picker — the full list inline.

    Returns ``"flowly"`` (managed sign-in), ``"xai_oauth"`` (Grok subscription
    browser flow), a BYOK provider slug, or ``None`` (backed out / cancelled).
    """
    from InquirerPy.base.control import Choice
    from InquirerPy.separator import Separator

    choices = [
        Choice(value="flowly", name="Sign in with Flowly      (recommended — hosted, nothing to configure)"),
        Separator("  ── or bring your own ──"),
    ]
    for slug, label, hint in _BYOK_PROVIDERS:
        choices.append(Choice(value=slug, name=f"{label}  ·  {hint}"))
        # xAI Grok OAuth is the user's own xAI subscription, not a Flowly-hosted
        # provider — so it sits in the BYOK list, right under the xAI API key.
        if slug == "xai":
            choices.append(Choice(
                value="xai_oauth",
                name="xAI Grok subscription  ·  SuperGrok / X Premium+ (opens browser)",
            ))
    choices.append(Separator())
    choices.append(Choice(value="back", name="← Back"))

    val = _select_with_back("How do you want to power Flowly?", choices, default="flowly")
    return None if val in (None, "back") else val


def _run_managed_login() -> None:
    """Run the standard Flowly account sign-in (OAuth) — reuses `flowly login`."""
    from flowly.cli.login_cmd import login

    # Call the command function with explicit values so Typer's OptionInfo
    # defaults are bypassed (calling it bare would pass OptionInfo objects).
    # ``login`` signals completion/abort with ``raise typer.Exit(...)`` — Typer's
    # own runner swallows that, but we're calling it as a plain function, so we
    # catch it here instead of letting it surface as an uncaught traceback.
    try:
        login(no_browser=False, repair=False, dry_run=False, key="", relay_opt=None)
    except (typer.Exit, SystemExit):
        pass
    except KeyboardInterrupt:
        console.print("  [dim]Sign-in cancelled.[/dim]")


def _run_xai_oauth_login() -> None:
    """Run the xAI Grok subscription OAuth (opens the browser) — reuses `flowly xai login`."""
    from flowly.cli.xai_cmd import login as xai_login

    # Same OptionInfo-bypass + Exit-catch pattern as the managed sign-in: we call
    # the Typer command as a plain function, so swallow its raise typer.Exit.
    try:
        xai_login(no_browser=False, manual_paste=False, set_active=True, timeout_seconds=300)
    except (typer.Exit, SystemExit):
        pass
    except KeyboardInterrupt:
        console.print("  [dim]xAI sign-in cancelled.[/dim]")


def _prompt_byok_key(slug: str) -> bool:
    """Prompt for a provider API key, persist it, make it active. True if saved."""
    from rich.prompt import Prompt

    from flowly.config.loader import load_config, save_config
    from flowly.integrations.active_provider import set_active_provider

    label = next((lbl for s, lbl, _ in _BYOK_PROVIDERS if s == slug), slug)
    key = Prompt.ask(f"  Paste your {label} API key", password=True).strip()
    if not key:
        console.print("  [yellow]No key entered — skipped.[/yellow]")
        return False
    cfg = load_config()
    provider_slot = getattr(cfg.providers, slug, None)
    if provider_slot is None:
        console.print(f"  [red]Provider slot '{slug}' missing from config.[/red]")
        return False
    provider_slot.api_key = key
    save_config(cfg)
    set_active_provider(slug)
    console.print(f"  [green]✓[/green] {label} key saved · active provider → [b]{slug}[/b]")
    return True


def _prompt_model(slug: str) -> None:
    """Let the user pick a model for ``slug`` from its catalog (or keep default).

    Uses the models.dev-backed catalog (no key needed to list), shown as a
    type-to-filter list. Skippable: "Keep default" / Esc / Ctrl-C leaves the
    provider's default model in place.
    """
    import asyncio

    from flowly.integrations.model_catalog import fetch_models

    console.print(f"  [dim]Fetching {slug} models…[/dim]")
    try:
        models = asyncio.run(fetch_models(slug))
    except Exception:
        models = []
    if not models:
        console.print("  [dim]No model catalog for this provider — using its default.[/dim]")
        return

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=None, name="Keep default")]
    choices += [Choice(value=m.id, name=_model_label(m)) for m in models]
    try:
        picked = inquirer.fuzzy(
            message="Pick a model (type to filter):",
            choices=choices,
            default="",
        ).execute()
    except KeyboardInterrupt:
        return
    if not picked:
        return

    from flowly.config.loader import load_config, save_config

    cfg = load_config()
    cfg.agents.defaults.model = picked
    save_config(cfg)
    console.print(f"  [green]✓[/green] Model → [b]{picked}[/b]")


def _model_label(m) -> str:
    """A compact one-line label for a catalog model row."""
    bits = [m.name or m.id]
    if getattr(m, "context_window", None):
        bits.append(f"{m.context_window // 1000}k ctx")
    tags = getattr(m, "tags", None) or []
    if tags:
        bits.append(" · ".join(tags[:2]))
    return "   ".join(bits)


def _offer_start_gateway() -> None:
    """Ask whether to start the gateway in the background, then guide next steps."""
    from rich.prompt import Confirm

    try:
        start_now = Confirm.ask("\n  Start Flowly in the background now?", default=True)
    except (EOFError, KeyboardInterrupt):
        start_now = False

    if start_now:
        argv0 = sys.argv[0] or "flowly"
        try:
            subprocess.run([argv0, "service", "install", "--start"], check=False)
            console.print("\n  [green]✓[/green] Done — run [cyan]flowly[/cyan] to start chatting.")
            return
        except Exception:
            console.print("  [yellow]Couldn't auto-start — start it manually:[/yellow]")
    console.print(
        "\n  Next:\n"
        "    [cyan]flowly service install --start[/cyan]   [dim]— run the gateway in the background[/dim]\n"
        "    [cyan]flowly[/cyan]                            [dim]— start chatting[/dim]"
    )


def _print_banner() -> None:
    """Render the FLOWLY wordmark + tagline, matching the TUI welcome screen."""
    from rich.align import Align
    from rich.console import Group
    from rich.text import Text

    from flowly import __version__
    from flowly.tui.panes.welcome import LOGO_ART, LOGO_GRADIENT, LOGO_SMALL

    # Turquoise brand gradient (the wordmark's 3 colour bands).
    grad = ["#19d3e6", "#00a6c8", "#0b7c97"]
    console.print()
    if console.width < 56:
        console.print(Align.center(Text(LOGO_SMALL, style="bold #00a6c8")))
    else:
        rows = [Text(art, style=f"bold {grad[LOGO_GRADIENT[i]]}") for i, art in enumerate(LOGO_ART)]
        console.print(Align.center(Group(*rows)))
    console.print(
        Align.center(
            Text("Your personal AI agent — terminal · desktop · phone, in sync.", style="dim")
        )
    )
    console.print(Align.center(Text(f"v{__version__}", style="dim #0b7c97")))
    console.print()


def _run_provider_step() -> bool:
    """Show the provider picker and handle the choice. True if now configured.

    Returns False when the user backs out (← / Esc) so callers can return to the
    setup home instead of leaving onboarding.
    """
    choice = _onboarding_menu()
    if choice is None:
        return False  # backed out — caller returns to the home
    if choice == "flowly":
        _run_managed_login()
    elif choice == "xai_oauth":
        _run_xai_oauth_login()
    else:
        if not _prompt_byok_key(choice):
            return False
    # ``choice`` is already the provider key ("flowly" / "xai_oauth" / a BYOK
    # slug) — offer a model from its catalog once the provider is usable. Flowly
    # hosted serves a plan-filtered list via the proxy; xAI / BYOK via /v1/models
    # or the models.dev catalogue.
    if _already_configured():
        _prompt_model(choice)
        return True
    return False


def _configure_channels() -> None:
    """Set up messaging channels inline (no Textual)."""
    from flowly.cli.inline_cards import configure_section_inline

    configure_section_inline("channel", "Channels", console)


def _configure_tools() -> None:
    """Set up service integrations inline (no Textual)."""
    from flowly.cli.inline_cards import configure_section_inline

    configure_section_inline("tool", "Integrations", console)


def _configure_media() -> None:
    """Set up media-generation providers (image gen) inline."""
    from flowly.cli.inline_cards import configure_section_inline

    configure_section_inline("media", "Media generation", console)


def _show_summary() -> None:
    """Print the read-only setup recap panel."""
    from flowly.cli.setup_summary import collect_summary, render_summary
    from flowly.config.loader import load_config

    render_summary(collect_summary(load_config()))


def _status_line() -> None:
    """One dim line above the home menu: what's set up right now."""
    try:
        from flowly.cli.setup_summary import collect_summary
        from flowly.config.loader import load_config

        s = collect_summary(load_config())
    except Exception:
        return
    gw = "running" if s.gateway_running else ("installed" if s.gateway_installed else "off")
    console.print(
        f"  [dim]Provider[/dim] {s.provider_key or '—'}   "
        f"[dim]Gateway[/dim] {gw}   "
        f"[dim]Channels[/dim] {len(s.configured_channels)}   "
        f"[dim]Tools[/dim] {len(s.configured_tools)}\n"
    )


def _setup_home_menu() -> str | None:
    """The setup home: a guided mode or a jump to one section. None = quit."""
    from InquirerPy.base.control import Choice
    from InquirerPy.separator import Separator

    _status_line()
    choices = [
        Choice(value="quick", name="Quick   ·  pick a provider and start chatting"),
        Choice(value="full", name="Full    ·  provider → channels → integrations → media"),
        Choice(value="blank", name="Blank   ·  just a provider, nothing else"),
        Separator(),
        Choice(value="provider", name="Configure  ·  provider"),
        Choice(value="channels", name="Configure  ·  channels"),
        Choice(value="tools", name="Configure  ·  integrations"),
        Choice(value="media", name="Configure  ·  media generation"),
        Choice(value="summary", name="Show summary"),
        Choice(value="quit", name="Done / quit"),
    ]
    # Esc / ← / Ctrl-C at the home = quit (returns None).
    return _select_with_back("Set up Flowly — choose a path:", choices, default="quick")


def _flow_quick() -> bool:
    """Provider, then the summary — straight to chatting.

    Returns True when it ran to completion, False if the user backed out of the
    provider step (so the caller re-shows the home instead of leaving).
    """
    if not _run_provider_step():
        return False
    _show_summary()
    _offer_start_gateway()
    return True


def _flow_full() -> bool:
    """Provider → channels → integrations → media → summary, all inline."""
    if not _run_provider_step():
        return False
    _configure_channels()
    _configure_tools()
    _configure_media()
    _show_summary()
    _offer_start_gateway()
    return True


def _flow_blank() -> bool:
    """Provider only — no gateway offer, nothing extra."""
    if not _run_provider_step():
        return False
    _show_summary()
    return True


def _run_setup_home() -> None:
    """Drive the setup home. A completed mode exits; backing out of a mode
    returns to the home. Esc / ← / 'Done' quit. Stays fully inline — onboarding
    never launches the Textual setup screens."""
    modes = {"quick": _flow_quick, "full": _flow_full, "blank": _flow_blank}
    while True:
        action = _setup_home_menu()
        if action is None or action == "quit":
            if _already_configured():
                _show_summary()
            else:
                console.print("  [dim]Run [cyan]flowly setup[/cyan] when ready.[/dim]")
            return
        if action in modes:
            if modes[action]():
                return  # completed → leave onboarding
            continue  # backed out → re-show the home
        if action == "provider":
            _run_provider_step()
        elif action == "channels":
            _configure_channels()
        elif action == "tools":
            _configure_tools()
        elif action == "media":
            _configure_media()
        elif action == "summary":
            _show_summary()


def run_onboarding() -> None:
    """Unified first-run onboarding. Safe in any context (see module note).

    Seeds the workspace always; only prompts when stdin is a real TTY.
    """
    workspace = seed_workspace()

    if _already_configured():
        # Workspace refreshed; provider/account already set — nothing to ask.
        console.print(f"[green]✓[/green] Workspace ready at {workspace}")
        return

    if not sys.stdin.isatty():
        # Non-interactive (Electron subprocess / piped installer): never prompt.
        console.print(f"[green]✓[/green] Workspace ready at {workspace}")
        console.print(
            "Flowly isn't configured yet. In a terminal run "
            "[cyan]flowly setup[/cyan] to choose an account or API key."
        )
        return

    _print_banner()
    _run_setup_home()


def _install_persona_files(workspace: Path):
    """Copy builtin persona files to workspace/personas/ directory."""
    personas_dir = workspace / "personas"
    personas_dir.mkdir(exist_ok=True)

    # Builtin personas are shipped in the package's workspace/personas/ directory
    builtin_dir = _builtin_workspace_dir() / "personas"
    if builtin_dir.exists():
        for src in builtin_dir.glob("*.md"):
            dst = personas_dir / src.name
            if not dst.exists():
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                console.print(f"  [dim]Created personas/{src.name}[/dim]")
    else:
        # Fallback: create a minimal default persona
        default_file = personas_dir / "default.md"
        if not default_file.exists():
            default_file.write_text(
                "# Persona: Flowly\n\n"
                "You are Flowly, a helpful AI assistant.\n\n"
                "## Personality\n\n"
                "- Helpful and friendly\n"
                "- Concise and to the point\n"
                "- Curious and eager to learn\n",
                encoding="utf-8",
            )
            console.print("  [dim]Created personas/default.md[/dim]")


