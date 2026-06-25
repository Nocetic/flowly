"""CLI commands — persona_cmd."""

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
# Persona Commands
# ============================================================================

persona_app = typer.Typer(help="Manage bot persona")

BUILTIN_PERSONAS = ["default", "jarvis", "friday", "pirate", "samurai", "casual", "professor", "butler"]


def _get_personas_dir() -> Path:
    """Get the personas directory from workspace config."""
    from flowly.config.loader import load_config
    config = load_config()
    return config.workspace_path / "personas"


def _ensure_personas(workspace: Path) -> Path:
    """Ensure personas directory exists, copying builtins if needed."""
    personas_dir = workspace / "personas"
    if not personas_dir.exists() or not any(personas_dir.glob("*.md")):
        from flowly.cli.onboard_cmd import _install_persona_files
        _install_persona_files(workspace)
    return personas_dir


@persona_app.command("list")
def persona_list():
    """List available personas."""
    from flowly.config.loader import load_config
    config = load_config()
    personas_dir = _ensure_personas(config.workspace_path)
    active = config.agents.defaults.persona

    if not any(personas_dir.glob("*.md")):
        console.print("[yellow]No persona files found.[/yellow]")
        raise typer.Exit(1)

    table = Table(title="Available Personas")
    table.add_column("Name", style="cyan")
    table.add_column("Active", justify="center")
    table.add_column("Description", style="dim")

    for md_file in sorted(personas_dir.glob("*.md")):
        name = md_file.stem
        is_active = "[green]✓[/green]" if name == active else ""
        # Read first non-header line as description
        desc = ""
        for line in md_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                desc = line[:60]
                break
        table.add_row(name, is_active, desc)

    console.print(table)


@persona_app.command("set")
def persona_set(
    name: str = typer.Argument(help="Persona name to activate"),
):
    """Set the active persona."""
    from flowly.config.loader import load_config, save_config
    config = load_config()
    personas_dir = config.workspace_path / "personas"
    persona_file = personas_dir / f"{name}.md"

    if not persona_file.exists():
        console.print(f"[red]Persona not found: {name}[/red]")
        available = [f.stem for f in personas_dir.glob("*.md")] if personas_dir.exists() else BUILTIN_PERSONAS
        console.print(f"[dim]Available: {', '.join(available)}[/dim]")
        raise typer.Exit(1)

    config.agents.defaults.persona = name
    save_config(config)
    console.print(f"[green]✓[/green] Persona set to: [cyan]{name}[/cyan]")

    # Auto-restart if gateway is running
    ok, _ = _service_health(config.gateway.port)
    if ok:
        console.print("[dim]Restarting gateway...[/dim]")
        try:
            service_restart(label=DEFAULT_SERVICE_LABEL)
        except (SystemExit, Exception):
            console.print("[yellow]Could not auto-restart. Run: flowly service restart[/yellow]")


@persona_app.command("show")
def persona_show(
    name: str = typer.Argument(help="Persona name to display"),
):
    """Show persona details."""
    from flowly.config.loader import load_config
    config = load_config()
    persona_file = config.workspace_path / "personas" / f"{name}.md"

    if not persona_file.exists():
        console.print(f"[red]Persona not found: {name}[/red]")
        raise typer.Exit(1)

    content = persona_file.read_text(encoding="utf-8")
    from rich.markdown import Markdown
    console.print(Markdown(content))


