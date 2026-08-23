"""Manage isolated local agent profiles."""

from __future__ import annotations

import json

import typer
from rich.console import Console

from flowly.profile import (
    create_profile,
    delete_profile,
    describe_profile,
    get_active_profile,
    list_profiles,
    read_profile_settings,
    update_profile_metadata,
    update_profile_settings,
)

profile_app = typer.Typer(help="Manage isolated local agent profiles")
console = Console()


def _emit(value: dict | list, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    else:
        console.print_json(data=value)


def _fail(exc: Exception) -> None:
    raise typer.BadParameter(str(exc)) from exc


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
            soul=soul,
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
    mark_text: str | None = typer.Option(None, "--mark-text", help="One or two characters shown in Desktop."),
    mark_tone: str | None = typer.Option(None, "--mark-tone", help="Desktop signature color."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Update profile metadata and isolated runtime settings."""
    if display_name is None and description is None and provider is None and model is None and soul is None and mark_text is None and mark_tone is None:
        raise typer.BadParameter("at least one field is required")
    try:
        settings = None
        if provider is not None or model is not None or soul is not None:
            settings = update_profile_settings(name, provider=provider, model=model, soul=soul)
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
