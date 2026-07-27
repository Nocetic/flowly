"""``flowly keychain`` — inspect and re-enable OS keychain credential storage.

Flowly prefers the OS keychain (macOS Keychain, Windows Credential Manager,
Linux Secret Service) for sign-in tokens and subscription credentials, and
falls back to an owner-only file when the keychain refuses. That fallback is
sticky on purpose — a Mac with no default keychain otherwise re-opens a
blocking "Keychain Not Found" panel on every save — but until now the only
way out was deleting a dotfile nobody knew about.

``retry`` clears the latch; ``status`` says which store is actually in use.
"""

from __future__ import annotations

import typer
from rich.console import Console

keychain_app = typer.Typer(help="Inspect or re-enable OS keychain storage")
console = Console()


@keychain_app.command("status")
def status() -> None:
    """Show where credentials are stored right now."""
    from flowly.account.token_store import storage_status

    current = storage_status()
    icon = "[green]🔒[/]" if current.secure else "[yellow]⚠[/]"
    console.print(f"  {icon} {current.detail}")
    if not current.secure:
        console.print(
            "  [dim]Fix the keychain, then run [b]flowly keychain retry[/b].[/]"
        )


@keychain_app.command("retry")
def retry() -> None:
    """Let Flowly try the OS keychain again after you've fixed it.

    Existing credentials stay where they are; the next read moves them into
    the keychain and removes the file copy.
    """
    from flowly.account.token_store import reset_keyring_disable, storage_status

    if not reset_keyring_disable():
        current = storage_status()
        if current.secure:
            console.print(f"  [green]✓[/] Already using {current.detail}")
        else:
            # No latch to clear, yet still not on the keychain — the machine
            # has no usable one at all (headless Linux, no login keychain).
            console.print(f"  [yellow]⚠[/] {current.detail}")
        return

    console.print("  [green]✓[/] Flowly will try the OS keychain again.")
    console.print(
        "  [dim]Restart Flowly (and [b]flowly gateway[/b] if it's running) "
        "so the change takes effect.[/]"
    )
