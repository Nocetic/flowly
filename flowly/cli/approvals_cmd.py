"""CLI commands — approvals_cmd."""

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
# Exec Approvals Commands
# ============================================================================

approvals_app = typer.Typer(help="Manage command execution approvals")


@approvals_app.command("status")
def approvals_status():
    """Show exec approvals configuration."""
    from flowly.exec.approvals import ExecApprovalStore

    store = ExecApprovalStore()
    config = store.load()

    console.print("\n[bold cyan]Exec Approvals Configuration[/bold cyan]")
    console.print("─" * 40)
    console.print(f"Security: [cyan]{config.security}[/cyan]")
    console.print(f"Ask mode: [cyan]{config.ask}[/cyan]")
    console.print(f"Ask fallback: [cyan]{config.ask_fallback}[/cyan]")
    console.print(f"Allowlist entries: [cyan]{len(config.allowlist)}[/cyan]")

    if config.security == "deny":
        console.print("\n[yellow]⚠️  Command execution is currently DENIED[/yellow]")
        console.print("[dim]Run 'flowly approvals set --security allowlist' to enable[/dim]")


@approvals_app.command("set")
def approvals_set(
    security: str = typer.Option(None, "--security", "-s", help="Security mode: deny, allowlist, full"),
    ask: str = typer.Option(None, "--ask", "-a", help="Ask mode: off, on-miss, always"),
):
    """Update exec approvals configuration."""
    from flowly.exec.approvals import ExecApprovalStore

    store = ExecApprovalStore()
    config = store.load()

    if security:
        if security not in ("deny", "allowlist", "full"):
            console.print(f"[red]Invalid security mode: {security}[/red]")
            raise typer.Exit(1)
        config.security = security
        console.print(f"[green]✓[/green] Security set to [cyan]{security}[/cyan]")

    if ask:
        if ask not in ("off", "on-miss", "always"):
            console.print(f"[red]Invalid ask mode: {ask}[/red]")
            raise typer.Exit(1)
        config.ask = ask
        console.print(f"[green]✓[/green] Ask mode set to [cyan]{ask}[/cyan]")

    store.save()


@approvals_app.command("list")
def approvals_list():
    """List allowlist entries."""
    from flowly.exec.approvals import ExecApprovalStore

    store = ExecApprovalStore()
    config = store.load()

    if not config.allowlist:
        console.print("[dim]No allowlist entries.[/dim]")
        console.print("[dim]Commands will require approval (if ask mode is on-miss or always)[/dim]")
        return

    table = Table(title="Exec Allowlist")
    table.add_column("Pattern", style="cyan")
    table.add_column("Last Used")
    table.add_column("Command")

    import time
    for entry in config.allowlist:
        last_used = ""
        if entry.last_used_at:
            last_used = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.last_used_at / 1000))
        cmd = entry.last_used_command or ""
        if len(cmd) > 40:
            cmd = cmd[:40] + "..."
        table.add_row(entry.pattern, last_used, cmd)

    console.print(table)


# ============================================================================
# Sessions Commands
# ============================================================================

sessions_app = typer.Typer(help="Monitor background subagent tasks")


