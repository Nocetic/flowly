"""CLI commands — server_cmd: this Core's home on the Flowly server (server/PROTOCOL.md B6)."""

import asyncio

import typer
from rich.console import Console

console = Console()

server_app = typer.Typer(help="Connect this machine's agent to the Flowly server (instead of the relay)")


@server_app.command("connect")
def server_connect(
    name: str = typer.Option("", "--name", help="The name the apps show (default: this machine's name)."),
    adopt: bool = typer.Option(
        False, "--adopt",
        help="Take over this machine's relay-era server entry (found by its fingerprint), so its conversations appear in the new apps.",
    ),
    host_id: str = typer.Option("", "--host-id", help="Re-register this exact server entry (its id)."),
    url: str = typer.Option("", "--url", help="The Flowly server's agent URL (default: config, then the public server)."),
):
    """Register this machine with the Flowly server and use it instead of the relay."""
    from flowly.account.auth import load_account_refreshing
    from flowly.account.fingerprint import machine_id, machine_name
    from flowly.account.home_registration import (
        HomeRegistrationError,
        register_home,
        wire_flowly_server,
    )
    from flowly.config.loader import load_config

    account = asyncio.run(load_account_refreshing())
    if account is None:
        console.print("[red]Not signed in.[/] Run [bold]flowly login[/] first.")
        raise typer.Exit(1)
    channel = load_config().channels.flowly_server
    server_url = url or channel.server_url
    # The home this machine already has, else the entry named, else (asked for)
    # its relay-era entry found by fingerprint, else a new one.
    target = channel.agent_host_id or host_id or None
    try:
        registration = asyncio.run(register_home(
            account.id_token, server_url=server_url, name=name or machine_name(), host_id=target,
            adopt_machine_id=machine_id() if adopt and not target else None,
        ))
    except HomeRegistrationError as error:
        console.print(f"[red]The Flowly server refused:[/] {error.code}")
        raise typer.Exit(1)
    wire_flowly_server(registration, server_url)
    state = "registered" if registration.created else "re-registered"
    console.print(f"[green]✓[/] This machine is {state} as agent host [bold]{registration.agent_host_id}[/].")
    if registration.adopted:
        console.print("  Its relay-era conversations carry over to the new apps.")
    elif adopt and not target:
        console.print("  [yellow]⚠ No relay-era entry of this machine was found; this is a new home with no past conversations.[/]")
    console.print("  Restart the gateway to connect: [bold]flowly service restart[/] (or restart Flowly Desktop).")


@server_app.command("disconnect")
def server_disconnect():
    """Stop using the Flowly server; the relay (if set up) takes over again."""
    from flowly.account.home_registration import unwire_flowly_server

    if unwire_flowly_server():
        console.print("[green]✓[/] The Flowly server channel is off. Restart the gateway to apply.")
    else:
        console.print("The Flowly server channel was already off.")


@server_app.command("status")
def server_status():
    """Show whether this machine uses the Flowly server or the relay."""
    from flowly.account.health import check_relay_state

    state = check_relay_state()
    if state.via == "flowlyServer":
        mark = "[green]✓[/]" if state.healthy else "[yellow]⚠[/]"
        console.print(f"{mark} Flowly server (agent host {state.server_id or '—'})" + ("" if state.healthy else f": {state.reason}"))
    else:
        console.print(f"Relay{'' if state.healthy else ' (not set up)'}; the Flowly server channel is off.")
