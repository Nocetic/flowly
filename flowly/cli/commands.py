"""CLI commands for flowly — main app definition and command registration."""

import typer
from rich.console import Console

from flowly import __version__, __logo__

# NOTE: We used to force WindowsSelectorEventLoopPolicy here "for
# uvicorn/aiohttp compatibility" — that hint is outdated. Modern uvicorn
# (≥0.11) and aiohttp (≥3.7) run on Windows Proactor without issue, and
# Proactor has been the Python ≥3.8 default on Windows anyway. More
# critically: SelectorEventLoop on Windows does NOT support subprocess
# — `asyncio.create_subprocess_exec` raises `NotImplementedError` with
# no message ("Command execution error:" in logs), which silently broke
# every exec-tool invocation. Leaving the default (Proactor) restores
# subprocess support for the bundled bash without breaking the network
# stack. If a specific dependency later needs Selector, scope the
# override to that code path, not to the entire process.

app = typer.Typer(
    name="flowly",
    help=f"{__logo__} flowly - Personal AI Assistant",
    # Bare `flowly` (no args) hands off to the smart-entry callback in
    # ``main()`` — opens the TUI when a provider is configured, else
    # opens the first-run onboarding picker. Click's default
    # behaviour would print --help here and never invoke the callback.
    no_args_is_help=False,
)

console = Console()


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} flowly v{__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Gateway host"),
    port: int = typer.Option(18790, "--port", "-P", help="Gateway port"),
    session: str = typer.Option(
        "", "--session", "-s",
        help="Open a specific session key (default: start a fresh session)",
    ),
    new: bool = typer.Option(
        False, "--new", "-n",
        help="Start a fresh session (now the default; kept for compatibility)",
    ),
    resume: bool = typer.Option(
        False, "--resume", "-r", help="Resume the last-used session",
    ),
    theme: str = typer.Option(
        "", "--theme",
        help="TUI theme: flowly, moonfly, catppuccin, tokyo-night, synthwave, mono, amber, hacker",
    ),
):
    """flowly - Personal AI Assistant.

    Run with no subcommand to start chatting (the TUI) when a provider is
    configured, or open the first-run onboarding picker when none is set yet.
    The session/theme/gateway flags above apply to that bare-``flowly`` launch
    — e.g. ``flowly --resume`` or ``flowly --theme hacker``.
    """
    if ctx.invoked_subcommand is not None:
        return

    # Smart entry: provider configured → open TUI, else first-run setup.
    # Failure to read config (corrupt JSON, fresh install) shouldn't
    # crash here — fall through to the unconfigured prompt instead of
    # exploding before the user even sees a message.
    try:
        from flowly.config.loader import load_config
        from flowly.integrations.active_provider import resolve_active_provider
        cfg = load_config()
        active = resolve_active_provider(cfg)
    except Exception:
        active = None

    if active is None:
        from flowly.cli.onboard_cmd import run_onboarding

        run_onboarding()
        raise typer.Exit()

    # Materialize bundled skills into ~/.flowly/skills (manifest-tracked,
    # preserves user edits) so the TUI's agent can find and run them — builtin
    # skills otherwise live only in the package and their scripts don't resolve
    # from the workspace cwd.
    from flowly.skills.sync import ensure_synced
    ensure_synced(quiet=True)

    # Provider ready → drop straight into the TUI. Defer the import so
    # `flowly --help` etc. don't pay the Textual cost. Use ``run_tui``
    # (the plain-Python launcher), not a Typer command — it takes plain
    # values rather than OptionInfo defaults.
    from flowly.tui.entry import run_tui
    run_tui(
        host=host, port=port, session=session, new=new, resume=resume, theme=theme
    )


# ── Register command groups from sub-modules ──────────────────────

from flowly.cli.setup_cmd import setup_app
app.add_typer(setup_app, name="setup")

from flowly.cli.persona_cmd import persona_app
app.add_typer(persona_app, name="persona")

from flowly.cli.service_cmd import service_app
app.add_typer(service_app, name="service")

from flowly.cli.channels_cmd import channels_app
app.add_typer(channels_app, name="channels")

from flowly.cli.cron_cmd import cron_app
app.add_typer(cron_app, name="cron")

from flowly.cli.skills_cmd import skills_app
app.add_typer(skills_app, name="skills")

from flowly.cli.bundles_cmd import bundles_app
app.add_typer(bundles_app, name="bundles")

from flowly.cli.plugins_cmd import plugins_app
app.add_typer(plugins_app, name="plugins")

from flowly.cli.mcp_cmd import mcp_app
app.add_typer(mcp_app, name="mcp")

from flowly.cli.xai_cmd import xai_app
app.add_typer(xai_app, name="xai")

from flowly.cli.codex_cmd import codex_app
app.add_typer(codex_app, name="codex")

from flowly.cli.glm_cmd import glm_app
app.add_typer(glm_app, name="glm")

from flowly.cli.approvals_cmd import approvals_app, sessions_app
app.add_typer(approvals_app, name="approvals")
app.add_typer(sessions_app, name="sessions")

from flowly.cli.pairing_cmd import pairing_app
app.add_typer(pairing_app, name="pairing")

