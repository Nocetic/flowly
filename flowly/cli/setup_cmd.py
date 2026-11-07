"""CLI commands — setup_cmd."""

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
# Setup Commands
# ============================================================================

setup_app = typer.Typer(
    help="Configure provider, channels, and tools — no gateway required.",
    no_args_is_help=False,
)


def _print_deprecation(old: str, suggestion: str) -> None:
    console.print(
        f"[yellow]⚠ `flowly setup {old}` is deprecated[/] — "
        "will be removed in a future release."
    )
    console.print(f"  Use: [cyan]{suggestion}[/]")
    console.print()


@setup_app.callback(invoke_without_command=True)
def setup_main(ctx: typer.Context):
    """Configure flowly — the one-stop first-run setup.

    Bare ``flowly setup`` seeds the workspace and opens the unified picker:
    sign in with a Flowly account (managed) or bring your own API key (BYOK),
    then offers to start the gateway. It runs gateway-free, so it works on a
    fresh machine before any gateway or provider exists. Channels and tool
    integrations are optional follow-ups:

        flowly setup           — first-run: account or API key (default)
        flowly setup channels  — Telegram / Discord / Slack
        flowly setup tools     — browser, voice, Trello, etc.
        flowly setup byok <p>  — quick BYOK one-shot (no UI)

    Non-interactive contexts (Core run by Flowly Desktop, piped installers) seed the workspace and skip the prompt — never hanging.
    """
    if ctx.invoked_subcommand is not None:
        return

    from flowly.cli.onboard_cmd import run_onboarding
    run_onboarding()


@setup_app.command("channels")
def setup_channels_cmd() -> None:
    """Connect messaging channels (Telegram / Discord / Slack)."""
    console.print("Opening the channels catalog...\n")
    from flowly.tui.setup_app import run_setup
    run_setup(target="channels")
    _print_runtime_next_steps()


@setup_app.command("tools")
def setup_tools_cmd() -> None:
    """Configure tool integrations (browser, voice, Trello, …)."""
    console.print("Opening the integrations catalog...\n")
    from flowly.tui.setup_app import run_setup
    run_setup(target="tools")
    _print_runtime_next_steps()


def _print_runtime_next_steps() -> None:
    """After setup, show how to actually run flowly.

    Setup only writes config — it never starts a gateway (that would lock
    the terminal, and a fresh user has nothing to run yet). The runtime is
    a deliberate, separate step, so spell out both ways to start it. We
    only nudge when a provider is actually configured; otherwise repeat
    the one mandatory step.
    """
    try:
        from flowly.config.loader import load_config
        from flowly.integrations.active_provider import resolve_active_provider
        active = resolve_active_provider(load_config())
    except Exception:
        active = None

    console.print()
    if active is None:
        console.print(
            "[yellow]No provider configured yet.[/] Run "
            "[cyan]flowly setup[/] and pick one, or "
            "[cyan]flowly setup byok <provider> --key <KEY>[/]."
        )
        return

    console.print(f"[green]✓[/] Provider ready: [b]{active.source}[/]")
    console.print()
    console.print("You're set:")
    console.print(
        "  [cyan]flowly[/]                          "
        "[dim]— start chatting (starts the gateway if it isn't running)[/]"
    )
    console.print()
    console.print("[dim]Optional:[/]")
    console.print(
        "  [cyan]flowly service install --start[/]  "
        "[dim](keep the gateway running in the background, across logins)[/]"
    )
    console.print(
        "  [cyan]flowly gateway[/]                  "
        "[dim](foreground — keeps this terminal busy; use a 2nd one to chat)[/]"
    )


