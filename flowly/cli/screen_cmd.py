"""Explicit lifecycle commands for the selected profile's Linux browser."""

import getpass
import json
import sys
import warnings

import typer
from filelock import Timeout

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.installer import install_plan, install_status, install_system
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.desktop_runtime.python_installer import install_python, python_plan, python_status
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


@screen_app.command("doctor")
def doctor() -> None:
    """Check screen dependencies and startup resources without creating a runtime."""
    report = ScreenRuntime.doctor(get_flowly_home() / "desktop")
    _print({"profileId": current_profile_name(), **report})
    if not report["ready"]:
        raise typer.Exit(1)


@screen_app.command("install-plan")
def installation_plan() -> None:
    """Preview the fixed host package plan; does not install or request sudo."""
    _print(install_plan())


@screen_app.command("install-python-plan")
def installation_python_plan() -> None:
    """Preview the locked screen packages for this agent's Python environment."""
    _print(python_plan())


@screen_app.command("install-python-status")
def installation_python_status(operation_id: str = typer.Option(...)) -> None:
    """Read a Python installation receipt without changing the environment."""
    try:
        _print(python_status(operation_id))
    except ScreenError as exc:
        _error(exc)


@screen_app.command("install-python")
def installation_python(plan_id: str = typer.Option(...), operation_id: str = typer.Option(...)) -> None:
    """Install reviewed wheel hashes into this virtualenv without sudo.

    A successful receipt reports restartRequired if existing agent processes
    need restarting to use changed packages. This command does not restart them.
    """
    try:
        receipt = install_python(plan_id, operation_id)
        _print(receipt)
        if receipt["state"] != "succeeded":
            raise typer.Exit(1)
    except ScreenError as exc:
        _error(exc)


@screen_app.command("install-status")
def installation_status(operation_id: str = typer.Option(...)) -> None:
    """Read the durable receipt for an explicit system-package installation."""
    try:
        _print(install_status(operation_id))
    except ScreenError as exc:
        _error(exc)


@screen_app.command("install-system")
def installation_system(plan_id: str = typer.Option(...), operation_id: str = typer.Option(...),
                        ask_sudo: bool = typer.Option(False)) -> None:
    """Apply a reviewed plan ID. Reuse the operation ID to inspect a lost reply.

    Installs OS packages only. Install the screen Python extra in the agent's
    virtualenv too; root agents still need unprivileged runtime provisioning.
    """
    try:
        # Validate a stale plan before asking for a secret. getpass reads the
        # controlling terminal; no password flag, environment variable or log.
        plan = install_plan()
        if not plan["supported"]:
            raise ScreenError(plan["reason"])
        if plan_id != plan["planId"]:
            raise ScreenError("INSTALL_PLAN_CHANGED")
        if ask_sudo and not sys.stdin.isatty():
            raise ScreenError("INSTALL_TERMINAL_REQUIRED")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                secret = getpass.getpass("Sudo password: ") if ask_sudo else None
            except (getpass.GetPassWarning, EOFError):
                raise ScreenError("INSTALL_TERMINAL_REQUIRED") from None
        try:
            receipt = install_system(plan_id, operation_id, password=secret)
        finally:
            secret = None
        _print(receipt)
        if receipt["state"] != "succeeded":
            raise typer.Exit(1)
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
    except Timeout:
        _error(ScreenError("LIFECYCLE_BUSY"))


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
    except Timeout:
        _error(ScreenError("LIFECYCLE_BUSY"))
