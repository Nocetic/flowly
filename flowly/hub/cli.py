"""CLI for Flowly Hub - skill management."""

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="flowly-hub",
    help="Flowly Hub - Skill management CLI",
    no_args_is_help=True,
)

console = Console()


def _get_manager():
    """Get skill manager instance."""
    from flowly.hub.manager import SkillManager
    from flowly.utils.helpers import get_workspace_path

    workspace = get_workspace_path()
    return SkillManager(workspace_dir=workspace)


# ============================================================================
# Search
# ============================================================================


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum results"),
):
    """
    Search for skills in the registry.

    Examples:
        flowly-hub search github
        flowly-hub search "weather api"
    """
    with _get_manager() as manager:
        results = manager.search(query)

    if not results:
        console.print(f"[yellow]No skills found for '{query}'[/yellow]")
        return

    table = Table(title=f"Skills matching '{query}'")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    table.add_column("Version", style="dim")
    table.add_column("Author", style="dim")

    for skill in results[:limit]:
        table.add_row(
            skill.slug,
            skill.description[:60] + "..." if len(skill.description) > 60 else skill.description,
            skill.version,
            skill.author,
        )

    console.print(table)
    console.print(f"\n[dim]Install with: flowly-hub install <skill-name>[/dim]")


# ============================================================================
# Install
# ============================================================================


@app.command()
def install(
    source: str = typer.Argument(..., help="Skill source (name, github:..., URL, or path)"),
    force: bool = typer.Option(False, "--force", "-f", help="Force reinstall"),
    workspace: bool = typer.Option(False, "--workspace", "-w", help="Install to workspace"),
):
    """
    Install a skill from various sources.

    Examples:
        flowly-hub install github
        flowly-hub install github@1.2.0
        flowly-hub install github:owner/repo/skill-name
        flowly-hub install https://example.com/skill.md
        flowly-hub install ./my-skill
    """
    with _get_manager() as manager:
        console.print(f"[cyan]Installing {source}...[/cyan]")

        skill = manager.install(source, force=force, to_workspace=workspace)

        if skill:
            console.print(f"[green]✓[/green] Installed [cyan]{skill.name}[/cyan] v{skill.version}")
            console.print(f"  Location: {skill.path}")
        else:
            console.print(f"[red]✗[/red] Failed to install {source}")
            raise typer.Exit(1)


# ============================================================================
# Update
# ============================================================================


@app.command()
def update(
    skill: str = typer.Argument(None, help="Skill to update (or all if not specified)"),
    force: bool = typer.Option(False, "--force", "-f", help="Force update even if modified"),
    all_skills: bool = typer.Option(False, "--all", "-a", help="Update all skills"),
):
    """
    Update installed skill(s) to latest version.

    Examples:
        flowly-hub update github
        flowly-hub update --all
        flowly-hub update --all --force
    """
    with _get_manager() as manager:
        if not skill and not all_skills:
            console.print("[yellow]Specify a skill or use --all[/yellow]")
            raise typer.Exit(1)

        slug = None if all_skills else skill
        updated = manager.update(slug, force=force)

        if updated:
            console.print(f"[green]✓[/green] Updated {len(updated)} skill(s):")
            for s in updated:
                console.print(f"  • {s.name} v{s.version}")
        else:
            console.print("[yellow]No skills updated[/yellow]")


# ============================================================================
# Remove
# ============================================================================


@app.command()
def remove(
    skill: str = typer.Argument(..., help="Skill to remove"),
    workspace: bool = typer.Option(False, "--workspace", "-w", help="Remove from workspace"),
):
    """
    Remove an installed skill.

    Examples:
        flowly-hub remove github
        flowly-hub remove github --workspace
    """
    with _get_manager() as manager:
        if manager.remove(skill, from_workspace=workspace):
            console.print(f"[green]✓[/green] Removed [cyan]{skill}[/cyan]")
        else:
            console.print(f"[red]✗[/red] Failed to remove {skill}")
            raise typer.Exit(1)


# ============================================================================
# List
# ============================================================================


@app.command("list")
def list_skills(
    all_skills: bool = typer.Option(False, "--all", "-a", help="Include workspace skills"),
):
    """
    List installed skills.

    Examples:
        flowly-hub list
        flowly-hub list --all
    """
    with _get_manager() as manager:
        skills = manager.list_installed(include_workspace=all_skills)

    if not skills:
        console.print("[yellow]No skills installed[/yellow]")
        console.print("\n[dim]Install skills with: flowly-hub install <skill-name>[/dim]")
        return

    table = Table(title="Installed Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Version")
    table.add_column("Source", style="dim")
    table.add_column("Modified", style="yellow")

    for skill in skills:
        modified = "✓" if skill.is_modified else ""
        source_short = skill.source[:30] + "..." if len(skill.source) > 30 else skill.source
        table.add_row(skill.slug, skill.version, source_short, modified)

    console.print(table)


