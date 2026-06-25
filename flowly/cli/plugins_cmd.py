"""CLI commands — plugins_cmd.

Subcommands:

* ``flowly plugins list``                   — show all discovered plugins
* ``flowly plugins install <git-or-path>``  — install from git URL,
  ``owner/repo`` shorthand, or local directory
* ``flowly plugins enable <name>``          — add to plugins.enabled
* ``flowly plugins disable <name>``         — add to plugins.disabled
* ``flowly plugins remove <name>``          — delete from disk
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from flowly.config.loader import load_config, save_config
from flowly.profile import get_flowly_home

console = Console()
plugins_app = typer.Typer(help="Manage plugins")


# ── Helpers ────────────────────────────────────────────────────


_GITHUB_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
# Three-or-more-segment monorepo form, e.g. ``Nocetic/plugins/figma`` or
# ``Nocetic/plugins/category/figma``. Owner + repo are always the first
# two segments; everything else is the in-repo path to the plugin dir.
_GITHUB_MONOREPO_RE = re.compile(r"^([\w.-]+)/([\w.-]+)/([\w./\-]+)$")


def _user_plugins_dir() -> Path:
    path = get_flowly_home() / "plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_install_source(source: str) -> tuple[str, str, bool]:
    """Resolve the install source.

    Returns ``(value, subpath, is_local)``:

    * ``value`` is the git URL to clone, or the local directory to copy
      from.
    * ``subpath`` is the in-repo path to the plugin's manifest folder
      when the user pointed at a monorepo entry (e.g. ``Nocetic/plugins/figma``
      or ``https://github.com/Nocetic/plugins.git#figma``). Empty
      string for single-plugin sources / local paths.
    * ``is_local`` selects between filesystem copy and git clone.
    """
    # ``source#subpath`` form — works for both owner/repo and full URLs.
    # We slice the fragment off before anything else so the rest of the
    # function only ever sees the bare source.
    bare, _, fragment_path = source.partition("#")
    fragment_path = fragment_path.strip("/")

    candidate = Path(bare).expanduser()
    if candidate.exists() and candidate.is_dir():
        # Local paths can't carry a fragment; ignore silently if one
        # was passed so the user isn't blocked over a cosmetic mistake.
        return str(candidate.resolve()), "", True

    if bare.startswith(("http://", "https://", "git@", "ssh://")):
        if bare.startswith("http://"):
            console.print(
                "[yellow]warning:[/] http:// is insecure — prefer https://"
            )
        return bare, fragment_path, False

    if _GITHUB_REPO_RE.match(bare):
        return f"https://github.com/{bare}.git", fragment_path, False

    # ``owner/repo/path`` monorepo shorthand — first two segments name the
    # repo, the rest is the plugin's subdirectory. Wins over the bare
    # owner/repo check above only when there are 3+ segments.
    match = _GITHUB_MONOREPO_RE.match(bare)
    if match:
        owner, repo, subpath = match.group(1), match.group(2), match.group(3)
        if fragment_path:
            # Ambiguous: both `owner/repo/path` AND `#path` given. Reject
            # rather than silently picking one — it's almost certainly a
            # copy/paste error.
            raise typer.BadParameter(
                f"Cannot resolve {source!r} — specify the plugin path "
                f"either via slash form (owner/repo/path) OR fragment "
                f"form (owner/repo#path), not both."
            )
        return f"https://github.com/{owner}/{repo}.git", subpath, False

    raise typer.BadParameter(
        f"Cannot resolve {source!r} — provide a git URL, owner/repo "
        f"shorthand (optionally followed by /subpath for monorepos), "
        f"or a path to a local plugin directory."
    )


def _sanitise_plugin_name(name: str, plugins_dir: Path) -> Path:
    """Validate plugin name and return its target path inside *plugins_dir*."""
    if not name or name in (".", ".."):
        raise typer.BadParameter(f"Invalid plugin name: {name!r}")
    for bad in ("/", "\\", ".."):
        if bad in name:
            raise typer.BadParameter(
                f"Invalid plugin name {name!r}: must not contain {bad!r}"
            )
    target = (plugins_dir / name).resolve()
    plugins_resolved = plugins_dir.resolve()
    try:
        target.relative_to(plugins_resolved)
    except ValueError:
        raise typer.BadParameter(
            f"Invalid plugin name {name!r}: resolves outside plugins dir"
        )
    return target


def _read_manifest_name(plugin_dir: Path) -> str | None:
    """Read the ``name`` field from a plugin's manifest (yaml/json)."""
    from flowly.plugins.manifest import find_manifest, parse_manifest
    manifest_file = find_manifest(plugin_dir)
    if manifest_file is None:
        return None
    manifest = parse_manifest(manifest_file, plugin_dir, source="user")
    return manifest.name if manifest else None


def _load_plugin_manager_for_listing():
    """Build a fresh PluginManager for read-only inspection.

    The CLI runs in a separate process from the agent, so we construct
    isolated registries here — never wired into a live AgentLoop.
    """
    from flowly.agent.hooks import HookRegistry
    from flowly.agent.tools.registry import ToolRegistry
    from flowly.plugins import PluginManager

    return PluginManager(
        tool_registry=ToolRegistry(),
        hook_registry=HookRegistry(),
    )


# ── Commands ───────────────────────────────────────────────────


@plugins_app.command("list")
def list_cmd(
    json_output: bool = typer.Option(
        False, "--json", help="Output as JSON for machine consumption."
    ),
):
    """List discovered plugins (bundled + user + project)."""
    cfg = load_config()
    enabled = set(cfg.plugins.enabled)
    disabled = set(cfg.plugins.disabled)

    mgr = _load_plugin_manager_for_listing()
    mgr.discover_and_load(enabled=enabled, disabled=disabled)

    plugins = mgr.list_plugins()

    if json_output:
        # Derive a status string the desktop UI can render directly,
        # mirroring the rich-table classification logic below.
        import json as _json
        out = []
        for p in plugins:
            if p["enabled"]:
                status = "enabled"
            elif p["error"] and "disabled" in p["error"]:
                status = "disabled"
            elif p["error"] and "not in plugins.enabled" in p["error"]:
                status = "available"
            else:
                status = "error"
            out.append({**p, "status": status})
        print(_json.dumps(out))
        return

    if not plugins:
        console.print("[yellow]No plugins discovered.[/]")
        console.print(
            "\n[dim]Install one with: "
            "flowly plugins install <git-url>[/]"
        )
        return

    table = Table(title="Plugins")
    table.add_column("Key", style="cyan")
    table.add_column("Version")
    table.add_column("Source", style="dim")
    table.add_column("Status")
    table.add_column("Description")

    for p in plugins:
        if p["enabled"]:
            status = "[green]enabled[/]"
        elif p["error"] and "disabled" in p["error"]:
            status = "[dim]disabled[/]"
        elif p["error"] and "not in plugins.enabled" in p["error"]:
            status = "[yellow]available[/]"
        else:
            status = f"[red]error[/]"
        desc = p["description"] or ""
        if len(desc) > 60:
            desc = desc[:57] + "..."
        table.add_row(
            p["key"], p["version"] or "—", p["source"], status, desc,
        )

    console.print(table)


@plugins_app.command("install")
def install_cmd(
    source: str = typer.Argument(
        ...,
        help=(
            "Git URL, owner/repo shorthand, owner/repo/subpath for "
            "monorepos (e.g. Nocetic/plugins/figma), or local directory "
            "path."
        ),
    ),
    enable: bool = typer.Option(
        True, "--enable/--no-enable",
        help="Enable the plugin after install (default: yes)",
    ),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Overwrite if plugin already exists",
    ),
):
    """Install a plugin from a git URL, owner/repo, owner/repo/subpath, or local path."""
    plugins_dir = _user_plugins_dir()
    resolved, subpath, is_local = _resolve_install_source(source)

    # Stage 1 — fetch into a temp dir we can inspect for the manifest.
    tmp_dir = plugins_dir / f".staging-{Path(resolved).name}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)

    try:
        if is_local:
            console.print(f"[cyan]Copying[/] from {resolved}")
            shutil.copytree(resolved, tmp_dir)
        else:
            console.print(
                f"[cyan]Cloning[/] {resolved}" +
                (f" (subpath: {subpath})" if subpath else "")
            )
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", resolved, str(tmp_dir)],
                    check=True, capture_output=True, text=True,
                )
            except subprocess.CalledProcessError as exc:
                console.print(f"[red]✗ git clone failed:[/]\n{exc.stderr}")
                raise typer.Exit(1)

        # Stage 1b — narrow to the monorepo subpath if one was given. We
        # keep the cloned root in ``tmp_dir`` for cleanup at the end; the
        # ``source_dir`` is what we actually scan for the manifest and
        # ultimately rename into place.
        if subpath:
            # Guard against ``..`` and absolute-style escapes from the
            # user-supplied subpath, which would otherwise let a crafted
            # input read outside the cloned repo.
            for segment in Path(subpath).parts:
                if segment in (".", "..") or segment.startswith("/"):
                    console.print(
                        f"[red]✗ invalid subpath {subpath!r}:[/] segments cannot be '.', '..', or absolute."
                    )
                    raise typer.Exit(1)
            source_dir = (tmp_dir / subpath).resolve()
            tmp_resolved = tmp_dir.resolve()
            try:
                source_dir.relative_to(tmp_resolved)
            except ValueError:
                console.print(
                    f"[red]✗ subpath {subpath!r} escapes the cloned repo[/]"
                )
                raise typer.Exit(1)
            if not source_dir.exists() or not source_dir.is_dir():
                console.print(
                    f"[red]✗ subpath {subpath!r} not found in {resolved}[/]"
                )
                raise typer.Exit(1)
        else:
            source_dir = tmp_dir

        # Stage 2 — read manifest name to know the final directory.
        name = _read_manifest_name(source_dir)
        if name is None:
            where = f"{subpath}/" if subpath else "the source root"
            console.print(
                f"[red]✗ no plugin.yaml/yml/json found at {where}[/]"
            )
            raise typer.Exit(1)

        target = _sanitise_plugin_name(name, plugins_dir)
        if target.exists():
            if not force:
                console.print(
                    f"[red]✗ plugin {name!r} already installed at {target}[/]"
                )
                console.print("[dim]Use --force to overwrite.[/]")
                raise typer.Exit(1)
            shutil.rmtree(target)

        # Stage 3 — move just the plugin's directory to its final home.
        # For monorepo installs we copy from the subpath (keeping the
        # rest of the clone in tmp_dir for the cleanup pass); for the
        # single-plugin case we can just rename the whole stage.
        if subpath:
            shutil.copytree(source_dir, target)
        else:
            tmp_dir.rename(target)
        console.print(f"[green]✓[/] installed [cyan]{name}[/] → {target}")

        if enable:
            _modify_enabled(name, add=True)
            console.print(f"[green]✓[/] enabled [cyan]{name}[/]")
        else:
            console.print(
                f"[dim]Run `flowly plugins enable {name}` to activate.[/]"
            )
        console.print(
            "[dim]Restart flowly (gateway/agent) for changes to apply.[/]"
        )
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


