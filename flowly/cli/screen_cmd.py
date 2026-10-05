"""Explicit lifecycle commands for the selected profile's Linux browser."""

import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
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
    return ScreenRuntime(get_flowly_home() / "desktop", require_parent=True)


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


# What the person can do about each outcome of `flowly screen install`.
_ADVICE = {
    "INSTALL_UNSUPPORTED_HOST": "This server's Linux distribution or architecture is not supported for screens yet.",
    "INSTALL_TOOLS_MISSING": "This server lacks the package tools screens need (apt-get, dpkg, python3).",
    "INSTALL_MANAGED_BUILD_REQUIRED": "This Flowly is a packaged build. Install Flowly with install.sh to use screens.",
    "INSTALL_VIRTUALENV_REQUIRED": "Flowly is not in its own virtual environment. Reinstall it with install.sh.",
    "INSTALL_UV_REQUIRED": "uv was not found. Reinstall Flowly with install.sh.",
    "INSTALL_ADMISSION_UNAVAILABLE": "Run this as root, or as a user who can use sudo.",
    "INSTALL_PRIVILEGE_REQUIRED": "sudo did not accept the password.",
    "INSTALL_TERMINAL_REQUIRED": "Run this in a terminal (it may ask for the sudo password), or as root with --yes.",
    "INSTALL_INDEX_FAILED": "The package lists could not be refreshed. Check the server's internet connection, "
                            "then run `flowly screen install` again.",
    "INSTALL_HOST_PACKAGES_BROKEN": "The server's package manager has unfinished work that Flowly did not start. "
                                    "Run `sudo dpkg --configure -a`, then `flowly screen install` again.",
    "INSTALL_ACCOUNT_CONFLICT": "An account named flowly-screen exists on this server that Flowly did not create. "
                                "Remove or rename it, then run `flowly screen install` again.",
    "INSTALL_HISTORY_FULL": "This server has too many installation records. Contact Flowly support.",
}
_REPAIR = "The installation did not finish. Run `flowly screen install` again to repair it."
_WAIT_SECONDS = 30 * 60


def _screen_identity() -> dict:
    """The same identity the gateway gives this profile's screen."""
    from flowly.profile import active_profile_identity, get_or_create_profile_host_id

    _, _, bot_id = active_profile_identity()
    return {"hostId": get_or_create_profile_host_id(), "botId": bot_id, "profileId": current_profile_name()}


def _sudo_needs_password() -> bool:
    if os.geteuid() == 0:
        return False
    sudo = shutil.which("sudo", path="/usr/bin:/bin")
    return sudo is None or subprocess.run([sudo, "-n", "true"], stdin=subprocess.DEVNULL,
                                          capture_output=True, timeout=10).returncode != 0


def _until_idle(attempt, waiting: str):
    """Retry while agent work or another installation holds the environment."""
    deadline, told = time.monotonic() + _WAIT_SECONDS, False
    while True:
        try:
            return attempt()
        except ScreenError as exc:
            if exc.code != "INSTALL_BUSY" or time.monotonic() > deadline:
                raise
            if not told:
                typer.echo(waiting)
                told = True
            time.sleep(5)


@screen_app.command("install")
def install(yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before installing."),
            restart: bool = typer.Option(True, "--restart/--no-restart",
                                         help="Restart the agent's service when done (default).")) -> None:
    """Set up (or repair) screens on this server, then restart the agent once.

    The same reviewed installation Flowly Desktop runs. Running it again after
    an interrupted installation repairs it.
    """
    from flowly.desktop_runtime.install_jobs import ScreenInstallJobs
    from flowly.desktop_runtime.install_plan import installation_plan

    code = None
    try:
        identity = _screen_identity()
        plan = installation_plan(identity)
        if not plan["applySupported"]:
            for step in ("system", "python"):
                if not plan[step]["supported"]:
                    raise ScreenError(plan[step]["reason"])
            raise ScreenError("INSTALL_ADMISSION_UNAVAILABLE")
        system = plan["system"]
        typer.echo(f"Flowly will install screen components on this server ({system.get('distribution') or 'Linux'}):")
        typer.echo("  System packages: " + ", ".join(system["packages"]))
        typer.echo("  Python packages: " + ", ".join(f"{item['name']} {item['version']}" for item in plan["python"]["packages"]))
        typer.echo("The agent restarts once when this is done; its current work finishes first.")
        if not yes and not typer.confirm("Continue?", default=True):
            raise typer.Exit(1)
        secret = None
        if _sudo_needs_password():
            if not sys.stdin.isatty():
                raise ScreenError("INSTALL_TERMINAL_REQUIRED")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                try:
                    secret = getpass.getpass("Sudo password: ")
                except (getpass.GetPassWarning, EOFError):
                    raise ScreenError("INSTALL_TERMINAL_REQUIRED") from None
        jobs = ScreenInstallJobs(identity, lambda: None)
        operation = secrets.token_hex(16)
        try:
            _until_idle(lambda: jobs.submit(plan["planId"], operation, password=secret),
                        "Waiting for the agent to finish its current work…")
        finally:
            secret = None
        phase, steps = None, {"system": "Installing system packages…", "python": "Installing Python packages…"}
        while True:
            status = jobs.status(operation)
            if status["phase"] != phase and status["phase"] in steps:
                phase = status["phase"]
                typer.echo(steps[phase])
            if status["state"] not in {"pending", "running"}:
                break
            time.sleep(2)
        if status["state"] != "succeeded":
            raise ScreenError(status["error"] or "INSTALL_RECOVERY_REQUIRED")
        needed, reservation = _until_idle(lambda: jobs.restart_reservation(plan["planId"], operation),
                                          "Waiting for the agent to finish its current work before restarting…")
        if not needed:
            typer.echo("Screens are ready on this server.")
            return
        try:
            if not restart:
                typer.echo("Installed. Restart the agent to start using screens: flowly restart")
                return
            typer.echo("Restarting the agent to load the new components…")
            from flowly.cli.service_cmd import DEFAULT_SERVICE_LABEL, service_restart

            service_restart(label=DEFAULT_SERVICE_LABEL)
        finally:
            reservation.__exit__(None, None, None)
        typer.echo("Screens are ready on this server.")
    except ScreenError as exc:
        code = exc.code
    if code is not None:
        typer.echo(_ADVICE.get(code, _REPAIR), err=True)
        raise typer.Exit(1)


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
