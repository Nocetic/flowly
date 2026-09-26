"""Explicit lifecycle commands for the selected profile's Linux browser."""

import json

import typer

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.profile import current_profile_name, get_flowly_home

screen_app = typer.Typer(help="Manage this profile's remote browser screen (Linux).", no_args_is_help=True)


def _runtime() -> ScreenRuntime:
    capabilities = ScreenRuntime.capabilities()
    if not capabilities["supported"]:
        raise ScreenError(capabilities["reason"])
    return ScreenRuntime(get_flowly_home() / "desktop")


def _status(runtime: ScreenRuntime) -> dict:
    state = runtime.status()
    return {"profileId": current_profile_name(), "phase": state["phase"] if state else "stopped",
            "generation": state["generation"] if state else None,
            "error": state.get("error") if state else None, "control": runtime.authority.snapshot()}


def _print(value: dict) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False))


def _error(exc: ScreenError) -> None:
    _print({"error": exc.code})
    raise typer.Exit(1)


@screen_app.command("capabilities")
def capabilities() -> None:
    """Check platform and OS dependencies without starting a screen."""
    _print(ScreenRuntime.capabilities())


@screen_app.command("status")
def status() -> None:
    """Show runtime and control status without exposing connection credentials."""
    try:
        _print(_status(_runtime()))
    except ScreenError as exc:
        _error(exc)


@screen_app.command("start")
def start() -> None:
    """Start or reconnect to this profile's persistent browser screen."""
    try:
        runtime = _runtime()
        runtime.start()
        _print(_status(runtime))
    except ScreenError as exc:
        _error(exc)


@screen_app.command("stop")
def stop() -> None:
    """Stop an idle screen; human control or an active run must be released first."""
    try:
        runtime = _runtime()
        state = runtime.status()
        if state is not None and state["phase"] != "stopped":
            runtime.stop(state["generation"])
        _print(_status(runtime))
    except ScreenError as exc:
        _error(exc)
