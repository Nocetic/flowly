"""``flowly logout`` — detach the local gateway from the Flowly account.

CLI counterpart to the TUI's ``/logout`` slash. Clears every Flowly-managed
credential while preserving external provider keys, then applies the change to
the running gateway using provider hot-reload or a relay restart as required.

Why all four:
  * tokens — obvious: revoke local credential material so a stolen
    laptop can't hit the relay.
  * relay config — without this the gateway keeps trying to dial the
    relay with the revoked auth_token; iOS UI still shows the device
    as paired from the server side.
  * active provider — if the user had Flowly hosted as their default
    LLM, the gateway would refuse to boot ("missing server
    identification") on its next start. Clearing the pointer lets the
    BYOK cascade take over silently.
  * account key — the account-scoped ``flw_…`` bearer must never survive
    an account switch.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console

console = Console()


def logout() -> None:
    """Sign out of Flowly and clear local credentials.

    Wipes:
      • keychain account tokens (id, refresh, gateway_auth_token)
      • Flowly hosted account key + ownership metadata
      • channels.web relay config (enabled, server_id, auth_token, jwt_secret)
      • providers.active when it points at "flowly" (BYOK keys preserved)

    Idempotent: no-ops with a friendly message when no Flowly credentials
    exist. Provider-only changes hot-reload; relay removal restarts a managed
    gateway because channel credentials are boot-time state.
    """
    from flowly.account import audit_log
    from flowly.account.account_key import clear_account_key
    from flowly.account.auth import clear_account, load_account_sync
    from flowly.account.relay_config import clear_relay_credentials
    from flowly.account.runtime_apply import apply_account_runtime_change
    from flowly.integrations.active_provider import clear_active_if_matches

    existing = load_account_sync()
    try:
        key_clear = clear_account_key(existing, revoke=existing is not None)
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[red]✗ Couldn't clear Flowly account credentials:[/] {exc}\n"
            "  Account tokens were left intact; fix config access and retry."
        )
        audit_log.error("cli.logout.account_key_clear_failed", error=str(exc))
        raise typer.Exit(code=1)

    if existing is not None:
        clear_account()
    relay_cleared = clear_relay_credentials()
    provider_cleared = False
    try:
        provider_cleared = clear_active_if_matches("flowly")
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]⚠ Couldn't reset providers.active:[/] {exc}\n"
            "  [dim]Tokens and relay are cleared regardless.[/]"
        )

    changed = bool(existing or key_clear.changed or relay_cleared or provider_cleared)
    if not changed:
        console.print("[dim]Not signed in — nothing to do.[/]")
        audit_log.info("cli.logout.no_account")
        return

    if existing is not None:
        console.print(
            f"  [green]✓[/] Signed out [b]{existing.email or existing.user_id}[/]"
        )
        console.print("  [green]✓[/] Cleared keychain tokens")
    if key_clear.changed:
        console.print("  [green]✓[/] Cleared Flowly account credentials")
        if key_clear.revoked is True:
            console.print("  [green]✓[/] Revoked this installation's account key")
        elif key_clear.revoked is False:
            console.print(
                "  [yellow]⚠[/] Account key was removed locally but couldn't be "
                "revoked remotely"
            )
    if relay_cleared:
        console.print("  [green]✓[/] Cleared relay config (iOS pairing disabled)")
    if provider_cleared:
        console.print(
            "  [green]✓[/] Cleared providers.active "
            "[dim](was 'flowly'; cascade resumes)[/]"
        )
    runtime = asyncio.run(
        apply_account_runtime_change(
            provider_reload_required=bool(
                existing or key_clear.changed or provider_cleared
            ),
            relay_restart_required=relay_cleared,
            security_sensitive=True,
        )
    )
    if runtime.status == "provider_reloaded":
        console.print(
            f"  [green]✓[/] Running provider reloaded "
            f"[dim]({runtime.detail})[/]"
        )
    elif runtime.status == "gateway_restarted":
        console.print(
            "  [green]✓[/] Gateway restarted without the signed-out account "
            f"[dim]({runtime.detail})[/]"
        )
    elif runtime.status == "next_start":
        console.print(
            "  [dim]· Gateway isn't running — signed-out state will apply on "
            "its next start.[/]"
        )
    elif runtime.status == "manual_restart":
        console.print(
            "\n  [yellow]⚠ A manually started gateway may still hold the old "
            "account in memory.[/]\n"
            "  Stop that gateway terminal now. Its next start will be signed out."
        )
    elif not runtime.ok:
        console.print(
            f"\n  [yellow]⚠ Credentials are cleared, but the running gateway "
            f"couldn't apply logout:[/] {runtime.detail}\n"
            "  Retry: [cyan]flowly service restart[/]"
        )

    audit_log.info(
        "cli.logout.cleared",
        user_id=existing.user_id if existing else None,
        email=existing.email if existing else None,
        provider_active_cleared=provider_cleared,
        account_key_cleared=key_clear.changed,
        account_key_revoked=key_clear.revoked,
        relay_cleared=relay_cleared,
        gateway_runtime_status=runtime.status,
    )
    if not runtime.ok:
        raise typer.Exit(code=4)
