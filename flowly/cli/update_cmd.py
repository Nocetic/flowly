"""``flowly update`` — bring a CLI install up to the latest release.

Install-mode aware: Flowly ships in several shapes and each has its own native
upgrade path. The keystone is the **managed** mode — when Flowly runs as the
Nuitka-compiled binary embedded in Flowly Desktop, there is nothing for this
command to do: the desktop app's own auto-updater owns that binary, and the CLI
package on PATH (if any) is a physically separate install. So in managed mode we
no-op with guidance instead of attempting (and failing at) a package upgrade.

Modes and their upgrade command:
    managed   → no-op (Flowly Desktop manages the binary)
    source    → no-op with guidance (git checkout; `git pull` + reinstall)
    uv-tool   → uv tool upgrade flowly-ai
    pipx      → pipx upgrade flowly-ai
    pip       → <python> -m pip install --upgrade flowly-ai
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

from rich.console import Console

from flowly import __version__

console = Console()

PACKAGE = "flowly-ai"
_PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"


# ---------------------------------------------------------------------------
# Install-mode detection
# ---------------------------------------------------------------------------
def is_managed_binary() -> bool:
    """True when running as a frozen / Nuitka-compiled binary.

    Nuitka injects a module-level ``__compiled__`` global into ``__main__``;
    ``sys.frozen`` is the belt-and-braces fallback. This is the Flowly Desktop
    discriminator: the embedded binary is the only place compiled Flowly runs,
    and it must never try to upgrade itself."""
    return bool(getattr(sys.modules.get("__main__"), "__compiled__", None)) or bool(
        getattr(sys, "frozen", False)
    )


def _is_source_checkout() -> bool:
    """True when the running package lives inside a git checkout (dev install)."""
    try:
        import flowly

        pkg = Path(flowly.__file__).resolve().parent  # .../flowly
        return (pkg.parent / ".git").exists()
    except Exception:
        return False


def detect_install_mode() -> str:
    """Return one of: ``managed``, ``uv-tool``, ``pipx``, ``source``, ``pip``."""
    if is_managed_binary():
        return "managed"
    prefix = str(Path(sys.prefix)).replace("\\", "/")
    if "uv/tools/" in prefix:
        return "uv-tool"
    if "pipx/venvs/" in prefix:
        return "pipx"
    if _is_source_checkout():
        return "source"
    return "pip"


def upgrade_command(mode: str) -> list[str] | None:
    """The native upgrade command for *mode*, or None when there's nothing to run
    (managed binary, or a source checkout that needs git + reinstall by hand)."""
    if mode == "uv-tool":
        return ["uv", "tool", "upgrade", PACKAGE]
    if mode == "pipx":
        return ["pipx", "upgrade", PACKAGE]
    if mode == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]
    return None  # managed / source


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------
def current_version() -> str:
    return __version__


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse ``"1.2.0"`` → ``(1, 2, 0)``; non-numeric segments (``-dev``, ``rc1``)
    collapse to 0 so a comparison never raises."""
    core = v.split("+", 1)[0].split("-", 1)[0]
    out: list[int] = []
    for seg in core.split("."):
        digits = "".join(c for c in seg if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _is_newer(latest: str, current: str) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


def _pypi_latest(timeout: float = 5.0) -> str | None:
    """Latest released version from the PyPI JSON API, or None if unreachable."""
    try:
        with urllib.request.urlopen(_PYPI_URL, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data.get("info", {}).get("version") or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Maintenance helpers
# ---------------------------------------------------------------------------
def clear_pycache() -> None:
    """Best-effort removal of stale bytecode under the package, so a restart
    doesn't import a half-old/half-new mix."""
    try:
        import flowly

        pkg = Path(flowly.__file__).resolve().parent
        for cache in pkg.rglob("__pycache__"):
            for pyc in cache.glob("*.pyc"):
                try:
                    pyc.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def _restart_gateway() -> None:
    """Bounce the running gateway so it picks up the new code. Best-effort:
    reuses the smart service restart, which no-ops with a clear hint when the
    gateway runs in the foreground or isn't running at all."""
    try:
        from flowly.cli.service_cmd import DEFAULT_SERVICE_LABEL, service_restart

        service_restart(DEFAULT_SERVICE_LABEL)
    except Exception:
        console.print(
            "[dim]Could not auto-restart the gateway — run [bold]flowly restart[/bold] "
            "to load the new version.[/dim]"
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _windows_self_update(cmd: list[str]) -> int:
    """Upgrade Flowly on Windows without the locked-running-exe failure.

    pip must replace ``flowly.exe``, but Windows refuses while it's running
    (this process + the gateway). Spawn a detached batch file that waits a
    moment for Flowly to exit, force-closes any remaining ``flowly.exe`` to free
    the launcher, runs the upgrade, and reports the result in its own window.
    """
    import tempfile

    pip_line = " ".join(f'"{c}"' if (" " in c or "\\" in c) else c for c in cmd)
    lines = [
        "@echo off",
        "echo Finishing the Flowly update (Flowly closes so it can replace its files)...",
        "ping -n 3 127.0.0.1 >nul",                 # ~2s grace for this process to exit
        "taskkill /im flowly.exe /f >nul 2>&1",     # free the locked launcher (stops the gateway too)
        "ping -n 2 127.0.0.1 >nul",
        pip_line,
        "if errorlevel 1 ( echo. & echo Update FAILED. & echo. & pause & exit /b 1 )",
        "echo.",
        "echo Flowly updated. To continue:  flowly service install --start  then  flowly",
        "echo You can close this window.",
        "pause >nul",
    ]
    bat = tempfile.NamedTemporaryFile(
        "w", suffix="_flowly_update.bat", delete=False, encoding="utf-8", newline="\r\n"
    )
    bat.write("\n".join(lines))
    bat.close()
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "Flowly Update", "cmd", "/c", bat.name],
            creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        )
    except Exception as e:  # noqa: BLE001
        console.print(
            f"[red]✗[/red] Couldn't launch the updater ({e}). Update manually in a new "
            f"terminal:\n  [bold]python -m pip install --user --upgrade {PACKAGE}[/bold]"
        )
        return 1
    console.print(
        "[green]Update starting in a separate window[/green] — Flowly will close so it can "
        "replace its files, then that window finishes the upgrade."
    )
    return 0


def run_update(
    *,
    check_only: bool = False,
    assume_yes: bool = False,
    force: bool = False,
    restart: bool = True,
) -> int:
    """Drive the update. Returns a process-style exit code (0 = ok)."""
    mode = detect_install_mode()
    cur = current_version()

    if mode == "managed":
        console.print(
            "[cyan]Flowly is running inside Flowly Desktop[/cyan] — the app manages "
            "its own updates.\nUpdate from the app (or download the latest from "
            "https://useflowlyapp.com). Nothing to do here."
        )
        return 0

    if mode == "source":
        console.print(
            "[yellow]Source checkout[/yellow] — update with git, then reinstall:\n"
            "  [bold]git pull[/bold]\n"
            "  [bold]uv pip install -e \".[dev]\"[/bold]  (or your usual editable install)"
        )
        return 0

    latest = _pypi_latest()
    if latest is None:
        console.print("[yellow]Couldn't reach PyPI to check the latest version.[/yellow]")
        if not force:
            console.print("Re-run with [bold]--force[/bold] to reinstall anyway.")
            return 1
    elif not force and not _is_newer(latest, cur):
        console.print(f"[green]✓[/green] Flowly is up to date ([bold]{cur}[/bold]).")
        return 0
    elif latest is not None:
        console.print(
            f"Update available: [bold]{cur}[/bold] → [bold cyan]{latest}[/bold cyan]"
        )

    if check_only:
        return 0

    # `flowly update` installs directly — no confirmation prompt. (Running the
    # command is the confirmation; `--check` is there for a dry look.)
    console.print("Installing the update...")

    cmd = upgrade_command(mode)
    if cmd is None:  # defensive — managed/source handled above
        return 0

    if platform.system() == "Windows":
        # Windows can't overwrite flowly.exe while it (this process) and the
        # gateway are running, so pip's in-process upgrade dies with
        # "WinError 32: file in use". Hand off to a detached updater that closes
        # Flowly first, then upgrades.
        return _windows_self_update(cmd)

    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    try:
        proc = subprocess.run(cmd, check=False)
    except FileNotFoundError:
        console.print(
            f"[red]✗[/red] [bold]{cmd[0]}[/bold] not found. Install it, or update "
            f"manually: [bold]pip install --upgrade {PACKAGE}[/bold]."
        )
        return 1
    if proc.returncode != 0:
        console.print(f"[red]✗[/red] Update failed (exit {proc.returncode}).")
        return proc.returncode

    clear_pycache()
    console.print("[green]✓[/green] Updated.")

    if restart:
        _restart_gateway()
    else:
        console.print("[dim]Skipped restart — run [bold]flowly restart[/bold] when ready.[/dim]")
    return 0