from flowly.cli.memory_cmd import memory_app
app.add_typer(memory_app, name="memory")

from flowly.cli.skill_gov_cmd import skill_gov_app
app.add_typer(skill_gov_app, name="skill")


# ── Standalone commands (registered directly on app) ──────────────

from flowly.cli.onboard_cmd import onboard
app.command("onboard")(onboard)

from flowly.cli.gateway_cmd import gateway
app.command("gateway")(gateway)

from flowly.cli.agent_cmd import agent
app.command("agent")(agent)

from flowly.cli.login_cmd import login
app.command("login")(login)

from flowly.cli.enroll_cmd import enroll
app.command("enroll")(enroll)

from flowly.cli.logout_cmd import logout
app.command("logout")(logout)
# /whoami still lives inside the TUI as a slash command.


@app.command()
def bootstrap():
    """Non-interactive workspace bootstrap. Safe to call from installers.

    Creates ``~/.flowly/workspace/`` and the bootstrap template files
    (SOUL.md, USER.md, AGENTS.md, memory/MEMORY.md) plus built-in
    personas. Existing files are never overwritten.

    `flowly onboard` does the same plus interactive persona selection
    and prompts — which hangs inside Electron-spawned subprocesses.
    Desktop installers should call `flowly bootstrap` instead so a
    fresh install doesn't leave the agent searching for missing files
    every turn.
    """
    from flowly.cli.onboard_cmd import _create_workspace_templates, _install_persona_files
    from flowly.utils.helpers import get_workspace_path

    workspace = get_workspace_path()
    workspace.mkdir(parents=True, exist_ok=True)
    _create_workspace_templates(workspace)
    _install_persona_files(workspace)
    console.print(f"[green]✓[/green] Workspace ready at {workspace}")


@app.command()
def restart():
    """Restart the gateway, detecting service vs foreground automatically.

    Shortcut for ``flowly service restart`` — same smart dispatch:

      * launchd/systemd/Windows-service mode → bounce the service
        atomically and wait for the port to come back up.
      * Manual ``flowly gateway`` mode → can't restart from the
        outside (the process is detached from this CLI), so we print
        a clear hint pointing the user at the terminal that owns it.
      * Not running at all → tell the user how to start it.

    Use this any time a config change wants the gateway to pick up
    fresh values (channel tokens, plugin enable/disable, etc.).
    Provider / model swaps already hot-reload via the slash commands
    and don't need a restart.
    """
    from flowly.cli.service_cmd import service_restart, DEFAULT_SERVICE_LABEL
    service_restart(label=DEFAULT_SERVICE_LABEL)


@app.command()
def update(
    check: bool = typer.Option(
        False, "--check", help="Only check whether a newer version exists; don't install."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Apply the update without confirming."
    ),
    force: bool = typer.Option(
        False, "--force", help="Reinstall the latest even if already up to date."
    ),
    no_restart: bool = typer.Option(
        False, "--no-restart", help="Don't restart the gateway after updating."
    ),
):
    """Update Flowly to the latest release.

    Install-mode aware: upgrades via uv / pipx / pip depending on how Flowly
    was installed, then bounces the gateway to load the new code. Running
    inside Flowly Desktop this is a no-op — the app manages its own binary.
    """
    from flowly.cli.update_cmd import run_update

    code = run_update(
        check_only=check, assume_yes=yes, force=force, restart=not no_restart
    )
    raise typer.Exit(code)


@app.command()
def doctor(
    fix: bool = typer.Option(False, "--fix", "-f", help="Auto-repair fixable issues"),
):
    """Check configuration and runtime health. Use --fix to auto-repair."""
    from flowly.cli.doctor import run_doctor
    raise typer.Exit(run_doctor(fix=fix))


@app.command()
def status():
    """Show flowly status."""
    from flowly.config.loader import load_config, get_config_path
    from flowly.utils.helpers import get_workspace_path

    config_path = get_config_path()
    workspace = get_workspace_path()

    console.print(f"{__logo__} Flowly Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        config = load_config()
        console.print(f"Model: {config.agents.defaults.model}")

        has_openrouter = bool(config.providers.openrouter.api_key)
        has_anthropic = bool(config.providers.anthropic.api_key)
        has_openai = bool(config.providers.openai.api_key)
        has_gemini = bool(config.providers.gemini.api_key)
        has_vllm = bool(config.providers.vllm.api_base)

        console.print(f"OpenRouter API: {'[green]✓[/green]' if has_openrouter else '[dim]not set[/dim]'}")
        console.print(f"Anthropic API: {'[green]✓[/green]' if has_anthropic else '[dim]not set[/dim]'}")
        console.print(f"OpenAI API: {'[green]✓[/green]' if has_openai else '[dim]not set[/dim]'}")
        console.print(f"Gemini API: {'[green]✓[/green]' if has_gemini else '[dim]not set[/dim]'}")
        vllm_status = f"[green]✓ {config.providers.vllm.api_base}[/green]" if has_vllm else "[dim]not set[/dim]"
        console.print(f"vLLM/Local: {vllm_status}")


if __name__ == "__main__":
    app()
