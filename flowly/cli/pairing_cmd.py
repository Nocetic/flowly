"""CLI commands — pairing_cmd."""

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
# Pairing Commands
# ============================================================================

pairing_app = typer.Typer(help="Secure channel pairing")

PAIRING_CLI_CHANNELS = ("telegram", "whatsapp", "discord", "slack", "imessage")


@pairing_app.command("list")
def pairing_list(
    channel: str = typer.Argument(..., help="Channel (telegram, whatsapp)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List pending pairing requests."""
    from flowly.pairing import list_pairing_requests

    if channel not in PAIRING_CLI_CHANNELS:
        console.print(f"[red]Invalid channel: {channel}. Use one of: {', '.join(PAIRING_CLI_CHANNELS)}[/red]")
        raise typer.Exit(1)

    requests = list_pairing_requests(channel)

    if json_output:
        import json
        data = [
            {
                "id": r.id,
                "code": r.code,
                "created_at": r.created_at,
                "meta": r.meta,
            }
            for r in requests
        ]
        console.print(json.dumps({"channel": channel, "requests": data}, indent=2))
        return

    if not requests:
        console.print(f"[dim]No pending {channel} pairing requests.[/dim]")
        return

    table = Table(title=f"Pending {channel.title()} Pairing Requests")
    table.add_column("Code", style="cyan")
    table.add_column("User ID")
    table.add_column("Meta")
    table.add_column("Requested")

    for r in requests:
        meta_str = ", ".join(f"{k}={v}" for k, v in r.meta.items()) if r.meta else ""
        table.add_row(r.code, r.id, meta_str, r.created_at[:19])

    console.print(table)


@pairing_app.command("approve")
def pairing_approve(
    channel: str = typer.Argument(..., help="Channel (telegram, whatsapp)"),
    code: str = typer.Argument(..., help="Pairing code"),
    notify: bool = typer.Option(False, "--notify", "-n", help="Notify user on approval"),
):
    """Approve a pairing code."""
    from flowly.pairing import approve_pairing_code
    from flowly.config.loader import load_config

    if channel not in PAIRING_CLI_CHANNELS:
        console.print(f"[red]Invalid channel: {channel}. Use one of: {', '.join(PAIRING_CLI_CHANNELS)}[/red]")
        raise typer.Exit(1)

    approved = approve_pairing_code(channel, code)

    if not approved:
        console.print(f"[red]No pending pairing request found for code: {code}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] Approved {channel} sender [cyan]{approved.id}[/cyan]")

    if approved.meta:
        meta_str = ", ".join(f"{k}={v}" for k, v in approved.meta.items())
        console.print(f"  [dim]({meta_str})[/dim]")

    # Notify user if requested
    if notify and channel == "telegram":
        config = load_config()
        if config.channels.telegram.token:
            async def send_notification():
                import httpx
                token = config.channels.telegram.token
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post(url, json={
                            "chat_id": approved.id,
                            "text": "✅ Access approved! Send a message to start chatting.",
                        })
                    console.print(f"[green]✓[/green] Notification sent")
                except Exception as e:
                    console.print(f"[yellow]Warning: Could not notify user: {e}[/yellow]")

            asyncio.run(send_notification())


@pairing_app.command("revoke")
def pairing_revoke(
    channel: str = typer.Argument(..., help="Channel (telegram, whatsapp)"),
    user_id: str = typer.Argument(..., help="User ID to revoke"),
):
    """Revoke access for a user."""
    from flowly.pairing import remove_allow_from_entry

    if channel not in PAIRING_CLI_CHANNELS:
        console.print(f"[red]Invalid channel: {channel}. Use one of: {', '.join(PAIRING_CLI_CHANNELS)}[/red]")
        raise typer.Exit(1)

    if remove_allow_from_entry(channel, user_id):
        console.print(f"[green]✓[/green] Revoked access for {user_id}")
    else:
        console.print(f"[yellow]User {user_id} was not in the allow list[/yellow]")


@pairing_app.command("allowed")
def pairing_allowed(
    channel: str = typer.Argument(..., help="Channel (telegram, whatsapp)"),
):
    """List allowed users from pairing store."""
    from flowly.pairing import read_allow_from_store

    if channel not in PAIRING_CLI_CHANNELS:
        console.print(f"[red]Invalid channel: {channel}. Use one of: {', '.join(PAIRING_CLI_CHANNELS)}[/red]")
        raise typer.Exit(1)

    allowed = read_allow_from_store(channel)

    if not allowed:
        console.print(f"[dim]No users in {channel} pairing store.[/dim]")
        console.print("[dim]Users can also be allowed via config.json allow_from list.[/dim]")
        return

    console.print(f"[bold]{channel.title()} Allowed Users (from pairing):[/bold]")
    for user_id in allowed:
        console.print(f"  • {user_id}")


