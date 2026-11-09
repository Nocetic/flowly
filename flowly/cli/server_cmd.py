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
    from flowly.account.home_registration import HomeLinkedElsewhere, HomeRegistrationError, link_this_machine
    from flowly.cli.login_cmd import _refuse_linked_elsewhere

    account = asyncio.run(load_account_refreshing())
    if account is None:
        console.print("[red]Not signed in.[/] Run [bold]flowly login[/] first.")
        raise typer.Exit(1)
    try:
        registration = asyncio.run(link_this_machine(account, server_url=url, name=name, host_id=host_id, adopt=adopt))
    except HomeLinkedElsewhere as linked:
        _refuse_linked_elsewhere(linked.owner_email)
    except HomeRegistrationError as error:
        console.print(f"[red]The Flowly server refused:[/] {error.code}")
        raise typer.Exit(1)
    state = "registered" if registration.created else "re-registered"
    console.print(f"[green]✓[/] This machine is {state} as agent host [bold]{registration.agent_host_id}[/].")
    if registration.adopted:
        console.print("  Its relay-era conversations carry over to the new apps.")
    elif adopt and registration.created:
        console.print("  [yellow]⚠ No relay-era entry of this machine was found; this is a new home with no past conversations.[/]")
    console.print("  Restart the gateway to connect: [bold]flowly service restart[/] (or restart Flowly Desktop).")


@server_app.command("disconnect")
def server_disconnect():
    """Unlink this agent from its Flowly account: the account's apps no longer reach it.

    The agent keeps running (terminal, Telegram, schedules) with its memory on
    this machine, and another account may link it next (D53).
    """
    from flowly.account import home_registration as homes
    from flowly.config.loader import load_config

    state = homes.link_state()
    channel = load_config().channels.flowly_server
    if state.linked:
        server_url, secret, host_id = channel.connection_settings()
        try:
            asyncio.run(homes.release_home(server_url, host_id, secret))
        except homes.HomeRegistrationError as error:
            console.print(
                f"[yellow]⚠ The Flowly server could not be told ({error.code}):[/] this computer may still "
                "show as offline in the account's apps until it is removed there."
            )
    if homes.unwire_flowly_server():
        owner = f" removed from {state.owner_email}" if state.owner_email else " unlinked"
        console.print(f"[green]✓[/] This computer is{owner}. Restart the gateway to apply.")
    else:
        console.print("This computer was not linked to a Flowly account.")


@server_app.command("status")
def server_status(
    as_json: bool = typer.Option(False, "--json", help="Print the link as JSON (Flowly Desktop reads it)."),
):
    """Show whether this machine uses the Flowly server or the relay, and for which account."""
    from flowly.account.health import check_relay_state
    from flowly.account.home_registration import link_state

    link = link_state()
    if as_json:
        import json

        from flowly.account.auth import load_account_sync

        signed_in = load_account_sync()
        print(json.dumps({
            "linked": link.linked, "hostId": link.host_id or None,
            "ownerUid": link.owner_uid or None, "ownerEmail": link.owner_email or None,
            "signedInUid": (signed_in.user_id or None) if signed_in is not None else None,
        }))
        return

    state = check_relay_state()
    if state.via == "flowlyServer":
        mark = "[green]✓[/]" if state.healthy else "[yellow]⚠[/]"
        console.print(f"{mark} Flowly server (agent host {state.server_id or '—'})" + ("" if state.healthy else f": {state.reason}"))
        if link.owner_email:
            console.print(f"  Linked to {link.owner_email} · reachable from the Flowly apps")
    else:
        console.print(f"Relay{'' if state.healthy else ' (not set up)'}; the Flowly server channel is off.")
