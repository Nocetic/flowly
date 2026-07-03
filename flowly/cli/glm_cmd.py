"""``flowly glm`` — Z.AI GLM Coding Plan commands."""

from __future__ import annotations

import httpx
import typer
from rich.console import Console
from rich.prompt import Prompt

from flowly.auth import zai_coding

glm_app = typer.Typer(help="Connect Z.AI GLM Coding Plan.")
console = Console()


def _enable_slot() -> None:
    from flowly.config.loader import load_config, save_config

    cfg = load_config()
    cfg.providers.zai_coding.enabled = True
    cfg.providers.zai_coding.api_base = zai_coding.DEFAULT_ZAI_CODING_BASE_URL
    save_config(cfg)


@glm_app.command("login")
def login(
    api_key: str = typer.Option("", "--api-key", help="GLM Coding Plan API key. If omitted, Flowly reuses OpenCode or prompts."),
    set_active: bool = typer.Option(True, "--set-active/--no-set-active", help="Use GLM Coding as active provider after login."),
) -> None:
    """Connect a Z.AI GLM Coding Plan key."""
    try:
        from flowly.config.loader import load_config
        from flowly.integrations.active_provider import set_active_provider

        key = api_key.strip()
        if not key:
            existing = zai_coding.resolve_runtime_credentials(config=load_config())
            if existing is not None and existing.api_key:
                _enable_slot()
                changed = set_active_provider("zai_coding") if set_active else None
                source = existing.source or "stored"
                if source == "opencode" and existing.provider_id:
                    source = f"OpenCode ({existing.provider_id})"
                console.print(f"[green]✓[/] GLM Coding Plan key found via {source}")
                if set_active:
                    note = f" · model → {changed}" if changed else ""
                    console.print(f"  Provider: [green]zai_coding active[/]{note}")
                return

            key = Prompt.ask("Paste your Z.AI GLM Coding Plan API key", password=True).strip()
        if not key:
            console.print("[yellow]No key entered — skipped.[/]")
            raise typer.Exit(code=2)

        backend = zai_coding.save_api_key(key)
        _enable_slot()
        changed = set_active_provider("zai_coding") if set_active else None
        console.print("\n[green]✓[/] Z.AI GLM Coding Plan connected")
        console.print(f"  Storage: [dim]{backend}[/]")
        if set_active:
            note = f" · model → {changed}" if changed else ""
            console.print(f"  Provider: [green]zai_coding active[/]{note}")
    except KeyboardInterrupt:
        console.print("\n[dim]cancelled[/]")
        raise typer.Exit(code=130)
    except Exception as exc:
        console.print(f"[red]✗ GLM Coding login failed:[/] {exc}")
        raise typer.Exit(code=1)


@glm_app.command("status")
def status() -> None:
    """Show GLM Coding Plan connection status."""
    payload = zai_coding.load_token_payload()
    if payload is None:
        console.print("[yellow]GLM Coding Plan: not connected[/]")
        raise typer.Exit()
    source = payload.source or "stored"
    if payload.source == "opencode" and payload.provider_id:
        source = f"OpenCode ({payload.provider_id})"
    console.print("[green]GLM Coding Plan: connected[/]")
    console.print(f"  Source: [cyan]{source}[/]")
    console.print(f"  Base URL: {payload.base_url}")
    from flowly.config.loader import load_config

    active = (load_config().providers.active or "").strip()
    console.print(f"  Active provider: {'zai_coding' if active == 'zai_coding' else active or 'cascade'}")


@glm_app.command("logout")
def logout() -> None:
    """Remove Flowly's stored GLM Coding Plan key.

    OpenCode credentials are read-only fallbacks and are left untouched.
    """
    before = zai_coding.load_token_payload()
    zai_coding.clear_token_payload()
    try:
        from flowly.integrations.active_provider import clear_active_if_matches

        clear_active_if_matches("zai_coding")
    except Exception:
        pass
    after = zai_coding.load_token_payload()
    console.print("[green]✓[/] Flowly GLM Coding Plan key removed")
    if before is not None and before.source != "flowly":
        console.print("[dim]The detected external key was not stored by Flowly and was left untouched.[/]")
    elif after is not None and after.source != "flowly":
        console.print("[dim]OpenCode/env GLM Coding key is still detectable as a fallback.[/]")


@glm_app.command("test")
def test() -> None:
    """Validate the configured key against Z.AI's OpenAI-compatible /models route."""
    from flowly.config.loader import load_config

    try:
        creds = zai_coding.resolve_runtime_credentials(config=load_config())
        if creds is None or not creds.api_key:
            console.print("[red]✗ GLM Coding Plan is not connected[/]")
            raise typer.Exit(code=2)
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                f"{creds.base_url}/models",
                headers={
                    "Authorization": f"Bearer {creds.api_key}",
                    "Accept": "application/json",
                    "User-Agent": "flowly/zai-coding-test",
                },
            )
        if response.status_code in (401, 403):
            console.print(f"[red]✗ Z.AI rejected key: HTTP {response.status_code}[/]")
            raise typer.Exit(code=1)
        if response.status_code >= 400:
            console.print(f"[red]✗ Z.AI /models failed: HTTP {response.status_code}[/]")
            raise typer.Exit(code=1)
        data = response.json()
        models = data.get("data") if isinstance(data, dict) else []
        count = len(models) if isinstance(models, list) else 0
        console.print(f"[green]✓[/] GLM Coding Plan works ({count} models)")
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"[red]✗ GLM Coding test failed:[/] {exc}")
        raise typer.Exit(code=1)
