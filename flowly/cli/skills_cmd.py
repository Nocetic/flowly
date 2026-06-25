"""CLI commands — skills_cmd."""

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
# Skills Commands (shortcuts to flowly-hub)
# ============================================================================

skills_app = typer.Typer(help="Manage skills (alias for flowly-hub)")


@skills_app.command("list")
def skills_list(
    all_skills: bool = typer.Option(False, "--all", "-a", help="Include workspace skills"),
):
    """List installed skills."""
    from flowly.hub.manager import SkillManager
    from flowly.utils.helpers import get_workspace_path

    workspace = get_workspace_path()
    with SkillManager(workspace_dir=workspace) as manager:
        skills = manager.list_installed(include_workspace=all_skills)

    if not skills:
        console.print("[yellow]No skills installed[/yellow]")
        console.print("\n[dim]Install skills with: flowly skills install <skill-name>[/dim]")
        return

    table = Table(title="Installed Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Version")
    table.add_column("Source", style="dim")

    for skill in skills:
        source_short = skill.source[:30] + "..." if len(skill.source) > 30 else skill.source
        table.add_row(skill.slug, skill.version, source_short)

    console.print(table)


@skills_app.command("install")
def skills_install(
    source: str = typer.Argument(..., help="Skill source"),
    force: bool = typer.Option(False, "--force", "-f", help="Force reinstall"),
):
    """Install a skill."""
    from flowly.hub.manager import SkillManager
    from flowly.utils.helpers import get_workspace_path

    workspace = get_workspace_path()
    with SkillManager(workspace_dir=workspace) as manager:
        console.print(f"[cyan]Installing {source}...[/cyan]")
        skill = manager.install(source, force=force)

        if skill:
            console.print(f"[green]✓[/green] Installed [cyan]{skill.name}[/cyan] v{skill.version}")
        else:
            console.print(f"[red]✗[/red] Failed to install {source}")
            raise typer.Exit(1)


@skills_app.command("remove")
def skills_remove(
    skill: str = typer.Argument(..., help="Skill to remove"),
):
    """Remove an installed skill."""
    from flowly.hub.manager import SkillManager
    from flowly.utils.helpers import get_workspace_path

    workspace = get_workspace_path()
    with SkillManager(workspace_dir=workspace) as manager:
        if manager.remove(skill):
            console.print(f"[green]✓[/green] Removed [cyan]{skill}[/cyan]")
        else:
            console.print(f"[red]✗[/red] Skill {skill} not found")
            raise typer.Exit(1)


@skills_app.command("search")
def skills_search(
    query: str = typer.Argument(..., help="Search query"),
):
    """Search for skills in the registry."""
    from flowly.hub.manager import SkillManager
    from flowly.utils.helpers import get_workspace_path

    workspace = get_workspace_path()
    with SkillManager(workspace_dir=workspace) as manager:
        results = manager.search(query)

    if not results:
        console.print(f"[yellow]No skills found for '{query}'[/yellow]")
        return

    table = Table(title=f"Skills matching '{query}'")
    table.add_column("Name", style="cyan")
    table.add_column("Description")

    for skill in results[:10]:
        desc = skill.description[:50] + "..." if len(skill.description) > 50 else skill.description
        table.add_row(skill.slug, desc)

    console.print(table)