def _render_sessions_table(records: list, status_filter: str | None = None) -> None:
    """Render subagent records as a rich table."""
    import time as _time

    if status_filter:
        if status_filter == "running":
            records = [r for r in records if r.ended_at is None]
        elif status_filter == "completed":
            records = [r for r in records if r.outcome == "ok"]
        elif status_filter == "failed":
            records = [r for r in records if r.outcome in ("error", "timeout")]

    if not records:
        label = f" with status '{status_filter}'" if status_filter else ""
        console.print(f"[dim]No background tasks{label}.[/dim]")
        return

    table = Table(title=f"Background Tasks ({len(records)})", expand=True)
    table.add_column("Status", width=14)
    table.add_column("Label", style="cyan")
    table.add_column("Model", style="dim", width=22)
    table.add_column("Duration", width=10)
    table.add_column("ID", style="dim", width=10)

    for r in sorted(records, key=lambda x: x.created_at, reverse=True):
        if r.ended_at is None:
            state = "[yellow]⏳ running[/yellow]"
        elif r.outcome == "ok":
            state = "[green]✓ done[/green]"
        elif r.outcome == "timeout":
            state = "[yellow]⏰ timeout[/yellow]"
        else:
            state = "[red]✗ failed[/red]"

        duration = ""
        if r.started_at and r.ended_at:
            secs = int(r.ended_at - r.started_at)
            duration = f"{secs}s"
        elif r.started_at:
            secs = int(_time.time() - r.started_at)
            duration = f"{secs}s…"

        model_short = (r.model or "")
        if "/" in model_short:
            model_short = model_short.split("/")[-1]
        if len(model_short) > 20:
            model_short = model_short[:20] + "…"

        table.add_row(state, r.label, model_short, duration, r.run_id[:8])

    console.print(table)


@sessions_app.command("list")
def sessions_list(
    status: str = typer.Option(None, "--status", "-s", help="Filter: running, completed, failed"),
    watch: bool = typer.Option(False, "--watch", "-w", help="Refresh every 2 seconds"),
):
    """List background subagent tasks."""
    from flowly.agent.subagent_registry import SubagentRegistry
    import time as _time

    registry = SubagentRegistry()

    if not watch:
        _render_sessions_table(registry.all(), status)
        return

    try:
        while True:
            console.clear()
            registry._load_from_disk()
            _render_sessions_table(registry.all(), status)
            console.print(f"\n[dim]Refreshing every 2s — Ctrl+C to exit[/dim]")
            _time.sleep(2)
    except KeyboardInterrupt:
        pass


@sessions_app.command("clear")
def sessions_clear(
    keep_running: bool = typer.Option(True, "--keep-running/--all", help="Keep running tasks (default: yes)"),
):
    """Clear completed/failed task history."""
    from flowly.agent.subagent_registry import SubagentRegistry

    registry = SubagentRegistry()
    records = registry.all()
    before = len(records)

    to_remove = [r for r in records if not (keep_running and r.ended_at is None)]
    for r in to_remove:
        registry._runs.pop(r.run_id, None)
    registry._persist()

    removed = before - len(registry.all())
    console.print(f"[green]✓[/green] Cleared {removed} task(s)")


@approvals_app.command("add")
def approvals_add(
    pattern: str = typer.Argument(..., help="Path pattern to allow (supports glob)"),
):
    """Add a pattern to the allowlist."""
    from flowly.exec.approvals import ExecApprovalStore

    store = ExecApprovalStore()
    store.load()
    store.add_to_allowlist(pattern)

    console.print(f"[green]✓[/green] Added [cyan]{pattern}[/cyan] to allowlist")


@approvals_app.command("remove")
def approvals_remove(
    pattern: str = typer.Argument(..., help="Pattern to remove"),
):
    """Remove a pattern from the allowlist."""
    from flowly.exec.approvals import ExecApprovalStore

    store = ExecApprovalStore()
    store.load()

    if store.remove_from_allowlist(pattern):
        console.print(f"[green]✓[/green] Removed [cyan]{pattern}[/cyan] from allowlist")
    else:
        console.print(f"[yellow]Pattern not found: {pattern}[/yellow]")


@approvals_app.command("safe-bins")
def approvals_safe_bins():
    """List safe bins that are always allowed."""
    from flowly.exec.safety import DEFAULT_SAFE_BINS

    console.print("\n[bold]Safe Bins (always allowed for stdin operations):[/bold]")
    for bin_name in sorted(DEFAULT_SAFE_BINS):
        console.print(f"  • {bin_name}")
    console.print("\n[dim]These commands are allowed without explicit allowlist entry[/dim]")
    console.print("[dim]when they don't reference files as arguments.[/dim]")