@setup_app.command("byok")
def setup_byok_cmd(
    provider: str = typer.Argument(
        ...,
        help="Provider slug: openrouter, anthropic, openai, xai, gemini, groq, zhipu, sakana",
    ),
    api_key: str = typer.Option(
        None, "--key", "-k",
        help="API key (prompted interactively if omitted)",
    ),
    set_active: bool = typer.Option(
        True, "--set-active/--no-set-active",
        help="Also pin this provider as the active default",
    ),
) -> None:
    """Quick BYOK one-shot: save an API key and (optionally) make it default.

    Designed for power users, CI bootstrap scripts, and dotfile setups
    that want to wire a provider without launching the TUI. The richer
    UX (live probe, switching, multi-provider catalog) lives in
    ``flowly setup`` / `/integrations`.
    """
    provider_slug = provider.strip().lower()
    valid = {"openrouter", "anthropic", "openai", "xai", "gemini", "groq", "zhipu", "sakana"}
    if provider_slug not in valid:
        console.print(
            f"[red]Unknown provider:[/] {provider_slug}\n"
            f"  Supported: {', '.join(sorted(valid))}"
        )
        raise typer.Exit(code=2)

    if not api_key:
        from rich.prompt import Prompt
        api_key = Prompt.ask(f"Enter {provider_slug} API key", password=True)
    api_key = (api_key or "").strip()
    if not api_key:
        console.print("[red]No API key entered.[/]")
        raise typer.Exit(code=2)

    try:
        from flowly.config.loader import load_config, save_config
        from flowly.integrations.active_provider import set_active_provider
        cfg = load_config()
        slot = getattr(cfg.providers, provider_slug, None)
        if slot is None:
            console.print(f"[red]Provider slot '{provider_slug}' missing from config schema.[/]")
            raise typer.Exit(code=2)
        slot.api_key = api_key
        save_config(cfg)
        if set_active:
            set_active_provider(provider_slug)
        console.print(f"  [green]✓[/] {provider_slug} key saved")
        if set_active:
            console.print(f"  [green]✓[/] Active provider → [b]{provider_slug}[/]")
        console.print("\n  Run [cyan]flowly[/] to start chatting.")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ Failed to save:[/] {exc}")
        raise typer.Exit(code=1)


@setup_app.command("ollama")
def setup_ollama_cmd(
    model: str = typer.Option(
        "", "--model", "-m",
        help="Ollama model id to use, e.g. gemma4, qwen3.6, or minimax-m2.5:cloud.",
    ),
    api_base: str = typer.Option(
        "http://127.0.0.1:11434/v1", "--api-base",
        help="Ollama OpenAI-compatible base URL.",
    ),
    set_active: bool = typer.Option(
        True, "--set-active/--no-set-active",
        help="Pin Ollama as the active default provider.",
    ),
) -> None:
    """Configure Flowly to use local Ollama via its OpenAI-compatible API."""
    api_base = (api_base or "").strip().rstrip("/") or "http://127.0.0.1:11434/v1"
    model = (model or "").strip()

    try:
        from flowly.config.loader import load_config, save_config
        from flowly.integrations.active_provider import set_active_provider
        from flowly.integrations import model_catalog

        cfg = load_config()
        cfg.providers.ollama.api_base = api_base
        if model:
            cfg.agents.defaults.model = model
        save_config(cfg)
        if set_active:
            set_active_provider("ollama")
        model_catalog.flush_cache()

        if not model:
            visible = asyncio.run(_list_ollama_models(api_base))
            if len(visible) == 1:
                cfg = load_config()
                cfg.agents.defaults.model = visible[0]
                save_config(cfg)
                model = visible[0]
            elif visible and sys.stdin.isatty():
                from InquirerPy import inquirer
                picked = inquirer.select(
                    message="Pick an Ollama model:",
                    choices=visible,
                    default=visible[0],
                ).execute()
                if picked:
                    cfg = load_config()
                    cfg.agents.defaults.model = str(picked)
                    save_config(cfg)
                    model = str(picked)

        console.print(f"  [green]✓[/] Ollama endpoint → [b]{api_base}[/]")
        if model:
            console.print(f"  [green]✓[/] Model → [b]{model}[/]")
        else:
            console.print(
                "  [yellow]![/] No model selected; set one with "
                "[cyan]/model <id>[/] or "
                "[cyan]flowly setup ollama --model <id>[/]."
            )
        if set_active:
            console.print("  [green]✓[/] Active provider → [b]ollama[/]")
        console.print("\n  Run [cyan]flowly[/] to start chatting.")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ Failed to configure Ollama:[/] {exc}")
        raise typer.Exit(code=1)


