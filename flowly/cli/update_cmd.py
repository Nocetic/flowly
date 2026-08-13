"""``flowly update`` — bring a CLI install up to the latest release.

Install-mode aware: Flowly ships in several shapes and each has its own native
upgrade path. The keystone is the **managed** mode — when Flowly runs as the
Nuitka-compiled binary embedded in Flowly Desktop, there is nothing for this
command to do: the desktop app's own auto-updater owns that binary, and the CLI
package on PATH (if any) is a physically separate install. So in managed mode we
no-op with guidance instead of attempting (and failing at) a package upgrade.

Modes and their upgrade command:
    managed   → no-op (Flowly Desktop manages the binary)
    source    → git pull --ff-only + reinstall (editable git checkout)
    uv-tool   → uv tool upgrade flowly-ai
    pipx      → pipx upgrade flowly-ai
    pip       → <python> -m pip install --upgrade flowly-ai
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from rich.console import Console

from flowly import __version__

console = Console()

PACKAGE = "flowly-ai"


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
# Source (git checkout) self-update — git pull --ff-only + reinstall
# ---------------------------------------------------------------------------
def _repo_root() -> Path | None:
    """The git checkout root when Flowly runs from a source/editable install."""
    try:
        import flowly

        root = Path(flowly.__file__).resolve().parent.parent  # parent of flowly/
        return root if (root / ".git").exists() else None
    except Exception:
        return None


def _git(repo: Path, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=capture, text=True, check=False,
    )


def _reinstall_editable(repo: Path) -> int:
    """Re-resolve dependencies for the editable checkout after a pull.

    Prefers uv (the installer uses it, and a uv-managed venv may not ship pip);
    falls back to this interpreter's pip.
    """
    import shutil

    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--python", sys.executable, "-e", str(repo)]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-e", str(repo)]
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    try:
        return subprocess.run(cmd, check=False).returncode
    except FileNotFoundError:
        console.print(f"[red]✗[/red] [bold]{cmd[0]}[/bold] not found for reinstall.")
        return 1


def _update_source(*, check_only: bool, force: bool, restart: bool) -> int:
    """Update a git-checkout install in place (git pull --ff-only + reinstall).

    Pulls the checkout's current branch from origin, autostashing local changes,
    then reinstalls deps and restarts the gateway. Mirrors the managed/PyPI
    paths' UX (``--check``, up-to-date short-circuit) for a source install.
    """
    repo = _repo_root()
    if repo is None:
        console.print("[yellow]Not a git checkout — nothing to git-update.[/yellow]")
        return 1

    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not branch or branch == "HEAD":
        console.print(
            "[yellow]Detached HEAD[/yellow] — check out a branch, then re-run "
            "[bold]flowly update[/bold]."
        )
        return 1

    console.print(f"[dim]Fetching origin/{branch}…[/dim]")
    if _git(repo, "fetch", "origin", branch, capture=False).returncode != 0:
        console.print("[red]✗[/red] git fetch failed — check your network / remote.")
        return 1

    behind_out = _git(repo, "rev-list", "--count", f"HEAD..origin/{branch}").stdout.strip()
    behind = int(behind_out) if behind_out.isdigit() else 0

    if behind == 0 and not force:
        console.print(
            f"[green]✓[/green] Flowly is up to date ([bold]{current_version()}[/bold], {branch})."
        )
        return 0
    if behind:
        plural = "s" if behind != 1 else ""
        console.print(
            f"Update available: [bold cyan]{behind}[/bold cyan] new commit{plural} on {branch}."
        )

    if check_only:
        return 0

    # Recover from a half-finished previous update so stash/pull don't abort.
    if _git(repo, "ls-files", "--unmerged").stdout.strip():
        _git(repo, "reset", "-q")

    stashed = False
    if _git(repo, "status", "--porcelain").stdout.strip():
        console.print("[dim]Stashing local changes…[/dim]")
        if _git(
            repo, "stash", "push", "--include-untracked", "-m", "flowly-update-autostash"
        ).returncode == 0:
            stashed = True

    console.print(f"[dim]$ git pull --ff-only origin {branch}[/dim]")
    if _git(repo, "pull", "--ff-only", "origin", branch, capture=False).returncode != 0:
        console.print(
            "[red]✗[/red] git pull failed (not a fast-forward?). Resolve it in:\n"
            f"  [bold]{repo}[/bold]"
        )
        if stashed:
            _git(repo, "stash", "pop")
        return 1

    if stashed:
        console.print("[dim]Restoring local changes…[/dim]")
        if _git(repo, "stash", "pop").returncode != 0:
            console.print("[yellow]⚠ Stash pop had conflicts — resolve them in the repo.[/yellow]")

    console.print("Reinstalling dependencies...")
    rc = _reinstall_editable(repo)
    if rc != 0:
        console.print(f"[red]✗[/red] Dependency reinstall failed (exit {rc}).")
        return rc

    clear_pycache()
    console.print("[green]✓[/green] Updated.")
    if restart:
        _restart_gateway()
    else:
        console.print("[dim]Skipped restart — run [bold]flowly restart[/bold] when ready.[/dim]")
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
        return _update_source(check_only=check_only, force=force, restart=restart)

    # Everything else is a legacy packaged install — the old PyPI ``flowly-ai``
    # package via uv tool / pipx / pip. PyPI no longer receives releases, so
    # "check PyPI, run the package manager's upgrade" would report "up to
    # date" forever: a silent lie. Say what's true and point at the migration,
    # which keeps ~/.flowly and the background service.
    console.print(
        f"[yellow]This is a legacy packaged install[/yellow] ({mode}, [bold]{cur}[/bold]) — "
        "Flowly no longer ships releases to\nPyPI, so this install cannot receive updates. "
        "Migrate in place (your ~/.flowly\ndata and background service are preserved):\n"
    )
    if platform.system() == "Windows":
        console.print("  [bold]irm https://useflowlyapp.com/install.ps1 | iex[/bold]")
    else:
        console.print("  [bold]curl -fsSL https://useflowlyapp.com/install.sh | bash[/bold]")
    return 1
