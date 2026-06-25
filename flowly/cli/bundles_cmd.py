"""CLI commands — ``flowly bundles ...``

Manage skill bundles: lightweight YAML files in
``~/.flowly/skill-bundles/`` that group a list of skills under a
single ``/slug`` slash command. Activating a bundle in chat injects
every referenced skill's body into one turn — fast way to load a
whole "mode" (research, devops, release-prep) without typing each
skill individually.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from flowly.agent import skill_bundles


console = Console()

bundles_app = typer.Typer(help="Manage skill bundles — /slug aliases for skill groups")


# --------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------- #


@bundles_app.command("list")
def bundles_list() -> None:
    """List all bundles defined for the active profile."""
    bundles = skill_bundles.scan_bundles()
    if not bundles:
        bundles_dir = skill_bundles.get_bundles_dir()
        console.print("[yellow]No bundles defined yet.[/yellow]")
        console.print(f"\nCreate one with: [cyan]flowly bundles create &lt;name&gt;[/cyan]")
        console.print(f"Bundles live in: [dim]{bundles_dir}[/dim]")
        return

    table = Table(title=f"Skill Bundles ({len(bundles)})", show_lines=False)
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Skills", justify="right", style="magenta")
    table.add_column("Description")

    for key in sorted(bundles):
        bundle = bundles[key]
        skill_count = len(bundle["skills"])
        description = bundle["description"] or "[dim]—[/dim]"
        table.add_row(key, str(skill_count), description)

    console.print(table)


# --------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------- #


@bundles_app.command("show")
def bundles_show(
    slug: str = typer.Argument(..., help="Bundle slug, with or without leading /"),
) -> None:
    """Show one bundle's full definition."""
    bundle = skill_bundles.get_bundle(slug)
    if not bundle:
        console.print(f"[red]Bundle not found: {slug}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[bold cyan]{bundle['name']}[/bold cyan]  [dim]({bundle['slug']})[/dim]")
    if bundle["description"]:
        console.print(bundle["description"])
    console.print(f"\n[dim]File:[/dim] {bundle['path']}")
    console.print(f"\n[bold]Skills ({len(bundle['skills'])}):[/bold]")
    for skill_name in bundle["skills"]:
        console.print(f"  • {skill_name}")
    if bundle["instruction"]:
        console.print(f"\n[bold]Bundle instruction:[/bold]")
        console.print(bundle["instruction"])


# --------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------- #


@bundles_app.command("create")
def bundles_create(
    name: str = typer.Argument(..., help="Bundle name (becomes /slug)"),
    skill: list[str] = typer.Option(
        None, "--skill", "-s",
        help="Skill name to include. Repeat for multiple skills.",
    ),
    description: str = typer.Option("", "--description", "-d", help="One-line summary"),
    instruction: str = typer.Option(
        "", "--instruction", "-i",
        help="Bundle-level guidance appended to every invocation",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite if exists"),
    interactive: bool = typer.Option(
        False, "--interactive", help="Prompt for skills one at a time",
    ),
) -> None:
    """Create a new bundle.

    Non-interactive flow:

        flowly bundles create research -s web-search -s arxiv -s blogwatcher \\
            -d "Web research workflow"

    Interactive flow:

        flowly bundles create research --interactive
    """
    skills: list[str] = list(skill or [])

    if interactive or not skills:
        console.print(f"[bold]Creating bundle:[/bold] {name}")
        if not description:
            description = Prompt.ask("Description (optional)", default="")
        if not skills:
            console.print(
                "[dim]Enter skill names one per line. Empty line to finish.[/dim]"
            )
            while True:
                entry = Prompt.ask("Skill", default="")
                if not entry.strip():
                    break
                skills.append(entry.strip())
        if not instruction:
            instruction = Prompt.ask("Bundle instruction (optional)", default="")

    if not skills:
        console.print("[red]A bundle must reference at least one skill.[/red]")
        raise typer.Exit(code=1)

    try:
        path = skill_bundles.save_bundle(
            name=name,
            skills=skills,
            description=description,
            instruction=instruction,
            overwrite=force,
        )
    except FileExistsError as exc:
        console.print(
            f"[red]Bundle already exists: {exc}\n"
            "Use --force to overwrite.[/red]"
        )
        raise typer.Exit(code=1)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✔[/green] Bundle saved: [cyan]{path}[/cyan]")
    console.print(
        f"\nInvoke with: [cyan]/{skill_bundles._slugify(name)} &lt;your task&gt;[/cyan]"
    )


# --------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------- #


@bundles_app.command("delete")
def bundles_delete(
    slug: str = typer.Argument(..., help="Bundle slug, with or without leading /"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a bundle file."""
    bundle = skill_bundles.get_bundle(slug)
    if not bundle:
        console.print(f"[red]Bundle not found: {slug}[/red]")
        raise typer.Exit(code=1)

    if not yes:
        confirmed = Confirm.ask(
            f"Delete bundle [cyan]{bundle['slug']}[/cyan] "
            f"({len(bundle['skills'])} skills)?",
            default=False,
        )
        if not confirmed:
            console.print("Cancelled.")
            raise typer.Exit(code=0)

    deleted = skill_bundles.delete_bundle(slug)
    if not deleted:
        console.print("[red]Delete failed — check logs.[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]✔[/green] Deleted: {deleted}")


# --------------------------------------------------------------------- #
# reload
# --------------------------------------------------------------------- #


@bundles_app.command("reload")
def bundles_reload() -> None:
    """Drop the in-process bundle cache.

    The cache normally refreshes on its own when a file's mtime
    changes. Use this when you've edited a bundle from another tool
    (git checkout, external editor) and the agent is still seeing
    the old version.
    """
    skill_bundles.reload()
    bundles = skill_bundles.scan_bundles()
    console.print(f"[green]✔[/green] Reloaded — {len(bundles)} bundle(s) found.")