@plugins_app.command("enable")
def enable_cmd(name: str = typer.Argument(..., help="Plugin name or key")):
    """Enable a plugin (add to plugins.enabled)."""
    _modify_enabled(name, add=True)
    _modify_disabled(name, add=False)
    console.print(f"[green]✓[/] enabled [cyan]{name}[/]")
    console.print("[dim]Restart flowly for changes to apply.[/]")


@plugins_app.command("disable")
def disable_cmd(name: str = typer.Argument(..., help="Plugin name or key")):
    """Disable a plugin (add to plugins.disabled, remove from enabled)."""
    _modify_disabled(name, add=True)
    _modify_enabled(name, add=False)
    console.print(f"[yellow]✓[/] disabled [cyan]{name}[/]")
    console.print("[dim]Restart flowly for changes to apply.[/]")


@plugins_app.command("remove")
def remove_cmd(
    name: str = typer.Argument(..., help="Plugin name to uninstall"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt",
    ),
):
    """Uninstall a plugin (deletes its directory under $FLOWLY_HOME/plugins/)."""
    plugins_dir = _user_plugins_dir()
    target = _sanitise_plugin_name(name, plugins_dir)
    if not target.exists():
        console.print(f"[red]✗ no plugin {name!r} at {target}[/]")
        raise typer.Exit(1)

    if not yes:
        confirm = typer.confirm(
            f"Delete {target}?", default=False,
        )
        if not confirm:
            console.print("[dim]Aborted.[/]")
            raise typer.Exit(0)

    shutil.rmtree(target)
    _modify_enabled(name, add=False)
    _modify_disabled(name, add=False)
    console.print(f"[green]✓[/] removed [cyan]{name}[/]")


# ── Config mutation helpers ────────────────────────────────────


def _modify_enabled(name: str, *, add: bool) -> None:
    cfg = load_config()
    enabled = list(cfg.plugins.enabled)
    if add:
        if name not in enabled:
            enabled.append(name)
    else:
        enabled = [x for x in enabled if x != name]
    cfg.plugins.enabled = enabled
    save_config(cfg)


def _modify_disabled(name: str, *, add: bool) -> None:
    cfg = load_config()
    disabled = list(cfg.plugins.disabled)
    if add:
        if name not in disabled:
            disabled.append(name)
    else:
        disabled = [x for x in disabled if x != name]
    cfg.plugins.disabled = disabled
    save_config(cfg)
