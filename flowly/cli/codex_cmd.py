"""``flowly codex`` — manage the Codex app-server runtime (opt-in).

The ``codex_session`` tool hands coding-heavy turns to OpenAI's
``codex app-server`` subprocess: Codex runs its own sandboxed
``shell`` / ``apply_patch`` tools, approvals route through Flowly's
prompt flow, its item stream is projected back into the chat, and —
with the Flowly tool callback — Codex can reach back into Flowly's
web/skills tools over MCP.

This group flips ``tools.codex_session.enabled`` in config and (on
enable) registers the Flowly tool-callback MCP server in
``~/.codex/config.toml``. It also verifies the ``codex`` CLI is
installed so enabling never silently fails on the first turn.
"""

from __future__ import annotations

import shutil
import subprocess

import typer
from rich.console import Console

from flowly.config.loader import load_config, save_config

codex_app = typer.Typer(help="Manage the Codex app-server runtime (opt-in).")
console = Console()

_MIN_CODEX = (0, 125, 0)


def _check_codex_binary(codex_bin: str = "codex") -> tuple[bool, str]:
    """Return (ok, version_or_message) for the codex CLI."""
    path = shutil.which(codex_bin)
    if path is None:
        return False, f"{codex_bin!r} not found on PATH"
    try:
        proc = subprocess.run(
            [codex_bin, "--version"], capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"could not run `{codex_bin} --version`: {exc}"
    import re
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", proc.stdout or "")
    if not m:
        return False, f"could not parse version from {proc.stdout!r}"
    ver = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    dotted = ".".join(map(str, ver))
    if ver < _MIN_CODEX:
        return False, f"codex {dotted} < required {'.'.join(map(str, _MIN_CODEX))}"
    return True, dotted


@codex_app.command("enable")
def enable(
    sandbox: str = typer.Option(
        "", "--sandbox",
        help="Sandbox level: read-only | workspace-write | full-access.",
    ),
    expose_tools: bool = typer.Option(
        True, "--expose-tools/--no-expose-tools",
        help="Register Flowly's tool callback so Codex can use web/skills.",
    ),
) -> None:
    """Enable the codex_session tool (delegates coding turns to Codex)."""
    ok, info = _check_codex_binary()
    if not ok:
        console.print(f"[red]Cannot enable:[/] {info}")
        console.print("Install with: [cyan]npm i -g @openai/codex[/]  then  [cyan]codex login[/]")
        raise typer.Exit(1)

    cfg = load_config()
    cfg.tools.codex_session.enabled = True
    cfg.tools.codex_session.expose_flowly_tools = expose_tools
    if sandbox:
        if sandbox not in ("read-only", "workspace-write", "full-access"):
            console.print(f"[red]Invalid --sandbox {sandbox!r}[/] (read-only | workspace-write | full-access)")
            raise typer.Exit(1)
        cfg.tools.codex_session.sandbox = sandbox
    save_config(cfg)

    console.print(f"[green]✓[/] codex_session enabled — codex CLI {info}")
    console.print(f"  sandbox: {cfg.tools.codex_session.sandbox}")

    if expose_tools:
        try:
            from flowly.codex.tool_migration import (
                _sandbox_to_permission,
                migrate_flowly_tools_to_codex,
            )
            # CLI path: full migration — callback + the user's MCP servers +
            # permission profile + live codex plugin discovery.
            path = migrate_flowly_tools_to_codex(
                codex_home=cfg.tools.codex_session.codex_home or None,
                config=cfg,
                default_permissions=_sandbox_to_permission(cfg.tools.codex_session.sandbox),
                discover_plugins=True,
            )
            n_servers = len(getattr(cfg, "mcp_servers", None) or {})
            console.print(f"  Flowly tool callback registered in {path}")
            console.print("    (codex turns can use web_search, web_fetch, web_extract, video_analyze, skill_view, skills_list)")
            if n_servers:
                console.print(f"    + migrated {n_servers} of your MCP server(s) into codex")
            console.print(f"    sandbox profile: {_sandbox_to_permission(cfg.tools.codex_session.sandbox)}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [yellow]![/] tool-callback registration skipped: {exc}")

    console.print("Takes effect on the next session / gateway restart.")


@codex_app.command("disable")
def disable() -> None:
    """Disable the codex_session tool (back to Flowly's own runtime)."""
    cfg = load_config()
    cfg.tools.codex_session.enabled = False
    save_config(cfg)
    console.print("[green]✓[/] codex_session disabled. Takes effect on the next session.")
    console.print("  (The flowly-tools block stays in ~/.codex/config.toml so re-enabling is instant.)")


@codex_app.command("cwd")
def cwd(path: str = typer.Argument("", help="Working directory for codex (empty = show current)")) -> None:
    """Set (or show) the working directory codex runs in. Persistent."""
    import os
    cfg = load_config()
    if not path:
        cur = cfg.tools.codex_session.cwd or "(gateway launch dir)"
        console.print(f"codex working dir: {cur}")
        return
    expanded = os.path.abspath(os.path.expanduser(path))
    cfg.tools.codex_session.cwd = expanded
    save_config(cfg)
    warn = "" if os.path.isdir(expanded) else "  [yellow](directory doesn't exist yet)[/]"
    console.print(f"[green]✓[/] codex working dir set to {expanded}{warn}")
    console.print("  Takes effect on the next codex turn / gateway restart.")


@codex_app.command("status")
def status() -> None:
    """Show whether the codex_session runtime is enabled + codex CLI health."""
    cfg = load_config()
    c = cfg.tools.codex_session
    ok, info = _check_codex_binary(c.codex_bin or "codex")
    console.print(f"codex_session enabled : {c.enabled}")
    console.print(f"sandbox               : {c.sandbox}")
    console.print(f"working dir           : {c.cwd or '(gateway launch dir)'}")
    console.print(f"approval policy       : {c.approval_policy}")
    console.print(f"expose Flowly tools   : {c.expose_flowly_tools}")
    console.print(f"codex CLI             : {'OK ' + info if ok else 'NOT available — ' + info}")
    if not ok:
        console.print("  Install: [cyan]npm i -g @openai/codex[/]  then  [cyan]codex login[/]")

    # ChatGPT subscription (provider auth) — separate from the tool above.
    from flowly.auth import openai_codex
    payload = openai_codex.load_token_payload()
    if payload is None:
        console.print("ChatGPT subscription  : not connected — run `flowly codex login`")
    else:
        who = payload.email or "connected"
        plan = f" · {payload.plan}" if payload.plan else ""
        console.print(f"ChatGPT subscription  : {who}{plan}")


@codex_app.command("login")
def login(
    device: bool = typer.Option(
        False, "--device", help="Use the device-code flow (headless / no browser)."
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the URL instead of opening a browser."
    ),
    manual_paste: bool = typer.Option(
        False, "--manual-paste", help="Paste the callback URL/code manually."
    ),
    set_active: bool = typer.Option(
        True, "--set-active/--no-set-active",
        help="Use the ChatGPT subscription as the active provider after login.",
    ),
    timeout_seconds: int = typer.Option(
        300, "--timeout", help="Seconds to wait for the loopback callback."
    ),
) -> None:
    """Sign in with ChatGPT (OpenAI Codex OAuth) to use your plan's GPT-5.x."""
    import time

    from flowly.auth import openai_codex

    client_id = openai_codex.require_client_id()
    try:
        if device:
            console.print("\nStarting device login…")
            payload = openai_codex.login_with_device_code(
                client_id=client_id,
                on_user_code=lambda code, url: console.print(
                    f"\nOpen [cyan]{url}[/] and enter code: [bold]{code}[/]\n"
                ),
            )
        else:
            console.print(
                f"\nWaiting for ChatGPT OAuth callback on "
                f"[cyan]{openai_codex.CODEX_OAUTH_REDIRECT_URI}[/]…"
            )
            if no_browser:
                console.print(
                    "[dim]On headless machines use --device (recommended) or --manual-paste.[/]"
                )
            payload = openai_codex.login_with_loopback(
                client_id=client_id,
                no_browser=no_browser,
                manual_paste=(
                    typer.prompt("Paste the final callback URL or code") if manual_paste else ""
                ),
                timeout_seconds=timeout_seconds,
                on_authorize_url=(
                    lambda url: console.print(f"\n[cyan]{url}[/cyan]\n") if no_browser else None
                ),
            )
        if set_active:
            from flowly.integrations.active_provider import set_active_provider

            changed = set_active_provider("openai_codex")
            if changed:
                console.print(f"  model → [cyan]{changed}[/]")
        console.print("\n[green]✓[/] ChatGPT subscription connected")
        if payload.email:
            console.print(f"  Account: [cyan]{payload.email}[/]")
        if payload.plan:
            console.print(f"  Plan: [cyan]{payload.plan}[/]")
        if payload.expires_at:
            mins = max(0, payload.expires_at - int(time.time())) // 60
            console.print(f"  Token expires in: [cyan]{mins} min[/]")
        if set_active:
            console.print("  Provider: [green]openai_codex active[/]")
    except KeyboardInterrupt:
        console.print("\n[dim]cancelled[/]")
        raise typer.Exit(code=130)
    except openai_codex.CodexEntitlementError as exc:
        console.print(f"[yellow]Authenticated, but this plan can't use Codex:[/] {exc}")
        raise typer.Exit(code=3)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ ChatGPT login failed:[/] {exc}")
        raise typer.Exit(code=1)


@codex_app.command("logout")
def logout() -> None:
    """Remove the stored ChatGPT subscription tokens (Flowly's own store)."""
    from flowly.auth import openai_codex

    openai_codex.clear_token_payload()
    try:
        from flowly.integrations.active_provider import clear_active_if_matches

        clear_active_if_matches("openai_codex")
    except Exception:
        pass
    console.print("[green]✓[/] ChatGPT subscription tokens removed")
    console.print(
        "  [dim](A `codex login` session in ~/.codex/auth.json, if any, is left untouched.)[/]"
    )
