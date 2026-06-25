"""``flowly mcp`` — manage MCP server entries in ``~/.flowly/config.json``.

Faz 1 commands:

* ``flowly mcp list``                                 — table of configured servers
* ``flowly mcp add <name> --command <cmd> ...``       — stdio server (probe-on-add)
* ``flowly mcp add <name> --url <url> ...``           — HTTP server (probe-on-add)
* ``flowly mcp remove <name>``                        — drop entry
* ``flowly mcp enable <name>`` / ``disable <name>``   — flip ``enabled``
* ``flowly mcp test <name>``                          — connect → list tools → disconnect

Writes go through :func:`flowly.config.loader.save_config`, which atomically
rotates ``config.json.bak``. ``${VAR}`` placeholders are written through
to the file unchanged; they resolve at agent boot from
``$FLOWLY_HOME/.env`` or the parent process env.

Faz 2 will add ``flowly mcp login`` for OAuth and ``flowly mcp configure``
for interactive include/exclude toggling. Faz 3 adds catalog / install /
serve.
"""

from __future__ import annotations

import os

import typer
from rich.console import Console
from rich.table import Table

from flowly.config.loader import (
    get_config_path,
    load_config,
    save_config,
)


console = Console()
mcp_app = typer.Typer(help="Manage MCP (Model Context Protocol) servers")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_with_servers():
    """Load config and return ``(config, servers_dict)`` for in-place edit."""
    config = load_config()
    return config, dict(config.mcp_servers or {})


def _save(config) -> None:
    save_config(config)


def _delete_server_on_disk(name: str) -> bool:
    """Remove ``mcpServers.{name}`` directly in the on-disk JSON.

    Necessary because :func:`save_config` deep-merges new values into
    existing fields, which preserves entries the in-memory ``Config``
    has dropped. Deletion is a structural change the merge can't express,
    so we operate on the JSON directly while keeping the atomic write
    contract that :func:`save_config` provides for everything else.
    """
    import json
    import os
    import secrets

    path = get_config_path()
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    if not servers:
        data.pop("mcpServers", None)

    tmp = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
    try:
        tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        tmp.unlink(missing_ok=True)
        return False
    try:
        from flowly.utils.file_security import secure_file
        secure_file(path)  # POSIX chmod; real owner-only ACL on Windows
    except OSError:
        pass
    return True


