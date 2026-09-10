"""Gmail authorization from a local terminal or a headless remote runtime."""

import json
import os
import time
import webbrowser

import typer
from rich.console import Console

from flowly.integrations.gmail_connection import GmailConnection, GmailConnectionError

gmail_app = typer.Typer(help="Connect Gmail to this Flowly profile.", no_args_is_help=True)
console = Console()


def _error(error: Exception) -> None:
    code = error.code if isinstance(error, GmailConnectionError) else "UNAVAILABLE"
    console.print(f"Gmail could not complete this request ({code}).", markup=False)
    raise typer.Exit(1)


@gmail_app.command()
def connect(
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the link without opening a browser."),
    no_wait: bool = typer.Option(False, "--no-wait", help="Print the request and return; run connect again to resume."),
    locale: str = typer.Option("en", help="Authorization page language: en, tr, es."),
):
    """Authorize Gmail in any browser; no callback port or pasted token is needed."""
    service = GmailConnection()
    try:
        setup = service.begin(locale=locale)
        console.print(f"Gmail → {setup['label']} / {setup['profile']}", markup=False)
        console.print(f"Confirmation code: {setup['verificationCode']}", markup=False)
        console.print(setup["authorizationUrl"], markup=False)
        console.print("Open this link on your own device. Confirm the matching agent and code, then sign in with Google.", markup=False)
        if not no_browser and not os.environ.get("SSH_CONNECTION") and not os.environ.get("SSH_TTY"):
            try:
                webbrowser.open(setup["authorizationUrl"])
            except Exception:
                pass  # The printed link remains usable from another device.
        if no_wait:
            return
        while True:
            result = service.setup_status(setup["requestId"])
            if result["status"] == "connected":
                console.print(f"Gmail connected: {result['email']}", markup=False)
                return
            if result["status"] in {"expired", "cancelled", "failed", "revoked", "reauthorize"}:
                console.print(f"Gmail setup ended ({result['status']}). Run flowly gmail connect to try again.", markup=False)
                raise typer.Exit(1)
            time.sleep(5)
    except KeyboardInterrupt:
        console.print("Setup is saved. Run flowly gmail connect to resume, or flowly gmail cancel to cancel.", markup=False)
        raise typer.Exit(130)
    except typer.Exit:
        raise
    except Exception as error:
        _error(error)


@gmail_app.command()
def status(json_output: bool = typer.Option(False, "--json", help="Print a secret-free JSON status.")):
    """Check Gmail from this profile without reading any email."""
    try:
        service = GmailConnection()
        result = {**service.status(), "setup": service.pending_setup()}
        if json_output:
            console.print(json.dumps(result), markup=False, highlight=False)
        else:
            console.print(f"Gmail: {result['status']}" + (f" · {result['email']}" if result.get("email") else ""), markup=False)
            if result["setup"]:
                console.print("A connection request is saved. Run flowly gmail connect to resume.", markup=False)
    except Exception as error:
        _error(error)


@gmail_app.command()
def cancel():
    """Cancel this profile's pending Gmail setup."""
    try:
        service = GmailConnection()
        setup = service.pending_setup()
        if not setup:
            console.print("No pending Gmail setup.")
            return
        service.cancel(setup["requestId"])
        console.print("Gmail setup cancelled.")
    except Exception as error:
        _error(error)


@gmail_app.command()
def disconnect(yes: bool = typer.Option(False, "--yes", "-y", help="Confirm removal from this profile only.")):
    """Remove this profile's connection, not other agents' Google access."""
    try:
        service = GmailConnection()
        current = service.status(verify=False)
        if not current.get("connectionId"):
            console.print("Gmail is not configured.")
            return
        if not yes and not typer.confirm("Disconnect Gmail from this Flowly profile only?"):
            return
        result = service.disconnect(current["connectionId"])
        if result["status"] == "disconnect_pending":
            console.print("Local Gmail access stopped. Remote revocation is pending; run this command again when the connection returns.")
            raise typer.Exit(1)
        console.print("Gmail disconnected from this profile.")
    except typer.Exit:
        raise
    except Exception as error:
        _error(error)