async def _list_ollama_models(api_base: str) -> list[str]:
    """Best-effort model discovery for ``flowly setup ollama``."""
    return await _list_openai_compatible_models(api_base, api_key="ollama")


async def _list_openai_compatible_models(api_base: str, *, api_key: str) -> list[str]:
    """Best-effort model discovery for any OpenAI-compatible ``/models`` route
    (Ollama, LM Studio, vLLM, llama.cpp server, text-generation-webui, …)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(
                f"{api_base.rstrip('/')}/models",
                headers={
                    "Authorization": f"Bearer {api_key or 'local'}",
                    "User-Agent": "flowly-setup/local",
                },
            )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    out: list[str] = []
    for item in data.get("data", []):
        mid = str(item.get("id") or item.get("model") or "").strip()
        if mid:
            out.append(mid)
    return sorted(set(out))


@setup_app.command("local")
def setup_local_cmd(
    api_base: str = typer.Option(
        "http://127.0.0.1:1234/v1", "--api-base",
        help="Base URL of any local OpenAI-compatible server — LM Studio "
             "(default port 1234), vLLM (8000), llama.cpp server, "
             "text-generation-webui, koboldcpp, etc.",
    ),
    model: str = typer.Option(
        "", "--model", "-m",
        help="Model id to use, e.g. qwen3-8b or meta-llama/Llama-3.1-8B-Instruct. "
             "Omit to pick from /v1/models.",
    ),
    api_key: str = typer.Option(
        "", "--api-key",
        help="Optional bearer token some local servers check. Most (LM Studio, "
             "Ollama, llama.cpp) ignore it; leave unset unless yours requires one.",
    ),
    set_active: bool = typer.Option(
        True, "--set-active/--no-set-active",
        help="Pin this endpoint as the active default provider.",
    ),
) -> None:
    """Configure Flowly to use a local OpenAI-compatible server (LM Studio,
    vLLM, llama.cpp server, text-generation-webui, ...).

    Distinct from ``flowly setup ollama``: this targets ANY server that speaks
    the OpenAI ``/v1/chat/completions`` + ``/v1/models`` wire protocol, not
    just Ollama's. Use ``flowly setup ollama`` if you're specifically running
    Ollama — it has its own default port and needs no ``--api-base``.
    """
    api_base = (api_base or "").strip().rstrip("/") or "http://127.0.0.1:1234/v1"
    model = (model or "").strip()
    api_key = (api_key or "").strip()

    try:
        from flowly.config.loader import load_config, save_config
        from flowly.integrations.active_provider import set_active_provider
        from flowly.integrations import model_catalog

        cfg = load_config()
        cfg.providers.vllm.api_base = api_base
        # Only write the key when one was actually passed — re-running
        # `setup local` to change just the URL/model must not wipe a key the
        # user set earlier (e.g. via the TUI card). Empty is the common case
        # (local servers ignore it) and the resolver supplies a dummy bearer.
        if api_key:
            cfg.providers.vllm.api_key = api_key
        if model:
            cfg.agents.defaults.model = model
        save_config(cfg)
        if set_active:
            set_active_provider("vllm")
        model_catalog.flush_cache()

        if not model:
            visible = asyncio.run(
                _list_openai_compatible_models(api_base, api_key=api_key)
            )
            if len(visible) == 1:
                cfg = load_config()
                cfg.agents.defaults.model = visible[0]
                save_config(cfg)
                model = visible[0]
            elif visible and sys.stdin.isatty():
                from InquirerPy import inquirer
                picked = inquirer.select(
                    message="Pick a model:",
                    choices=visible,
                    default=visible[0],
                ).execute()
                if picked:
                    cfg = load_config()
                    cfg.agents.defaults.model = str(picked)
                    save_config(cfg)
                    model = str(picked)

        console.print(f"  [green]✓[/] Local endpoint → [b]{api_base}[/]")
        if model:
            console.print(f"  [green]✓[/] Model → [b]{model}[/]")
        else:
            console.print(
                "  [yellow]![/] No model selected; set one with "
                "[cyan]/model <id>[/] or "
                "[cyan]flowly setup local --model <id>[/]."
            )
        if set_active:
            console.print("  [green]✓[/] Active provider → [b]Local OpenAI-compatible[/]")
        console.print("\n  Run [cyan]flowly[/] to start chatting.")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ Failed to configure local endpoint:[/] {exc}")
        raise typer.Exit(code=1)


# ── Deprecated section wizards (kept one release for backward compat) ──
#
# These predate `/integrations`. Each one duplicates a card that the TUI
# now configures with live probes + validation + hot-reload. They keep
# working so old tutorials don't break, but emit a deprecation hint.


@setup_app.command("telegram", hidden=True)
def setup_telegram_cmd():
    """[Deprecated] Set up Telegram bot."""
    _print_deprecation("telegram", "flowly  (then F5 / /integrations → Telegram)")
    from flowly.cli.setup import setup_telegram
    setup_telegram()


@setup_app.command("voice", hidden=True)
def setup_voice_cmd():
    """[Deprecated] Set up voice transcription (Groq Whisper)."""
    _print_deprecation("voice", "flowly  (then F5 / /integrations → Voice)")
    from flowly.cli.setup import setup_voice
    setup_voice()


@setup_app.command("voice-calls", hidden=True)
def setup_voice_calls_cmd():
    """[Deprecated] Set up voice calls (Twilio)."""
    _print_deprecation("voice-calls", "flowly  (then F5 / /integrations → Voice calls)")
    from flowly.cli.setup import setup_voice_calls
    setup_voice_calls()


@setup_app.command("openrouter", hidden=True)
def setup_openrouter_cmd():
    """[Deprecated] Set up OpenRouter LLM provider."""
    _print_deprecation("openrouter", "flowly setup byok openrouter  (one-shot CLI)")
    from flowly.cli.setup import setup_openrouter
    setup_openrouter()


@setup_app.command("trello", hidden=True)
def setup_trello_cmd():
    """[Deprecated] Set up Trello integration."""
    _print_deprecation("trello", "flowly  (then F5 / /integrations → Trello)")
    from flowly.cli.setup import setup_trello
    setup_trello()


@setup_app.command("discord", hidden=True)
def setup_discord_cmd():
    """[Deprecated] Set up Discord bot."""
    _print_deprecation("discord", "flowly  (then F5 / /integrations → Discord)")
    from flowly.cli.setup import setup_discord
    setup_discord()


@setup_app.command("slack", hidden=True)
def setup_slack_cmd():
    """[Deprecated] Set up Slack bot."""
    _print_deprecation("slack", "flowly  (then F5 / /integrations → Slack)")
    from flowly.cli.setup import setup_slack
    setup_slack()


# ── Subcommands that genuinely belong on the CLI (kept verbatim) ───────


@setup_app.command("agents")
def setup_agents_cmd():
    """Set up multi-agent orchestration."""
    from flowly.cli.setup import setup_agents
    setup_agents()


@setup_app.command("google-workspace")
def setup_google_workspace_cmd():
    """Install and authenticate the Google Workspace CLI (gws)."""
    from flowly.cli.setup import setup_google_workspace
    setup_google_workspace()