def _parse_env_assignments(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in items or []:
        text = (raw or "").strip()
        if not text:
            continue
        if "=" not in text:
            raise typer.BadParameter(f"--env value must be KEY=VALUE, got {text!r}")
        key, _, value = text.partition("=")
        key = key.strip()
        if not key:
            raise typer.BadParameter(f"--env value missing key in {text!r}")
        out[key] = value
    return out


def _parse_header_assignments(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in items or []:
        text = (raw or "").strip()
        if not text:
            continue
        if ":" not in text:
            raise typer.BadParameter(
                f"--header value must be 'Name: value', got {text!r}"
            )
        key, _, value = text.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            raise typer.BadParameter(f"--header missing name in {text!r}")
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@mcp_app.command("list")
def list_servers() -> None:
    """Show the configured MCP servers."""
    config = load_config()
    servers = config.mcp_servers or {}

    if not servers:
        console.print(
            "[dim]No MCP servers configured. Add one with "
            "`flowly mcp add <name> --command <cmd>` or "
            "`flowly mcp add <name> --url <url>`.[/dim]"
        )
        return

    table = Table(title="MCP servers")
    table.add_column("Name", style="cyan")
    table.add_column("Transport", style="yellow")
    table.add_column("Tools filter", style="green")
    table.add_column("Status")

    for name, cfg in servers.items():
        if cfg.url:
            transport = f"http: {cfg.url}"
        elif cfg.command:
            args_preview = " ".join(cfg.args[:3])
            transport = f"stdio: {cfg.command} {args_preview}".strip()
        else:
            transport = "[red]invalid (no command or url)[/red]"

        tools_filter = "all"
        if cfg.tools.include:
            tools_filter = f"{len(cfg.tools.include)} included"
        elif cfg.tools.exclude:
            tools_filter = f"-{len(cfg.tools.exclude)} excluded"

        status = "[green]enabled[/green]" if cfg.enabled else "[dim]disabled[/dim]"
        table.add_row(name, transport, tools_filter, status)

    console.print(table)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@mcp_app.command("add")
def add_server(
    name: str = typer.Argument(..., help="Unique server name (becomes tool prefix)"),
    command: str | None = typer.Option(
        None, "--command", help="stdio command (mutually exclusive with --url)",
    ),
    args: list[str] | None = typer.Option(
        None, "--arg", "-a", help="stdio arg, repeatable",
    ),
    env: list[str] | None = typer.Option(
        None, "--env", "-e", help="KEY=VALUE env var for stdio, repeatable",
    ),
    url: str | None = typer.Option(
        None, "--url", help="HTTP MCP endpoint (mutually exclusive with --command)",
    ),
    headers: list[str] | None = typer.Option(
        None, "--header", "-H", help="'Name: value' HTTP header, repeatable",
    ),
    timeout: float = typer.Option(
        120.0, "--timeout", help="Per-tool-call timeout in seconds",
    ),
    connect_timeout: float = typer.Option(
        60.0, "--connect-timeout", help="Initial connect timeout in seconds",
    ),
    auth: str = typer.Option(
        "", "--auth", help="Auth scheme for HTTP servers: 'oauth' for OAuth 2.1 PKCE",
    ),
    probe: bool = typer.Option(
        True, "--probe/--no-probe",
        help="Connect once and confirm tools are reachable",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite an existing entry without asking",
    ),
) -> None:
    """Register an MCP server. Probes by default; use --no-probe to skip."""
    if not command and not url:
        raise typer.BadParameter("Specify either --command or --url")
    if command and url:
        raise typer.BadParameter("--command and --url are mutually exclusive")
    if auth and auth != "oauth":
        raise typer.BadParameter("--auth only supports 'oauth'")
    if auth and not url:
        raise typer.BadParameter("--auth oauth requires --url (HTTP servers only)")

    config, servers = _config_with_servers()

    if name in servers and not force:
        if not typer.confirm(
            f"Server {name!r} already exists. Overwrite?", default=False,
        ):
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(code=1)

    try:
        env_map = _parse_env_assignments(env)
        header_map = _parse_header_assignments(headers)
    except typer.BadParameter as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)

    from flowly.config.schema import MCPServerConfig
    entry = MCPServerConfig(
        enabled=True,
        command=command or "",
        args=list(args or []),
        env=env_map,
        url=url or "",
        headers=header_map,
        timeout=timeout,
        connect_timeout=connect_timeout,
        auth=auth or "",
    )

    if probe:
        # OAuth servers need the interactive browser flow on first probe.
        ok, message = _probe(name, entry, interactive=bool(auth == "oauth"))
        if not ok:
            console.print(f"[red]Probe failed:[/red] {message}")
            if not typer.confirm(
                "Save the entry anyway (disabled)?", default=False,
            ):
                raise typer.Exit(code=1)
            entry.enabled = False
        else:
            console.print(f"[green]{message}[/green]")

    servers[name] = entry
    config.mcp_servers = servers
    _save(config)
    console.print(f"[green]Saved MCP server {name!r}.[/green]")


# ---------------------------------------------------------------------------
# remove / enable / disable
# ---------------------------------------------------------------------------


@mcp_app.command("remove")
def remove_server(
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Drop an MCP server entry from the config."""
    _, servers = _config_with_servers()
    if name not in servers:
        console.print(f"[red]Unknown MCP server {name!r}.[/red]")
        raise typer.Exit(code=1)
    if not yes and not typer.confirm(f"Remove MCP server {name!r}?", default=False):
        console.print("[dim]Cancelled.[/dim]")
        return
    if not _delete_server_on_disk(name):
        console.print(f"[red]Failed to remove MCP server {name!r} from disk.[/red]")
        raise typer.Exit(code=1)
    # Drop any stored OAuth tokens so a re-add starts clean.
    try:
        from flowly.mcp.oauth import clear_tokens
        if clear_tokens(name):
            console.print("[dim]Cleared stored OAuth tokens.[/dim]")
    except Exception:
        pass
    console.print(f"[green]Removed MCP server {name!r}.[/green]")


def _set_enabled(name: str, enabled: bool) -> None:
    config, servers = _config_with_servers()
    if name not in servers:
        console.print(f"[red]Unknown MCP server {name!r}.[/red]")
        raise typer.Exit(code=1)
    servers[name].enabled = enabled
    config.mcp_servers = servers
    _save(config)
    state = "enabled" if enabled else "disabled"
    console.print(f"[green]{name}: {state}.[/green]")


@mcp_app.command("enable")
def enable_server(name: str = typer.Argument(...)) -> None:
    """Mark an MCP server enabled (it will load at next agent boot)."""
    _set_enabled(name, True)


@mcp_app.command("disable")
def disable_server(name: str = typer.Argument(...)) -> None:
    """Mark an MCP server disabled (no connect attempt at boot)."""
    _set_enabled(name, False)


# ---------------------------------------------------------------------------
# configure — interactively toggle which tools a server exposes
# ---------------------------------------------------------------------------


def _current_selection(entry, tool_names: list[str]) -> set[str]:
    """Tools currently enabled for *entry* given its include/exclude + the
    server's actual tool list. include wins; else all-minus-exclude."""
    include = set(entry.tools.include or [])
    exclude = set(entry.tools.exclude or [])
    if include:
        return {t for t in tool_names if t in include}
    return {t for t in tool_names if t not in exclude}


def _apply_tool_selection(entry, tool_names: list[str], chosen: set[str]) -> None:
    """Write *chosen* onto *entry*'s tool filter.

    All selected → clear the filter (register everything). Otherwise pin
    ``tools.include`` to the chosen names (preserving server order) and
    clear ``tools.exclude`` so the two never fight.
    """
    if set(chosen) == set(tool_names):
        entry.tools.include = []
        entry.tools.exclude = []
    else:
        entry.tools.include = [t for t in tool_names if t in chosen]
        entry.tools.exclude = []


@mcp_app.command("configure")
def configure(
    name: str = typer.Argument(..., help="Configured server name"),
) -> None:
    """Pick which of a server's tools are enabled (interactive checklist).

    Connects to the server, lists its tools, and lets you toggle each on
    or off. Saves the selection as the server's ``tools.include`` list
    (or clears the filter when all are selected). Start a new session for
    changes to take effect.
    """
    import sys as _sys
    if not _sys.stdin.isatty():
        console.print("[red]`flowly mcp configure` needs an interactive terminal.[/red]")
        raise typer.Exit(code=1)

    config, servers = _config_with_servers()
    if name not in servers:
        console.print(f"[red]Unknown MCP server {name!r}.[/red]")
        raise typer.Exit(code=1)
    entry = servers[name]

    console.print(f"[cyan]Connecting to {name!r} to list tools...[/cyan]")
    ok, tool_names, error = _probe_tool_names(
        name, entry, interactive=(entry.auth == "oauth"),
    )
    if not ok:
        console.print(f"[red]Failed to connect:[/red] {error}")
        raise typer.Exit(code=1)
    if not tool_names:
        console.print("[yellow]Server reports no tools — nothing to configure.[/yellow]")
        return

    pre_selected = _current_selection(entry, tool_names)

    try:
        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice
    except ImportError:
        console.print("[red]InquirerPy not available.[/red]")
        raise typer.Exit(code=1)

    choices = [
        Choice(value=t, name=t, enabled=(t in pre_selected)) for t in tool_names
    ]
    chosen = inquirer.checkbox(
        message=f"Enable which tools for {name!r}? (space toggles, enter confirms)",
        choices=choices,
        instruction="↑↓ move · space toggle · enter save",
    ).execute()

    if chosen is None:
        console.print("[dim]Cancelled.[/dim]")
        return
    if set(chosen) == set(pre_selected):
        console.print("[dim]No changes.[/dim]")
        return

    _apply_tool_selection(entry, tool_names, set(chosen))
    servers[name] = entry
    config.mcp_servers = servers
    _save(config)
    enabled_n = len(entry.tools.include) or len(tool_names)
    console.print(
        f"[green]Saved: {enabled_n}/{len(tool_names)} tool(s) enabled for {name!r}.[/green]"
    )
    console.print("[dim]Start a new session for changes to take effect.[/dim]")


# ---------------------------------------------------------------------------
# serve — expose Flowly itself as an MCP server (Faz 3, M1)
# ---------------------------------------------------------------------------


@mcp_app.command("serve")
def serve(
    allow_writes: bool = typer.Option(
        False, "--allow-writes",
        help="Expose send + approval-resolve tools (needs a running gateway)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging"),
) -> None:
    """Run Flowly as an MCP server on stdio.

    External MCP clients (Claude Desktop, Cursor, another agent) can read
    your Flowly conversation history. Read tools work standalone; write
    tools (--allow-writes) require the gateway to be running.

    This serves on stdio — point your MCP client at:
        flowly mcp serve
    """
    from flowly.mcp.server import run_server
    run_server(allow_writes=allow_writes, verbose=verbose)


# ---------------------------------------------------------------------------
# catalog / install / picker (Faz 3, M2/M3)
# ---------------------------------------------------------------------------


@mcp_app.command("catalog")
def catalog() -> None:
    """List the curated, ready-to-install MCP servers shipped with Flowly."""
    from flowly.mcp.catalog import load_catalog

    entries = load_catalog()
    if not entries:
        console.print("[dim]No catalog entries found.[/dim]")
        return

    table = Table(title="MCP catalog")
    table.add_column("Name", style="cyan")
    table.add_column("Auth", style="yellow")
    table.add_column("Transport", style="green")
    table.add_column("Description")
    for entry in entries.values():
        table.add_row(
            entry.name, entry.auth_type, entry.transport_summary(), entry.description,
        )
    console.print(table)
    console.print("\n[dim]Install one with: flowly mcp install <name>[/dim]")


def _install_entry(name: str, *, force: bool, probe: bool) -> None:
    """Shared install logic for `install` and the picker."""
    from flowly.mcp.catalog import get_entry, build_server_config
    from flowly.mcp.env_loader import save_env_value

    entry = get_entry(name)
    if entry is None:
        console.print(f"[red]Unknown catalog entry {name!r}.[/red]")
        raise typer.Exit(code=1)

    config, servers = _config_with_servers()
    if name in servers and not force:
        if not typer.confirm(f"Server {name!r} already configured. Overwrite?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            return

    # Prompt for declared env vars → persist to $FLOWLY_HOME/.env
    for spec in entry.env:
        existing = os.environ.get(spec.name)
        if existing:
            console.print(f"[dim]{spec.name}: already set, keeping.[/dim]")
            continue
        value = typer.prompt(
            f"  {spec.prompt}",
            default=spec.default or None,
            hide_input=spec.secret,
            show_default=not spec.secret,
        )
        if value:
            save_env_value(spec.name, value)
            console.print(f"  [green]Saved {spec.name} to $FLOWLY_HOME/.env[/green]")

    from flowly.config.schema import MCPServerConfig
    cfg_dict = build_server_config(entry)
    entry_cfg = MCPServerConfig(**cfg_dict)

    # OAuth servers can't be probed non-interactively; probe others.
    if probe and entry.auth_type != "oauth":
        ok, message = _probe(name, entry_cfg)
        if ok:
            console.print(f"[green]{message}[/green]")
        else:
            console.print(f"[red]Probe failed:[/red] {message}")
            if not typer.confirm("Save anyway (disabled)?", default=False):
                raise typer.Exit(code=1)
            entry_cfg.enabled = False

    servers[name] = entry_cfg
    config.mcp_servers = servers
    _save(config)
    console.print(f"[green]Installed MCP server {name!r}.[/green]")
    if entry.post_install:
        console.print(f"\n[dim]{entry.post_install.strip()}[/dim]")
    if entry.auth_type == "oauth":
        console.print(f"\n[yellow]Next:[/yellow] flowly mcp login {name}")


@mcp_app.command("install")
def install(
    name: str = typer.Argument(..., help="Catalog entry name (see `flowly mcp catalog`)"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing entry"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Verify connection on install"),
) -> None:
    """Install a curated MCP server from the catalog."""
    _install_entry(name, force=force, probe=probe)


@mcp_app.command("picker")
def picker() -> None:
    """Interactively browse the catalog and install a server."""
    import sys as _sys
    if not _sys.stdin.isatty():
        console.print("[red]The picker needs an interactive terminal.[/red]")
        raise typer.Exit(code=1)

    from flowly.mcp.catalog import load_catalog

    entries = load_catalog()
    if not entries:
        console.print("[dim]No catalog entries found.[/dim]")
        return

    _, configured = _config_with_servers()
    try:
        from InquirerPy import inquirer
    except ImportError:
        console.print("[red]InquirerPy not available; use `flowly mcp install <name>`.[/red]")
        raise typer.Exit(code=1)

    choices = []
    for entry in entries.values():
        mark = " [installed]" if entry.name in configured else ""
        choices.append({
            "name": f"{entry.name}{mark} — {entry.description}",
            "value": entry.name,
        })

    selected = inquirer.select(
        message="Select an MCP server to install (Ctrl+C to cancel):",
        choices=choices,
    ).execute()

    if not selected:
        console.print("[dim]Cancelled.[/dim]")
        return
    _install_entry(selected, force=True, probe=True)


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


@mcp_app.command("test")
def test_server(
    name: str = typer.Argument(...),
) -> None:
    """Connect to a configured MCP server and show its tool list."""
    config, servers = _config_with_servers()
    if name not in servers:
        console.print(f"[red]Unknown MCP server {name!r}.[/red]")
        raise typer.Exit(code=1)
    entry = servers[name]
    # OAuth servers may need the browser flow if no valid token is cached.
    ok, message = _probe(name, entry, interactive=(entry.auth == "oauth"))
    if ok:
        console.print(f"[green]{message}[/green]")
    else:
        console.print(f"[red]{message}[/red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# login (OAuth re-authentication)
# ---------------------------------------------------------------------------


@mcp_app.command("login")
def login_server(
    name: str = typer.Argument(...),
) -> None:
    """Run (or re-run) the OAuth flow for an OAuth-configured HTTP server.

    Clears any cached tokens first so a stuck/expired grant is replaced
    by a fresh browser authorization.
    """
    config, servers = _config_with_servers()
    if name not in servers:
        console.print(f"[red]Unknown MCP server {name!r}.[/red]")
        raise typer.Exit(code=1)
    entry = servers[name]
    if not entry.url:
        console.print(f"[red]Server {name!r} is stdio — OAuth applies to HTTP servers.[/red]")
        raise typer.Exit(code=1)
    if entry.auth != "oauth":
        console.print(
            f"[red]Server {name!r} is not configured for OAuth.[/red] "
            "Re-add it with --auth oauth."
        )
        raise typer.Exit(code=1)

    try:
        from flowly.mcp.oauth import clear_tokens, oauth_available
    except Exception as exc:
        console.print(f"[red]OAuth runtime not importable: {exc}[/red]")
        raise typer.Exit(code=1)
    if not oauth_available():
        console.print("[red]This 'mcp' SDK build lacks OAuth support — upgrade the package.[/red]")
        raise typer.Exit(code=1)

    clear_tokens(name)
    console.print(f"[cyan]Starting OAuth flow for {name!r}...[/cyan]")
    ok, message = _probe(name, entry, interactive=True)
    if ok:
        console.print(f"[green]Authenticated. {message}[/green]")
    else:
        console.print(f"[red]Authentication failed: {message}[/red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Probe helper (shared by `add --probe` and `test`)
# ---------------------------------------------------------------------------


def _probe_tool_names(
    name: str, entry, *, interactive: bool = False,
) -> tuple[bool, list[str], str]:
    """Connect once to *entry* and return ``(ok, tool_names, error)``.

    Thin wrapper over :func:`flowly.mcp.probe.probe_tool_names` (shared with the
    feature-RPC ``mcp.test`` method) — *entry* is an ``MCPServerConfig``.
    """
    from flowly.mcp.probe import probe_tool_names
    return probe_tool_names(name, entry.model_dump(), interactive=interactive)


def _probe(name: str, entry, *, interactive: bool = False) -> tuple[bool, str]:
    """Connect once and return ``(ok, human_message)`` with a tool preview."""
    from flowly.mcp.probe import probe_message
    return probe_message(name, entry.model_dump(), interactive=interactive)
