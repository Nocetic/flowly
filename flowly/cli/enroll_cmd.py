"""CLI command — ``flowly enroll``: connect a phone / remote client in one step.

Consolidates everything a self-hosted remote connection needs (and that was
painful to do by hand): enable remote access, surface the RIGHT ip (the LAN ip
for the common same-Wi-Fi case, not the public ip), offer to open the firewall
on Windows, and spell out the exact values (and the gateway's key) to give the app.
"""

from __future__ import annotations

import platform
import subprocess

from rich.console import Console

console = Console()


def _add_windows_firewall_rule(port: int) -> bool:
    """Add an inbound TCP allow rule for the gateway port (needs admin)."""
    try:
        r = subprocess.run(
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name=Flowly Gateway {port}", "dir=in", "action=allow",
                "protocol=TCP", f"localport={port}",
            ],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def enroll() -> None:
    """Connect your phone (or another device) to this gateway.

    Enables remote access (binds 0.0.0.0 + ensures a token), shows the exact
    values to type into the Flowly app — the LAN IP for the common same-Wi-Fi
    case — and offers to open the firewall on Windows. Restart the gateway
    afterward so it rebinds.
    """
    from flowly.gateway.remote_info import connect_block, enable_remote_access

    r = enable_remote_access()
    port = r["port"]
    changed = r["host_changed"] or r["token_changed"]

    console.print("\n  [b]✦ Connect your phone[/b] — in the Flowly app, add a gateway with:\n")
    lines, _ = connect_block(r)
    for line in lines:
        console.print(line)
    console.print()

    # Firewall — the usual blocker. On Windows we can add the rule directly.
    if platform.system() == "Windows":
        from rich.prompt import Confirm

        try:
            do_fw = Confirm.ask(f"  Open the Windows firewall for inbound TCP {port}?", default=True)
        except (EOFError, KeyboardInterrupt):
            do_fw = False
        if do_fw:
            if _add_windows_firewall_rule(port):
                console.print(f"  [green]✓[/green] Firewall opened for port {port}.")
            else:
                console.print(
                    f"  [yellow]Couldn't add the rule (needs admin).[/yellow] In an elevated "
                    f"PowerShell:\n  [cyan]New-NetFirewallRule -DisplayName 'Flowly Gateway' "
                    f"-Direction Inbound -LocalPort {port} -Protocol TCP -Action Allow[/cyan]"
                )
    else:
        console.print(
            f"  [dim]If the phone can't reach it, allow inbound TCP {port} in any firewall "
            f"on this machine.[/dim]"
        )

    if changed:
        console.print(
            "\n  [yellow]Apply:[/yellow] the gateway must rebind for remote — "
            "[cyan]flowly service restart[/cyan] [dim](or restart `flowly gateway`).[/dim]"
        )
    console.print(
        "\n  [dim]Keep the token secret. A new one: [cyan]flowly service rotate-token[/cyan].[/dim]"
    )
