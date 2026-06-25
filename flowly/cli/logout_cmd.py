"""``flowly logout`` — clear keychain tokens + relay config + active provider.

CLI counterpart to the TUI's ``/logout`` slash. Drives the same three
mutations the TUI does (``clear_account`` + ``clear_relay_credentials``
+ ``clear_active_if_matches('flowly')``), produces the same audit
log, so a user that logs out from either surface ends up in the
same state.

Why all three:
  * tokens — obvious: revoke local credential material so a stolen
    laptop can't hit the relay.
  * relay config — without this the gateway keeps trying to dial the
    relay with the revoked auth_token; iOS UI still shows the device
    as paired from the server side.
  * active provider — if the user had Flowly hosted as their default
    LLM, the gateway would refuse to boot ("missing server
    identification") on its next start. Clearing the pointer lets the
    BYOK cascade take over silently.
"""

from __future__ import annotations

import typer
from rich.console import Console

console = Console()


def logout() -> None:
    """Sign out of Flowly and clear local credentials.

    Wipes:
      • keychain account tokens (id, refresh, gateway_auth_token)
      • channels.web relay config (enabled, server_id, auth_token, jwt_secret)
      • providers.active when it points at "flowly" (BYOK keys preserved)

    Idempotent: no-ops with a friendly message when not signed in.
    Restart a running ``flowly gateway`` afterwards so it stops trying
    to authenticate to the relay with the revoked credentials.
    """
    from flowly.account import audit_log
    from flowly.account.auth import clear_account, load_account_sync
    from flowly.account.relay_config import clear_relay_credentials
    from flowly.integrations.active_provider import clear_active_if_matches

    existing = load_account_sync()
    if existing is None:
        console.print("[dim]Not signed in — nothing to do.[/]")
        audit_log.info("cli.logout.no_account")
        return

    clear_account()
    clear_relay_credentials()
    provider_cleared = False
    try:
        provider_cleared = clear_active_if_matches("flowly")
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]⚠ Couldn't reset providers.active:[/] {exc}\n"
            "  [dim]Tokens and relay are cleared regardless.[/]"
        )

    console.print(
        f"  [green]✓[/] Signed out [b]{existing.email or existing.user_id}[/]"
    )
    console.print("  [green]✓[/] Cleared keychain tokens")
    console.print("  [green]✓[/] Cleared relay config (iOS pairing disabled)")
    if provider_cleared:
        console.print(
            "  [green]✓[/] Cleared providers.active "
            "[dim](was 'flowly'; cascade resumes)[/]"
        )
    console.print()
    console.print(
        "  [dim]Restart [b]flowly gateway[/b] so it stops dialing the relay "
        "with revoked credentials.[/]"
    )

    audit_log.info(
        "cli.logout.cleared",
        user_id=existing.user_id,
        email=existing.email,
        provider_active_cleared=provider_cleared,
    )
