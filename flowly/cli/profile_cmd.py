"""Manage isolated local agent profiles."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import typer
from rich.console import Console

from flowly.profile import (
    create_profile,
    delete_profile,
    describe_profile,
    export_profile,
    get_active_profile,
    import_profile,
    list_profiles,
    read_profile_settings,
    update_profile_metadata,
    update_profile_settings,
)

profile_app = typer.Typer(help="Manage isolated local agent profiles")
console = Console()
_MAX_SOUL_OPTION_BYTES = 64 * 1024


def _emit(value: dict | list, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    else:
        console.print_json(data=value)


def _fail(exc: Exception) -> None:
    raise typer.BadParameter(str(exc)) from exc


def _resolve_soul_option(soul: str | None, soul_file: str | None) -> str | None:
    if soul is not None and soul_file is not None:
        raise ValueError("Use either --soul or --soul-file, not both.")
    if soul_file is None:
        return soul
    path = Path(soul_file)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SOUL_OPTION_BYTES:
            raise ValueError("Persona instruction file is invalid or too large.")
        if os.name != "nt" and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("Persona file must use 0600 permissions.")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError("Persona file must be owned by this user.")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = handle.read(_MAX_SOUL_OPTION_BYTES + 1)
            if len(value.encode("utf-8")) > _MAX_SOUL_OPTION_BYTES:
                raise ValueError("Persona instruction file is invalid or too large.")
            return value
    except UnicodeDecodeError as exc:
        raise ValueError("Persona instruction file is not valid UTF-8.") from exc
    finally:
        if fd >= 0:
            os.close(fd)


@profile_app.command("list")
def profile_list(
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List the default profile and every named profile."""
    _emit({"profiles": [profile.to_dict() for profile in list_profiles()]}, json_output)


@profile_app.command("describe")
def profile_describe(
    name: str = typer.Argument(..., help="Profile identifier."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show one profile descriptor."""
    try:
        profile = describe_profile(name)
    except (ValueError, FileNotFoundError) as exc:
        _fail(exc)
    _emit({"profile": profile.to_dict()}, json_output)


@profile_app.command("create")
def profile_create(
    name: str = typer.Argument(..., help="Lowercase profile identifier."),
    clone: bool = typer.Option(False, "--clone", help="Copy the active profile's configuration."),
    clone_from: str = typer.Option("", "--clone-from", help="Profile whose configuration should be copied."),
    clone_all: bool = typer.Option(False, "--clone-all", help="Also copy sessions and memory."),
    display_name: str = typer.Option("", "--display-name", help="Name shown in clients."),
    description: str = typer.Option("", "--description", help="Short purpose shown in clients."),
    provider: str | None = typer.Option(None, "--provider", help="Profile-local active model provider."),
    model: str | None = typer.Option(None, "--model", help="Profile-local default model."),
    soul: str | None = typer.Option(None, "--soul", help="Profile-local SOUL.md contents."),
    soul_file: str | None = typer.Option(None, "--soul-file", hidden=True),
    mark_text: str = typer.Option("", "--mark-text", help="One or two characters shown in Desktop."),
    mark_tone: str = typer.Option("", "--mark-tone", help="Desktop signature color."),
    local_only: bool = typer.Option(
        False,
        "--local-only",
        help="Disable cloned messaging transports and remove relay identity.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Create a profile with its own config, workspace, memory and sessions."""
    source = clone_from.strip() or (get_active_profile() if clone or clone_all else None)
    try:
        create_profile(
            name,
            clone_from=source,
            clone_all=clone_all,
            display_name=display_name,
            description=description,
            local_runtime=local_only,
            provider=provider,
            model=model,
            soul=_resolve_soul_option(soul, soul_file),
            mark_text=mark_text,
            mark_tone=mark_tone,
        )
        profile = describe_profile(name)
    except (ValueError, FileExistsError, FileNotFoundError, OSError) as exc:
        _fail(exc)
    _emit({"ok": True, "profile": profile.to_dict()}, json_output)


@profile_app.command("configure")
def profile_configure(
    name: str = typer.Argument(..., help="Profile identifier."),
    display_name: str | None = typer.Option(None, "--display-name", help="Name shown in clients."),
    description: str | None = typer.Option(None, "--description", help="Short purpose shown in clients."),
    provider: str | None = typer.Option(None, "--provider", help="Profile-local active model provider."),
    model: str | None = typer.Option(None, "--model", help="Profile-local default model."),
    soul: str | None = typer.Option(None, "--soul", help="Replace profile-local SOUL.md contents."),
    soul_file: str | None = typer.Option(None, "--soul-file", hidden=True),
    mark_text: str | None = typer.Option(None, "--mark-text", help="One or two characters shown in Desktop."),
    mark_tone: str | None = typer.Option(None, "--mark-tone", help="Desktop signature color."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Update profile metadata and isolated runtime settings."""
    if all(
        value is None
        for value in (display_name, description, provider, model, soul, soul_file, mark_text, mark_tone)
    ):
        raise typer.BadParameter("at least one field is required")
    try:
        settings = None
        resolved_soul = _resolve_soul_option(soul, soul_file)
        if provider is not None or model is not None or resolved_soul is not None:
            settings = update_profile_settings(name, provider=provider, model=model, soul=resolved_soul)
        if display_name is not None or description is not None or mark_text is not None or mark_tone is not None:
            profile = update_profile_metadata(
                name,
                display_name=display_name,
                description=description,
                mark_text=mark_text,
                mark_tone=mark_tone,
            )
        else:
            profile = describe_profile(name)
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        _fail(exc)
    payload = {"ok": True, "profile": profile.to_dict()}
    if settings is not None:
        payload["settings"] = settings
    _emit(payload, json_output)


@profile_app.command("settings")
def profile_settings(
    name: str = typer.Argument(..., help="Profile identifier."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show non-secret model and persona settings for one profile."""
    try:
        settings = read_profile_settings(name)
    except (ValueError, FileNotFoundError, OSError) as exc:
        _fail(exc)
    _emit({"settings": settings}, json_output)


@profile_app.command("delete")
def profile_delete(
    name: str = typer.Argument(..., help="Named profile to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm permanent deletion."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Permanently delete a named profile and all of its local data."""
    if not yes:
        typer.confirm(
            f"Delete profile '{name}' and all of its local sessions, memory and files?",
            abort=True,
        )
    try:
        delete_profile(name)
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        _fail(exc)
    _emit({"ok": True, "deleted": name}, json_output)


@profile_app.command("export")
def profile_export(
    name: str = typer.Argument(..., help="Profile identifier."),
    output: str = typer.Option(..., "--output", help="Destination .tar.gz archive."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Export one complete isolated profile to a portable archive."""
    try:
        archive = export_profile(name, output)
    except (ValueError, FileNotFoundError, FileExistsError, RuntimeError, OSError) as exc:
        _fail(exc)
    _emit({"ok": True, "profile": name, "archive": str(archive)}, json_output)


@profile_app.command("import")
def profile_import(
    archive: str = typer.Argument(..., help="Source .tar.gz profile archive."),
    name: str | None = typer.Option(None, "--name", help="New profile identifier."),
    local_only: bool = typer.Option(
        False,
        "--local-only",
        help="Remove messaging transports and relay identity for a managed local bot.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Import a portable profile archive after validating every entry."""
    try:
        profile_dir = import_profile(archive, name=name, local_runtime=local_only)
        profile = describe_profile(profile_dir.name)
    except (ValueError, FileNotFoundError, FileExistsError, OSError) as exc:
        _fail(exc)
    _emit({"ok": True, "profile": profile.to_dict()}, json_output)