# ============================================================================
# Info
# ============================================================================


@app.command()
def info(
    skill: str = typer.Argument(..., help="Skill name"),
):
    """
    Show detailed information about a skill.

    Examples:
        flowly-hub info github
    """
    with _get_manager() as manager:
        data = manager.info(skill)

    if not data:
        console.print(f"[red]Skill '{skill}' not found[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold cyan]{skill}[/bold cyan]")
    console.print()

    if data.get("installed"):
        console.print("[green]✓ Installed[/green]")
        console.print(f"  Version: {data['version']}")
        console.print(f"  Source: {data['source']}")
        console.print(f"  Path: {data['path']}")
        if data.get("modified"):
            console.print("  [yellow]⚠ Locally modified[/yellow]")
        console.print()

    if data.get("registry"):
        reg = data["registry"]
        console.print("[dim]Registry Info:[/dim]")
        console.print(f"  Name: {reg['name']}")
        console.print(f"  Description: {reg['description']}")
        console.print(f"  Latest: v{reg['version']}")
        console.print(f"  Author: {reg['author']}")
        if reg.get("homepage"):
            console.print(f"  Homepage: {reg['homepage']}")

    if data.get("update_available"):
        console.print(f"\n[yellow]Update available: v{data['update_available']}[/yellow]")
        console.print(f"[dim]Run: flowly-hub update {skill}[/dim]")


# ============================================================================
# Check
# ============================================================================


@app.command()
def check():
    """
    Check for skill updates.

    Examples:
        flowly-hub check
    """
    with _get_manager() as manager:
        skills = manager.list_installed(include_workspace=False)

    updates = []
    for skill in skills:
        info = manager.info(skill.slug)
        if info and info.get("update_available"):
            updates.append((skill.slug, skill.version, info["update_available"]))

    if not updates:
        console.print("[green]✓[/green] All skills are up to date")
        return

    console.print(f"[yellow]{len(updates)} update(s) available:[/yellow]\n")

    table = Table()
    table.add_column("Skill", style="cyan")
    table.add_column("Current")
    table.add_column("Latest", style="green")

    for slug, current, latest in updates:
        table.add_row(slug, current, latest)

    console.print(table)
    console.print("\n[dim]Run: flowly-hub update --all[/dim]")


# ============================================================================
# Create (for skill authors)
# ============================================================================


@app.command()
def create(
    name: str = typer.Argument(..., help="Skill name"),
    output: str = typer.Option(".", "--output", "-o", help="Output directory"),
):
    """
    Create a new skill template.

    Examples:
        flowly-hub create my-skill
        flowly-hub create my-skill -o ./skills
    """
    from pathlib import Path

    output_path = Path(output).expanduser().resolve()
    skill_dir = output_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Create SKILL.md template
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(f'''---
name: {name}
description: "Short description of what this skill does"
metadata: {{"flowly":{{"emoji":"🔧","requires":{{"bins":[],"env":[]}}}}}}
---

# {name.replace("-", " ").title()} Skill

Describe what this skill does and how to use it.

## Usage

```bash
# Example command
your-command --example
```

## Examples

### Example 1: Basic usage

Explain how to use the skill in a common scenario.

### Example 2: Advanced usage

Show more complex use cases.

## Notes

- Important considerations
- Known limitations
''', encoding="utf-8")

    # Create optional directories
    (skill_dir / "scripts").mkdir(exist_ok=True)
    (skill_dir / "references").mkdir(exist_ok=True)

    # Create README for scripts
    (skill_dir / "scripts" / "README.md").write_text('''# Scripts

Put helper scripts here. They will be available to the agent.

Example:
- setup.sh - Initial setup script
- check.sh - Health check script
''', encoding="utf-8")

    console.print(f"[green]✓[/green] Created skill template at [cyan]{skill_dir}[/cyan]")
    console.print("\nFiles created:")
    console.print(f"  • {skill_dir}/SKILL.md")
    console.print(f"  • {skill_dir}/scripts/")
    console.print(f"  • {skill_dir}/references/")
    console.print("\n[dim]Edit SKILL.md to customize your skill[/dim]")


# ============================================================================
# Publish (for skill authors)
# ============================================================================


@app.command()
def publish():
    """
    Publish a skill to the Flowly Skills Marketplace.

    Skills are published through the web UI at https://useflowlyapp.com/skills/publish
    """
    console.print("Publish skills at: [cyan]https://useflowlyapp.com/skills/publish[/cyan]")
    console.print("\n[dim]Sign in with your Flowly account, upload your SKILL.md,")
    console.print("and the marketplace handles security scanning and publishing.[/dim]")


# ============================================================================
# Main entry point
# ============================================================================


def main():
    """Main entry point for flowly-hub CLI."""
    app()


if __name__ == "__main__":
    main()
