"""``flowly xai`` — Grok subscription OAuth commands."""

from __future__ import annotations

import secrets
import time
import webbrowser

import httpx
import typer
from rich.console import Console

from flowly.auth import xai_oauth

xai_app = typer.Typer(help="Connect xAI/Grok subscription OAuth.")
console = Console()


@xai_app.command("login")
def login(
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL instead of opening a browser."),
    manual_paste: bool = typer.Option(False, "--manual-paste", help="Paste the callback URL/code manually."),
    set_active: bool = typer.Option(True, "--set-active/--no-set-active", help="Use xAI OAuth as active provider after login."),
    timeout_seconds: int = typer.Option(300, "--timeout", help="Seconds to wait for loopback callback."),
) -> None:
    """Sign in to xAI OAuth for SuperGrok / X Premium+ API access."""
    # Shared public grok-cli client — not configurable (see xai_oauth).
    resolved_client_id = xai_oauth.require_client_id()
    try:
        if manual_paste:
            metadata = xai_oauth.discover_oauth_metadata()
            verifier = xai_oauth.pkce_verifier()
            challenge = xai_oauth.pkce_challenge(verifier)
            state = secrets.token_urlsafe(32)
            nonce = secrets.token_urlsafe(32)
            url = xai_oauth.build_authorize_url(
                client_id=resolved_client_id,
                code_challenge=challenge,
                state=state,
                nonce=nonce,
                authorization_endpoint=metadata["authorization_endpoint"],
            )
            console.print("\nOpen this URL and complete xAI login:\n")
            console.print(f"[cyan]{url}[/cyan]\n")
            if not no_browser:
                webbrowser.open(url)
            pasted = typer.prompt("Paste the final callback URL or code")
            parsed = xai_oauth._parse_callback_input(pasted)  # narrow CLI glue
            if parsed.get("state") and parsed["state"] != state:
                raise xai_oauth.XAIAuthError("OAuth state mismatch in pasted callback")
            code = parsed.get("code", "")
            if not code:
                raise xai_oauth.XAIAuthError("Pasted value did not contain an OAuth code")
            payload = xai_oauth.exchange_code_for_tokens(
                code=code,
                client_id=resolved_client_id,
                code_verifier=verifier,
                code_challenge_value=challenge,
                token_endpoint=metadata["token_endpoint"],
            )
        else:
            console.print(
                f"\nWaiting for xAI OAuth callback on [cyan]{xai_oauth.XAI_OAUTH_REDIRECT_URI}[/]..."
            )
            if no_browser:
                console.print("[dim]Use --manual-paste on headless machines if loopback is unreachable.[/]")
            payload = xai_oauth.login_with_loopback(
                client_id=resolved_client_id,
                no_browser=no_browser,
                timeout_seconds=timeout_seconds,
                on_authorize_url=lambda url: console.print(f"\n[cyan]{url}[/cyan]\n") if no_browser else None,
            )
        backend = xai_oauth.save_token_payload(payload)
        if set_active:
            from flowly.config.loader import load_config, save_config
            from flowly.integrations.active_provider import set_active_provider
            from flowly.providers.xai_responses_provider import DEFAULT_XAI_RESPONSES_MODEL

            set_active_provider("xai_oauth")
            cfg = load_config()
            current_model = (cfg.agents.defaults.model or "").strip()
            if "/" in current_model or not current_model.lower().startswith("grok"):
                cfg.agents.defaults.model = DEFAULT_XAI_RESPONSES_MODEL
                save_config(cfg)
        console.print("\n[green]✓[/] xAI Grok OAuth connected")
        if payload.email:
            console.print(f"  Account: [cyan]{payload.email}[/]")
        if payload.expires_at:
            console.print(f"  Token expires in: [cyan]{max(0, payload.expires_at - int(time.time())) // 60} min[/]")
        console.print(f"  Storage: [dim]{backend}[/]")
        if set_active:
            console.print("  Provider: [green]xai_oauth active[/]")
    except KeyboardInterrupt:
        console.print("\n[dim]cancelled[/]")
        raise typer.Exit(code=130)
    except xai_oauth.XAIEntitlementError as exc:
        console.print(f"[yellow]Authenticated, but not entitled:[/] {exc}")
        raise typer.Exit(code=3)
    except Exception as exc:
        console.print(f"[red]✗ xAI login failed:[/] {exc}")
        raise typer.Exit(code=1)


@xai_app.command("status")
def status() -> None:
    """Show xAI OAuth connection status."""
    payload = xai_oauth.load_token_payload()
    if payload is None:
        console.print("[yellow]xAI OAuth: not connected[/]")
        raise typer.Exit()
    expires = "unknown"
    if payload.expires_at:
        seconds = payload.expires_at - int(time.time())
        expires = "expired" if seconds <= 0 else f"in {seconds // 60} min"
    console.print("[green]xAI OAuth: connected[/]")
    if payload.email:
        console.print(f"  Account: [cyan]{payload.email}[/]")
    console.print(f"  Expires: {expires}")
    console.print(f"  Base URL: {payload.base_url}")
    from flowly.config.loader import load_config

    active = (load_config().providers.active or "").strip()
    console.print(f"  Active provider: {'xai_oauth' if active == 'xai_oauth' else active or 'cascade'}")


@xai_app.command("logout")
def logout() -> None:
    """Remove stored xAI OAuth tokens."""
    xai_oauth.clear_token_payload()
    try:
        from flowly.integrations.active_provider import clear_active_if_matches

        clear_active_if_matches("xai_oauth")
    except Exception:
        pass
    console.print("[green]✓[/] xAI OAuth tokens removed")


@xai_app.command("test")
def test(
    refresh: bool = typer.Option(False, "--refresh", help="Force token refresh before testing."),
) -> None:
    """Validate the stored OAuth token against xAI /models."""
    from flowly.config.loader import load_config

    try:
        creds = xai_oauth.resolve_runtime_credentials(config=load_config(), force_refresh=refresh)
        if creds is None:
            console.print("[red]✗ xAI OAuth is not connected[/]")
            raise typer.Exit(code=2)
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                f"{creds.base_url}/models",
                headers={
                    "Authorization": f"Bearer {creds.api_key}",
                    "Accept": "application/json",
                    "User-Agent": "flowly/xai-oauth-test",
                },
            )
        if response.status_code in (401, 403):
            console.print(f"[red]✗ xAI rejected token: HTTP {response.status_code}[/]")
            raise typer.Exit(code=1)
        if response.status_code >= 400:
            console.print(f"[red]✗ xAI /models failed: HTTP {response.status_code}[/]")
            raise typer.Exit(code=1)
        data = response.json()
        models = data.get("data") if isinstance(data, dict) else []
        count = len(models) if isinstance(models, list) else 0
        console.print(f"[green]✓[/] xAI OAuth works ({count} models)")
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"[red]✗ xAI test failed:[/] {exc}")
        raise typer.Exit(code=1)
