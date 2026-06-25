"""CLI commands — service_cmd."""

import asyncio
import os
import platform
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from flowly import __version__, __logo__

console = Console()

# ============================================================================
# Service Commands
# ============================================================================

service_app = typer.Typer(help="Manage background gateway service")


def _is_windows_admin() -> bool:
    """Return True if the current process has Administrator rights on Windows.

    Returns False on any non-Windows platform or if the probe itself fails
    (e.g. Wine, stripped-down Windows SKU, ctypes missing). A False return
    means "do not assume admin", never "definitely not admin" — callers
    should fail closed.
    """
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

DEFAULT_SERVICE_LABEL = "ai.flowly.gateway"


def _strip_control_chars(s: str) -> str:
    """Remove control characters that plistlib cannot serialize."""
    import re
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)


def _resolve_flowly_exec_argv() -> list[str]:
    """Resolve the executable argv prefix used for service definitions.

    Resolution order (highest priority first):
      1. Running as a Nuitka-compiled standalone binary → use sys.executable,
         which points directly at the compiled binary (e.g. flowly-bin bundled
         inside an Electron app). This must win over PATH lookups so the
         launchd/systemd service always targets the bundled runtime.
      2. argv[0] starts with "flowly" and points at an existing file → trust it
         (covers direct invocation of either `flowly` or `flowly-bin`).
      3. `flowly` on PATH (legacy pip/uv installs).
      4. `~/.local/bin/flowly` fallback.
      5. `uv run flowly` as a last resort.
    """
    # (1) Nuitka-compiled standalone or PyInstaller frozen binary.
    # For Nuitka, sys.executable points at the compiled binary (e.g. flowly-bin).
    # For regular Python, sys.executable points at the python interpreter.
    # We treat anything whose basename does not start with "python" as a
    # compiled runtime and trust sys.executable directly.
    exe = Path(sys.executable).expanduser().resolve()
    exe_name = exe.name.lower()
    looks_like_python = exe_name.startswith("python")
    is_frozen = (
        getattr(sys, "frozen", False)
        or bool(getattr(sys.modules.get("__main__"), "__compiled__", None))
        or (not looks_like_python and exe.exists())
    )
    if is_frozen and exe.exists():
        return [str(exe)]

    # (2) argv[0] points at a flowly* binary
    argv0 = Path(sys.argv[0]).expanduser()
    if argv0.exists() and argv0.name.startswith("flowly"):
        return [str(argv0.resolve())]

    # (3) PATH lookup
    flowly_bin = shutil.which("flowly")
    if flowly_bin:
        return [str(Path(flowly_bin).expanduser())]

    # (4) Standard user-local path
    local_bin = (Path.home() / ".local" / "bin" / "flowly").expanduser()
    if local_bin.exists():
        return [str(local_bin)]

    # (5) uv fallback
    uv_bin = shutil.which("uv")
    if uv_bin:
        return [str(Path(uv_bin).expanduser()), "run", "flowly"]

    return ["flowly"]


def _service_paths(label: str) -> tuple[Path | None, Path | None, Path | None]:
    """Return service file paths for macOS/Linux/Windows."""
    system = platform.system().lower()
    if system == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist", None, None
    if system == "linux":
        return None, Path.home() / ".config" / "systemd" / "user" / f"{label}.service", None
    if system == "windows":
        return None, None, Path.home() / "AppData" / "Local" / "flowly" / f"{label}.xml"
    return None, None, None


def _get_log_dir() -> Path:
    """Return platform-appropriate log directory for gateway."""
    system = platform.system().lower()
    if system == "windows":
        log_dir = Path.home() / "AppData" / "Local" / "flowly" / "logs"
    else:
        from flowly.profile import get_flowly_home
        log_dir = get_flowly_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _run_cmd(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run command and return completed process with text output."""
    proc = subprocess.run(args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"{' '.join(args)} failed: {stderr}")
    return proc


def _service_health(port: int) -> tuple[bool, str]:
    """Check local gateway health endpoint."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            if 200 <= int(resp.status) < 300:
                return True, f"{url} OK"
            return False, f"{url} HTTP {resp.status}"
    except urllib.error.URLError as e:
        return False, f"{url} unavailable ({e.reason})"
    except Exception as e:
        return False, f"{url} unavailable ({e})"


def _port_listener_pids(port: int) -> list[int]:
    """PIDs listening on ``port`` — READ-ONLY, never kills. Cross-platform.

    Used for status/diagnostics. Empty list on any failure (best-effort).
    """
    system = platform.system().lower()
    pids: list[int] = []
    try:
        if system in ("darwin", "linux"):
            r = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        elif system == "windows":
            r = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line.upper():
                    parts = line.split()
                    if parts and parts[-1].isdigit():
                        pids.append(int(parts[-1]))
    except Exception:
        pass
    return sorted(set(pids))


def _print_port_diagnostics(port: int, *, installed: bool, service_running: bool) -> None:
    """Show what holds the gateway port + warn on a process/service mismatch.

    READ-ONLY: never kills anything. Flowly Desktop and a manual ``flowly
    gateway`` legitimately hold the port WITHOUT the service, so we only warn
    and tell the user how to resolve it — we never act on their behalf.
    """
    pids = _port_listener_pids(port)
    if not pids:
        console.print(f"Port {port}: [dim]free — nothing listening[/dim]")
        return
    console.print(f"Port {port}: [green]in use[/green] by PID {', '.join(map(str, pids))}")
    if installed and not service_running:
        console.print(
            f"  [yellow]⚠ A gateway is running, but NOT via the installed service[/yellow] "
            f"[dim](a manual `flowly gateway`, or Flowly Desktop).[/dim]"
        )
        console.print(
            f"  [dim]To let the service manage it:[/dim] "
            f"[cyan]flowly service stop[/cyan] [dim]then[/dim] [cyan]flowly service start[/cyan]"
        )


def _kill_gateway_on_port(port: int, wait: float = 2.0) -> bool:
    """Kill any process listening on the gateway port. Returns True if killed."""
    system = platform.system().lower()
    try:
        if system == "darwin" or system == "linux":
            result = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if pids:
                import time
                time.sleep(wait)
                # SIGKILL any survivors
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                return True
        elif system == "windows":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = int(line.strip().split()[-1])
                    subprocess.run(
                        ["taskkill", "/pid", str(pid), "/T", "/F"],
                        capture_output=True, timeout=5,
                    )
                    return True
    except Exception:
        pass
    return False


def _extract_port_from_plist(plist_path: Path) -> int:
    if not plist_path.exists():
        return 18790
    try:
        raw = plist_path.read_bytes()
        data = plistlib.loads(raw)
        args = data.get("ProgramArguments", [])
        if "--port" in args:
            idx = args.index("--port")
            if idx + 1 < len(args):
                return int(args[idx + 1])
    except Exception:
        pass
    return 18790


def _extract_port_from_unit(unit_path: Path) -> int:
    if not unit_path.exists():
        return 18790
    try:
        content = unit_path.read_text(encoding="utf-8")
    except Exception:
        return 18790
    marker = "--port"
    if marker not in content:
        return 18790
    try:
        after = content.split(marker, 1)[1].strip()
        return int(after.split()[0])
    except Exception:
        return 18790


def _extract_port_from_win_xml(xml_path: Path) -> int:
    """Extract --port value from Windows Task Scheduler XML."""
    if not xml_path.exists():
        return 18790
    try:
        content = xml_path.read_text(encoding="utf-16")
    except Exception:
        return 18790
    marker = "--port"
    if marker not in content:
        return 18790
    try:
        after = content.split(marker, 1)[1].strip()
        return int(after.split()[0].strip('"').strip("'"))
    except Exception:
        return 18790


def _service_env_base(flowly_home: str, runtime_cwd: str) -> dict[str, str]:
    """Env vars common to every platform's service definition.

    ``FLOWLY_HOME`` is written explicitly so the service resolves the
    right profile regardless of its WorkingDirectory (which is now a
    stable home, not the install-time cwd). ``FLOWLY_CWD`` is added only
    when the operator passed ``--cwd``.
    """
    env = {"FLOWLY_HOME": flowly_home}
    if runtime_cwd:
        env["FLOWLY_CWD"] = runtime_cwd
    return env


def _build_mac_plist_obj(
    *, label: str, argv: list[str], flowly_home: str, runtime_cwd: str,
) -> dict:
    """Build the launchd plist dict. Pure — no filesystem/launchctl."""
    env = {
        "PATH": _strip_control_chars(os.environ.get("PATH", "")),
        "PYTHONUNBUFFERED": "1",
        # Force UTF-8 stdio so Rich/Typer can write unicode (e.g. the
        # checkmark character) to the launchd-owned log files, which
        # are non-TTY and default to ASCII on macOS.
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "LC_ALL": "en_US.UTF-8",
        "LANG": "en_US.UTF-8",
    }
    env.update(_service_env_base(flowly_home, runtime_cwd))
    return {
        "Label": label,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": True,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
        # Stable home, NOT Path.cwd(): never capture the install-time
        # directory — subprocess cwd is owned by runtime_cwd resolution.
        "WorkingDirectory": str(Path.home()),
        "StandardOutPath": str(_get_log_dir() / "flowly-gateway.out.log"),
        "StandardErrorPath": str(_get_log_dir() / "flowly-gateway.err.log"),
        "EnvironmentVariables": env,
    }


def _build_linux_unit(
    *, exec_line: str, flowly_home: str, runtime_cwd: str,
) -> str:
    """Build the systemd user unit text. Pure."""
    env_lines = ["Environment=PYTHONUNBUFFERED=1"]
    for key, val in _service_env_base(flowly_home, runtime_cwd).items():
        env_lines.append(f"Environment={key}={val}")
    env_block = "\n".join(env_lines)
    return textwrap.dedent(
        f"""\
        [Unit]
        Description=Flowly Gateway Service
        After=network.target

        [Service]
        Type=simple
        ExecStart={exec_line}
        Restart=always
        RestartSec=3
        WorkingDirectory={Path.home()}
        {env_block}
        StandardOutput=append:{_get_log_dir() / "flowly-gateway.out.log"}
        StandardError=append:{_get_log_dir() / "flowly-gateway.err.log"}

        [Install]
        WantedBy=default.target
        """
    )


def _provider_configured() -> bool:
    """True if an LLM provider is usable right now.

    The gateway exits non-zero at boot when no provider is configured
    (see ``flowly gateway``). Under launchd/systemd that turns into a
    crash-restart loop. We preflight the same check so service commands
    can refuse to *start* an unconfigured gateway — the unit still gets
    installed so it's ready the moment ``flowly setup`` is done.
    """
    try:
        from flowly.config.loader import load_config
        from flowly.integrations.active_provider import resolve_active_provider
        return resolve_active_provider(load_config()) is not None
    except Exception:
        return False


def _warn_no_provider(action: str) -> None:
    console.print(
        f"[yellow]No LLM provider configured — not {action}.[/yellow]\n"
        "The gateway would crash-loop without one. Configure a provider, "
        "then start the service:"
    )
    console.print(
        "  [cyan]flowly setup[/]            [dim]— pick a provider[/]"
    )
    console.print(
        "  [cyan]flowly service start[/]    [dim]— start once configured[/]"
    )


def _detect_public_ip() -> str:
    """Best-effort public IP of this host, for the post-install hint.

    When the gateway binds 0.0.0.0 we can't know the reachable address from
    the socket alone (a VPS sits behind cloud NAT), so we ask a couple of
    well-known echo endpoints. Short timeout; returns "" on any failure so the
    install never blocks or errors on a missing/filtered network.
    """
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 — fixed https hosts
                ip = (resp.read().decode("utf-8") or "").strip()
            # Reject anything that isn't a plausible bare IPv4/IPv6 literal.
            if ip and len(ip) <= 45 and all(c in "0123456789abcdefABCDEF.:" for c in ip):
                return ip
        except Exception:
            continue
    return ""


def _ensure_linger_linux() -> None:
    """Enable systemd-user *linger* so the gateway keeps running after you log
    out — and starts on boot.

    A ``systemctl --user`` service lives under the user's systemd instance
    (``user@<uid>.service``). With linger OFF, that instance is torn down when
    the user's last login session ends, taking every --user service (the
    gateway) with it — so the bot dies a while after you close SSH, and
    ``Restart=always`` can't help because it's the whole manager going away, not
    the service crashing. ``loginctl enable-linger`` keeps the instance alive
    independently of logins. Best-effort: works without sudo when polkit
    permits it; otherwise we print the one-line manual fix and continue.
    """
    if platform.system().lower() != "linux" or not shutil.which("loginctl"):
        return
    import getpass

    username = getpass.getuser()
    try:
        probe = subprocess.run(
            ["loginctl", "show-user", username, "--property=Linger"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if "Linger=yes" in (probe.stdout or ""):
            return  # already persistent — nothing to do
    except Exception:
        pass  # fall through and try to enable

    try:
        r = subprocess.run(
            ["loginctl", "enable-linger", username],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception as exc:
        console.print(f"[yellow]Could not enable linger ({exc}).[/yellow]")
        console.print(f"[dim]Without it the service stops when you log out. Fix: sudo loginctl enable-linger {username}[/dim]")
        return

    if r.returncode == 0:
        console.print(f"[green]✓[/green] Enabled linger for [bold]{username}[/bold] — service survives logout and reboots")
    else:
        detail = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
        console.print(f"[yellow]Could not enable linger: {detail}[/yellow]")
        console.print(f"[dim]Without it the service stops when you log out. Fix: sudo loginctl enable-linger {username}[/dim]")


@service_app.command("install")
def service_install(
    label: str = typer.Option(DEFAULT_SERVICE_LABEL, "--label", help="Service label"),
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable gateway verbose mode"),
    start: bool = typer.Option(True, "--start/--no-start", help="Start service after install"),
    force: bool = typer.Option(False, "--force", "-f", help="(No longer required — install is idempotent and reinstalls cleanly. Kept for back-compat.)"),
    persona: str = typer.Option("", "--persona", help="Bot persona (default, jarvis, pirate, samurai, casual, professor, butler, friday)"),
    cwd: str = typer.Option("", "--cwd", help="Pin the gateway's runtime working directory (absolute, existing). Writes FLOWLY_CWD; omit to use config/workspace."),
    host: str = typer.Option("", "--host", help="Bind address baked into the service. Use 0.0.0.0 (or the VPS IP) to accept remote desktop clients; omit for local-only (127.0.0.1)."),
    remote: bool = typer.Option(False, "--remote", help="Accept connections from your phone / other devices — plain-language alias for --host 0.0.0.0 (a token is ensured automatically)."),
    token: str = typer.Option("", "--token", help="Remote-access token baked into the service (persisted). Omit to let the gateway auto-generate + print one on first remote bind."),
):
    """Install background service for flowly gateway."""
    from flowly.profile import get_flowly_home
    from flowly.runtime_cwd import validate_cwd

    flowly_home = str(get_flowly_home())
    runtime_cwd = ""
    if cwd:
        validated = validate_cwd(cwd, require_absolute=True)
        if validated is None:
            console.print(f"[red]--cwd is not an existing absolute directory: {cwd}[/red]")
            raise typer.Exit(1)
        runtime_cwd = str(validated)

    # Preflight: don't hand launchd/systemd a gateway that will crash-loop
    # on a missing provider. Install the unit either way (so it's ready),
    # but skip the auto-start when there's nothing for it to run.
    if start and not _provider_configured():
        _warn_no_provider("starting the service")
        start = False

    mac_plist, linux_unit, win_xml = _service_paths(label)
    exec_argv = _resolve_flowly_exec_argv()
    system = platform.system().lower()

    # Build the `flowly gateway …` argv suffix ONCE so every platform's service
    # definition is identical. --host/--token are baked into the unit's
    # ExecStart so the *running service* binds the right address + token — the
    # `flowly gateway` command is separate from `service install`, so the flags
    # must live on the unit, not just an interactive invocation. Persist them
    # too, so a manual restart matches.
    gateway_args = ["gateway", "--port", str(port)]

    # Resolve host + token the SAME way `flowly gateway` does. A background
    # service can't print an auto-generated token on first bind where you'd
    # see it (its stdout goes to the service log), so when you expose the
    # gateway remotely (--host 0.0.0.0 / a public IP) WITHOUT a token, we
    # generate one HERE, bake it into the unit, persist it, and print it below
    # — that's the credential you type into the desktop.
    from flowly.gateway.auth import generate_gateway_token, is_loopback_host
    from flowly.config.loader import load_config, save_config

    try:
        _cfg = load_config()
    except Exception:
        _cfg = None

    # --remote is the friendly alias for --host 0.0.0.0; explicit --host wins.
    if host.strip():
        effective_host = host.strip()
    elif remote:
        effective_host = "0.0.0.0"
    else:
        effective_host = _cfg.gateway.host if _cfg else "127.0.0.1"
    auth_token = token.strip() if token.strip() else ((_cfg.gateway.token or "").strip() if _cfg else "")
    remote_exposed = not is_loopback_host(effective_host)
    if remote_exposed and not auth_token:
        auth_token = generate_gateway_token()

    if host.strip():
        gateway_args += ["--host", effective_host]
    if auth_token:
        gateway_args += ["--token", auth_token]
    if verbose:
        gateway_args.append("--verbose")
    if persona:
        gateway_args += ["--persona", persona]

    if _cfg is not None and (host.strip() or auth_token):
        try:
            if host.strip():
                _cfg.gateway.host = effective_host
            if auth_token:
                _cfg.gateway.token = auth_token
            save_config(_cfg)
        except Exception as e:
            console.print(f"[yellow]Could not persist gateway host/token: {e}[/yellow]")

    # Hand the user the exact values to enter in the desktop. Printed BEFORE the
    # platform install so it's never lost behind a long install trace.
    if remote_exposed and auth_token:
        _bind_all = effective_host in ("0.0.0.0", "::")
        console.print("\n[bold yellow]Remote access[/bold yellow] — enter in the app (Settings → Connections):")
        if _bind_all:
            # LAN IP first: same-Wi-Fi is the common case, and the public IP only
            # works from outside the network with a router port-forward.
            from flowly.gateway.remote_info import detect_lan_ip
            lan = detect_lan_ip()
            pub = _detect_public_ip()
            if lan:
                console.print(f"  Same Wi-Fi (most common) → Host : [bold]{lan}[/bold]")
            if pub:
                console.print(f"  Over the internet (needs port-forward) → Host : [bold]{pub}[/bold]")
            if not lan and not pub:
                console.print("  Host : [bold]<this machine's IP>[/bold]")
        else:
            console.print(f"  Host : [bold]{effective_host}[/bold]")
        console.print(f"  Port  : [bold]{port}[/bold]")
        console.print(f"  Token : [bold]{auth_token}[/bold]")
        console.print("  TLS   : [bold]off[/bold]  [dim](plain ws:// — leave 'Use TLS' off in the app)[/dim]")
        console.print(
            f"[dim]Keep the token secret. Phone on the same Wi-Fi → use the first IP. Allow "
            f"inbound TCP {port} in the firewall (tip: `flowly enroll` sets this up for you).[/dim]\n"
        )

    if system == "darwin" and mac_plist:
        mac_plist.parent.mkdir(parents=True, exist_ok=True)
        if mac_plist.exists():
            console.print(f"[dim]Reinstalling — service already present ({mac_plist}).[/dim]")

        argv = exec_argv + list(gateway_args)
        plist_obj = _build_mac_plist_obj(
            label=label, argv=argv, flowly_home=flowly_home, runtime_cwd=runtime_cwd,
        )
        mac_plist.write_bytes(plistlib.dumps(plist_obj, fmt=plistlib.FMT_XML, sort_keys=False))

        try:
            _run_cmd(["launchctl", "unload", str(mac_plist)], check=False)
            _run_cmd(["launchctl", "load", str(mac_plist)])
            if start:
                _run_cmd(["launchctl", "start", label], check=False)
        except Exception as e:
            console.print(f"[red]Service install failed: {e}[/red]")
            raise typer.Exit(1)

        console.print(f"[green]✓[/green] Installed launchd service: {label}")
        console.print(f"[dim]File: {mac_plist}[/dim]")
        return

    if system == "linux" and linux_unit:
        linux_unit.parent.mkdir(parents=True, exist_ok=True)
        if linux_unit.exists():
            console.print(f"[dim]Reinstalling — service already present ({linux_unit}).[/dim]")

        argv = exec_argv + list(gateway_args)
        exec_line = shlex.join(argv)
        unit_content = _build_linux_unit(
            exec_line=exec_line, flowly_home=flowly_home, runtime_cwd=runtime_cwd,
        )
        linux_unit.write_text(unit_content, encoding="utf-8")

        try:
            # Persist the user systemd instance FIRST, so the service we enable
            # below isn't torn down when this login session ends.
            _ensure_linger_linux()
            _run_cmd(["systemctl", "--user", "daemon-reload"])
            _run_cmd(["systemctl", "--user", "enable", label])
            if start:
                _run_cmd(["systemctl", "--user", "restart", label])
        except Exception as e:
            console.print(f"[red]Service install failed: {e}[/red]")
            console.print("[dim]Tip: Ensure user systemd is available (login session).[/dim]")
            raise typer.Exit(1)

        console.print(f"[green]✓[/green] Installed systemd user service: {label}")
        console.print(f"[dim]File: {linux_unit}[/dim]")
        return

    if system == "windows" and win_xml:
        # Task Scheduler creation may need elevation, but we no longer refuse up
        # front: we try schtasks and, if it's denied / locked down / wedged,
        # fall back to a Startup-folder launcher (below) that needs no admin.
        if not _is_windows_admin():
            console.print(
                "[dim]Not elevated — will fall back to the Startup folder if "
                "Task Scheduler denies the task (no admin needed).[/dim]"
            )

        win_xml.parent.mkdir(parents=True, exist_ok=True)
        if win_xml.exists():
            console.print(f"[dim]Reinstalling — service already present ({win_xml}).[/dim]")

        log_dir = _get_log_dir()
        argv = exec_argv + list(gateway_args)

        command = argv[0]
        arguments = " ".join(argv[1:]) if len(argv) > 1 else ""
        out_log = str(log_dir / "flowly-gateway.out.log")
        err_log = str(log_dir / "flowly-gateway.err.log")

        # Escape XML special characters in dynamic values
        def _xml_escape(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

        # Use cmd /c wrapper to redirect stdout/stderr to log files.
        # Inject FLOWLY_HOME (and FLOWLY_CWD when --cwd given) via `set`
        # so the service resolves the right profile / runtime cwd without
        # relying on the install-time directory.
        #
        # PYTHONUTF8=1 first: the gateway prints Unicode glyphs (✓, →, the
        # banner) and, when stdout is redirected to the log file under Task
        # Scheduler, Python otherwise defaults to the legacy code page (cp1252)
        # and rich's console.print raises UnicodeEncodeError — crashing the
        # service on startup while a foreground run (UTF-8 console) is fine.
        env_sets = ['set "PYTHONUTF8=1"', f'set "FLOWLY_HOME={flowly_home}"']
        if runtime_cwd:
            env_sets.append(f'set "FLOWLY_CWD={runtime_cwd}"')
        env_prefix = " && ".join(env_sets)
        # Append (>>) not overwrite (>): a bare > truncates the log on every
        # service start, losing the prior run's diagnostics. >> preserves
        # history across restarts (matching launchd/systemd append behaviour).
        # The cmd line that launches the gateway (env + redirect to logs).
        cmd_line = f'cmd /c {env_prefix} && "{command}" {arguments} >> "{out_log}" 2>> "{err_log}"'
        # Run it HIDDEN via a tiny VBScript. Task Scheduler + cmd.exe pops a
        # visible console window for InteractiveToken tasks, and CLOSING that
        # window kills the gateway. wscript's Run(cmd, 0, False) launches with no
        # window (0) and doesn't wait — so there's nothing to accidentally close.
        vbs_cmd = cmd_line.replace('"', '""')  # VBS string escaping
        vbs_body = (
            'Set sh = CreateObject("WScript.Shell")\r\n'
            'sh.Run "' + vbs_cmd + '", 0, False\r\n'
        )
        vbs_path = win_xml.parent / f"{label}.vbs"
        vbs_path.write_text(vbs_body, encoding="utf-8")
        task_command = "wscript.exe"
        task_arguments = f'"{vbs_path}"'
        # Stable home, NOT Path.cwd(): never capture the install-time dir.
        working_dir = str(Path.home())

        task_xml = textwrap.dedent(
            f"""\
            <?xml version="1.0" encoding="UTF-16"?>
            <Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
              <RegistrationInfo>
                <Description>Flowly Gateway Service</Description>
              </RegistrationInfo>
              <Triggers>
                <LogonTrigger>
                  <Enabled>true</Enabled>
                </LogonTrigger>
              </Triggers>
              <Principals>
                <Principal id="Author">
                  <LogonType>InteractiveToken</LogonType>
                  <RunLevel>LeastPrivilege</RunLevel>
                </Principal>
              </Principals>
              <Settings>
                <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
                <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
                <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
                <AllowHardTerminate>true</AllowHardTerminate>
                <StartWhenAvailable>true</StartWhenAvailable>
                <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
                <AllowStartOnDemand>true</AllowStartOnDemand>
                <Enabled>true</Enabled>
                <Hidden>true</Hidden>
                <RestartOnFailure>
                  <Interval>PT1M</Interval>
                  <Count>10</Count>
                </RestartOnFailure>
                <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
              </Settings>
              <Actions Context="Author">
                <Exec>
                  <Command>{_xml_escape(task_command)}</Command>
                  <Arguments>{_xml_escape(task_arguments)}</Arguments>
                  <WorkingDirectory>{_xml_escape(working_dir)}</WorkingDirectory>
                </Exec>
              </Actions>
            </Task>
            """
        )
        win_xml.write_text(task_xml, encoding="utf-16")

        # Try Task Scheduler with a hard timeout (schtasks can wedge on locked-
        # down machines). On any failure, fall back to a Startup-folder .cmd.
        schtasks_ok = False
        try:
            r = subprocess.run(
                ["schtasks", "/create", "/tn", label, "/xml", str(win_xml), "/f"],
                capture_output=True, text=True, timeout=20,
            )
            schtasks_ok = r.returncode == 0
            if not schtasks_ok:
                detail = (r.stderr or r.stdout or "").strip().splitlines()
                console.print(
                    f"[yellow]Task Scheduler refused ({detail[0][:120] if detail else 'unknown'}) — "
                    f"using the Startup folder instead.[/yellow]"
                )
        except Exception as e:
            console.print(
                f"[yellow]Task Scheduler unavailable ({e}) — using the Startup folder instead.[/yellow]"
            )

        if schtasks_ok:
            if start:
                _run_cmd(["schtasks", "/run", "/tn", label], check=False)
            console.print(f"[green]✓[/green] Installed Windows Task Scheduler service: {label}")
            console.print(f"[dim]File: {win_xml}[/dim]")
            return

        # Fallback: a Startup-folder launcher. Runs the gateway at logon in the
        # user session — no admin, no Task Scheduler. (Not managed by
        # `flowly service stop/restart`, which target the scheduled task; stop a
        # Startup-folder gateway by quitting the process / `flowly service stop`
        # won't reach it — acceptable for this rarely-hit fallback path.)
        startup_dir = (
            Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        )
        startup_dir.mkdir(parents=True, exist_ok=True)
        startup_cmd = startup_dir / f"{label}.cmd"
        cmd_lines = ["@echo off", 'set "PYTHONUTF8=1"', f'set "FLOWLY_HOME={flowly_home}"']
        if runtime_cwd:
            cmd_lines.append(f'set "FLOWLY_CWD={runtime_cwd}"')
        cmd_lines.append(f'start "Flowly Gateway" /min "{command}" {arguments}')
        startup_cmd.write_text("\r\n".join(cmd_lines) + "\r\n", encoding="utf-8")
        console.print(f"[green]✓[/green] Installed Startup-folder launcher (runs at logon): {startup_cmd}")

        if start:
            try:
                _env = {**os.environ, "PYTHONUTF8": "1", "FLOWLY_HOME": str(flowly_home)}
                if runtime_cwd:
                    _env["FLOWLY_CWD"] = str(runtime_cwd)
                subprocess.Popen(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | NEW_PROCESS_GROUP
                    env=_env,
                )
                console.print("[green]✓[/green] Gateway started.")
            except Exception as ee:
                console.print(f"[yellow]Couldn't auto-start ({ee}) — run 'flowly gateway'.[/yellow]")
        console.print(f"[dim]File: {startup_cmd}[/dim]")
        return

    console.print(f"[red]Unsupported platform for service install: {platform.system()}[/red]")
    raise typer.Exit(1)


@service_app.command("start")
def service_start(
    label: str = typer.Option(DEFAULT_SERVICE_LABEL, "--label", help="Service label"),
):
    """Start installed background service."""
    if not _provider_configured():
        _warn_no_provider("starting the service")
        raise typer.Exit(1)
    mac_plist, linux_unit, win_xml = _service_paths(label)
    system = platform.system().lower()

    # Don't launch a second gateway: if the port is already held, say so and
    # stop. This is exactly the "multiple gateways / port in use" trap.
    try:
        from flowly.config.loader import load_config
        _port = int(load_config().gateway.port or 18790)
    except Exception:
        _port = 18790
    _existing = _port_listener_pids(_port)
    if _existing:
        console.print(
            f"[green]✓[/green] A gateway is already running on port {_port} "
            f"(PID {', '.join(map(str, _existing))})."
        )
        console.print(
            "  [dim]Run[/dim] [cyan]flowly service status[/cyan] [dim]for details, or[/dim] "
            "[cyan]flowly service stop[/cyan] [dim]first to restart cleanly.[/dim]"
        )
        return

    try:
        if system == "darwin" and mac_plist:
            if not mac_plist.exists():
                console.print(f"[red]Service not installed: {mac_plist}[/red]")
                raise typer.Exit(1)
            _run_cmd(["launchctl", "load", str(mac_plist)], check=False)
            _run_cmd(["launchctl", "start", label], check=False)
            console.print(f"[green]✓[/green] Started service {label}")
            return
        if system == "linux":
            # Ensure linger so the service stays up after this session ends —
            # covers users who only ever run `service start` on an already
            # installed unit (no re-install).
            _ensure_linger_linux()
            _run_cmd(["systemctl", "--user", "start", label])
            console.print(f"[green]✓[/green] Started service {label}")
            return
        if system == "windows":
            if win_xml and not win_xml.exists():
                console.print("[red]Service not installed. Run 'flowly service install' first.[/red]")
                raise typer.Exit(1)
            _run_cmd(["schtasks", "/run", "/tn", label])
            console.print(f"[green]✓[/green] Started service {label}")
            return
    except Exception as e:
        console.print(f"[red]Failed to start service: {e}[/red]")
        raise typer.Exit(1)
    console.print(f"[red]Unsupported platform: {platform.system()}[/red]")
    raise typer.Exit(1)


@service_app.command("stop")
def service_stop(
    label: str = typer.Option(DEFAULT_SERVICE_LABEL, "--label", help="Service label"),
):
    """Stop background service."""
    mac_plist, linux_unit, win_xml = _service_paths(label)
    system = platform.system().lower()

    # Determine the port so we can force-kill if needed
    port = 18790
    if system == "darwin" and mac_plist:
        port = _extract_port_from_plist(mac_plist)
    elif system == "linux" and linux_unit:
        port = _extract_port_from_unit(linux_unit)
    elif system == "windows" and win_xml:
        port = _extract_port_from_win_xml(win_xml)

    try:
        if system == "darwin" and mac_plist:
            _run_cmd(["launchctl", "stop", label], check=False)
            _run_cmd(["launchctl", "unload", str(mac_plist)], check=False)
        elif system == "linux":
            _run_cmd(["systemctl", "--user", "stop", label], check=False)
        elif system == "windows":
            _run_cmd(["schtasks", "/end", "/tn", label], check=False)
        else:
            console.print(f"[red]Unsupported platform: {platform.system()}[/red]")
            raise typer.Exit(1)

        # Force-kill any remaining process on the port
        _kill_gateway_on_port(port)
        console.print(f"[green]✓[/green] Stopped service {label}")
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Failed to stop service: {e}[/red]")
        raise typer.Exit(1)


@service_app.command("restart")
def service_restart(
    label: str = typer.Option(DEFAULT_SERVICE_LABEL, "--label", help="Service label"),
):
    """Restart the gateway.

    Smart restart: detects how the gateway is running and uses the right
    mechanism for that mode. The three modes are mutually exclusive on a
    given host:

      1. ``launchd`` / ``systemd-user`` / Windows service — installed
         via ``flowly service install`` (or by Desktop's autostart
         flow). ``launchctl kickstart`` / ``systemctl restart`` /
         ``sc start`` bounces the process atomically and we wait for
         the gateway port to come back up before returning.
      2. Manual foreground (user ran ``flowly gateway`` in another
         terminal). No service manager owns the process; the only way
         to restart is from that terminal. We surface a clear hint
         instead of pretending we did something.
      3. Not running at all. Same hint: tell the user how to start
         the gateway.

    Before this change the command unconditionally did a launchd
    stop+start, which silently no-op'd in foreground mode — the user
    thought they'd restarted, but the same old process was still
    serving requests.
    """
    # Ensure linger too, in case the service was only ever `restart`ed (never
    # install/start) on this host — no-op on macOS/Windows and when it's already
    # enabled, so it's cheap to assert on every restart.
    _ensure_linger_linux()

    # Avoid asyncio.run inside Typer when an event loop is already
    # active (e.g. called from a TUI worker). The helper is async so
    # we always need a loop — either pick up the existing one or
    # create a fresh one.
    import asyncio
    from flowly.integrations.service_control import restart_gateway

    try:
        result = asyncio.run(restart_gateway(label=label))
    except RuntimeError:
        # Existing event loop (rare path: ``flowly service restart``
        # invoked from inside an already-running async caller).
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(restart_gateway(label=label))
        finally:
            loop.close()

    if result.ok:
        console.print(f"[green]✓[/green] {result.detail}")
        return
    if result.method == "no_service":
        console.print(f"[yellow]{result.detail}[/yellow]")
        raise typer.Exit(2)
    console.print(f"[red]{result.detail}[/red]")
    raise typer.Exit(1)


@service_app.command("status")
def service_status(
    label: str = typer.Option(DEFAULT_SERVICE_LABEL, "--label", help="Service label"),
):
    """Show service state and local health."""
    mac_plist, linux_unit, win_xml = _service_paths(label)
    system = platform.system().lower()

    if system == "darwin" and mac_plist:
        installed = mac_plist.exists()
        loaded = False
        pid = ""
        try:
            proc = _run_cmd(["launchctl", "list", label], check=False)
            loaded = proc.returncode == 0
            output = proc.stdout or ""
            for line in output.splitlines():
                if "pid" in line.lower():
                    pid = line.strip()
                    break
        except Exception:
            loaded = False
        port = _extract_port_from_plist(mac_plist)
        ok, health = _service_health(port)
        console.print(f"Service: [cyan]{label}[/cyan]")
        console.print(f"Installed: {'[green]yes[/green]' if installed else '[red]no[/red]'}")
        console.print(f"Loaded: {'[green]yes[/green]' if loaded else '[red]no[/red]'}")
        if pid:
            console.print(f"PID info: [dim]{pid}[/dim]")
        console.print(f"Health: {'[green]ok[/green]' if ok else '[yellow]down[/yellow]'} - {health}")
        if installed:
            console.print(f"[dim]File: {mac_plist}[/dim]")
        _print_port_diagnostics(port, installed=installed, service_running=loaded)
        return

    if system == "linux" and linux_unit:
        installed = linux_unit.exists()
        enabled = False
        active = False
        try:
            enabled = _run_cmd(["systemctl", "--user", "is-enabled", label], check=False).returncode == 0
            active = _run_cmd(["systemctl", "--user", "is-active", label], check=False).returncode == 0
        except Exception:
            pass
        port = _extract_port_from_unit(linux_unit)
        ok, health = _service_health(port)
        console.print(f"Service: [cyan]{label}[/cyan]")
        console.print(f"Installed: {'[green]yes[/green]' if installed else '[red]no[/red]'}")
        console.print(f"Enabled: {'[green]yes[/green]' if enabled else '[red]no[/red]'}")
        console.print(f"Active: {'[green]yes[/green]' if active else '[red]no[/red]'}")
        console.print(f"Health: {'[green]ok[/green]' if ok else '[yellow]down[/yellow]'} - {health}")
        if installed:
            console.print(f"[dim]File: {linux_unit}[/dim]")
        _print_port_diagnostics(port, installed=installed, service_running=active)
        return

    if system == "windows" and win_xml:
        installed = win_xml.exists()
        running = False
        status_text = "Unknown"
        try:
            proc = _run_cmd(
                ["schtasks", "/query", "/tn", label, "/fo", "CSV", "/nh"],
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                # CSV format: "task_name","Next Run","Status"
                parts = proc.stdout.strip().split(",")
                if len(parts) >= 3:
                    status_text = parts[2].strip().strip('"')
                    running = status_text.lower() == "running"
        except Exception:
            pass
        port = _extract_port_from_win_xml(win_xml)
        ok, health = _service_health(port)
        console.print(f"Service: [cyan]{label}[/cyan]")
        console.print(f"Installed: {'[green]yes[/green]' if installed else '[red]no[/red]'}")
        console.print(f"Status: {'[green]Running[/green]' if running else f'[yellow]{status_text}[/yellow]'}")
        console.print(f"Health: {'[green]ok[/green]' if ok else '[yellow]down[/yellow]'} - {health}")
        if installed:
            console.print(f"[dim]File: {win_xml}[/dim]")
        _print_port_diagnostics(port, installed=installed, service_running=running)
        return

    console.print(f"[red]Unsupported platform: {platform.system()}[/red]")
    raise typer.Exit(1)


@service_app.command("logs")
def service_logs(
    label: str = typer.Option(DEFAULT_SERVICE_LABEL, "--label", help="Service label"),
    follow: bool = typer.Option(True, "--follow/--no-follow", "-f", help="Follow logs in real time"),
    lines: int = typer.Option(200, "--lines", "-n", min=1, help="Number of lines to show"),
    stream: str = typer.Option(
        "both",
        "--stream",
        help="Log stream (macOS launchd logs only): out|err|both",
    ),
):
    """Show background service logs (real-time by default)."""
    system = platform.system().lower()

    if system == "darwin":
        stream = stream.lower().strip()
        if stream not in {"out", "err", "both"}:
            console.print("[red]Invalid --stream value. Use out, err, or both.[/red]")
            raise typer.Exit(1)

        log_dir = _get_log_dir()
        out_log = log_dir / "flowly-gateway.out.log"
        err_log = log_dir / "flowly-gateway.err.log"
        selected_files: list[Path] = []
        if stream in {"out", "both"}:
            selected_files.append(out_log)
        if stream in {"err", "both"}:
            selected_files.append(err_log)

        existing_files = [p for p in selected_files if p.exists()]
        missing_files = [p for p in selected_files if not p.exists()]
        for missing in missing_files:
            console.print(f"[yellow]Log file not found yet:[/yellow] {missing}")

        if not existing_files:
            console.print("[red]No log file available yet.[/red]")
            raise typer.Exit(1)

        if follow:
            console.print(
                f"[dim]Following logs ({', '.join(str(p) for p in existing_files)}). "
                "Press Ctrl+C to stop.[/dim]"
            )
            try:
                subprocess.run(
                    ["tail", "-n", str(lines), "-F", *[str(p) for p in existing_files]],
                    check=False,
                )
            except KeyboardInterrupt:
                return
            return

        for file_path in existing_files:
            console.print(f"\n[bold]{file_path}[/bold]")
            proc = _run_cmd(["tail", "-n", str(lines), str(file_path)], check=False)
            if proc.stdout:
                console.print(proc.stdout.rstrip("\n"))
        return

    if system == "linux":
        # Try log files first (same as macOS), fall back to journalctl
        log_dir = _get_log_dir()
        out_log = log_dir / "flowly-gateway.out.log"
        err_log = log_dir / "flowly-gateway.err.log"
        log_files_exist = out_log.exists() or err_log.exists()

        if log_files_exist:
            # Use file-based logs (same logic as macOS)
            selected_files: list[Path] = []
            if stream in {"out", "both"}:
                selected_files.append(out_log)
            if stream in {"err", "both"}:
                selected_files.append(err_log)

            existing_files = [p for p in selected_files if p.exists()]
            if existing_files:
                if follow:
                    console.print(
                        f"[dim]Following logs ({', '.join(str(p) for p in existing_files)}). "
                        "Press Ctrl+C to stop.[/dim]"
                    )
                    try:
                        subprocess.run(
                            ["tail", "-n", str(lines), "-F", *[str(p) for p in existing_files]],
                            check=False,
                        )
                    except KeyboardInterrupt:
                        return
                    return

                for file_path in existing_files:
                    console.print(f"\n[bold]{file_path}[/bold]")
                    proc = _run_cmd(["tail", "-n", str(lines), str(file_path)], check=False)
                    if proc.stdout:
                        console.print(proc.stdout.rstrip("\n"))
                return

        # Fallback: journalctl (when using systemd without StandardOutput redirect)
        args = ["journalctl", "--user", "-u", label, "-n", str(lines), "--no-pager"]
        if follow:
            args.append("-f")
            console.print(f"[dim]Following journal logs for {label}. Press Ctrl+C to stop.[/dim]")
        proc = _run_cmd(args, check=False)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            console.print(f"[red]Failed to read logs: {err}[/red]")
            console.print(f"[dim]Tip: check {log_dir} for log files[/dim]")
            raise typer.Exit(1)
        if proc.stdout:
            console.print(proc.stdout.rstrip("\n"))
        return

    if system == "windows":
        log_dir = _get_log_dir()
        out_log = log_dir / "flowly-gateway.out.log"
        err_log = log_dir / "flowly-gateway.err.log"
        selected_files: list[Path] = []
        if stream in {"out", "both"}:
            selected_files.append(out_log)
        if stream in {"err", "both"}:
            selected_files.append(err_log)

        existing_files = [p for p in selected_files if p.exists()]
        missing_files = [p for p in selected_files if not p.exists()]
        for missing in missing_files:
            console.print(f"[yellow]Log file not found yet:[/yellow] {missing}")

        if not existing_files:
            console.print("[red]No log file available yet.[/red]")
            raise typer.Exit(1)

        if follow:
            console.print(
                f"[dim]Following logs ({', '.join(str(p) for p in existing_files)}). "
                "Press Ctrl+C to stop.[/dim]"
            )
            # Use PowerShell Get-Content -Wait for tail -f equivalent on Windows
            ps_files = ", ".join(f'"{p}"' for p in existing_files)
            ps_cmd = f"Get-Content -Path {ps_files} -Tail {lines} -Wait"
            try:
                subprocess.run(
                    ["powershell", "-Command", ps_cmd],
                    check=False,
                )
            except KeyboardInterrupt:
                return
            return

        # Read last N lines using PowerShell
        for file_path in existing_files:
            console.print(f"\n[bold]{file_path}[/bold]")
            ps_cmd = f'Get-Content -Path "{file_path}" -Tail {lines}'
            proc = _run_cmd(["powershell", "-Command", ps_cmd], check=False)
            if proc.stdout:
                console.print(proc.stdout.rstrip("\n"))
        return

    console.print(f"[red]Unsupported platform: {platform.system()}[/red]")
    raise typer.Exit(1)


@service_app.command("uninstall")
def service_uninstall(
    label: str = typer.Option(DEFAULT_SERVICE_LABEL, "--label", help="Service label"),
):
    """Uninstall background service definition."""
    mac_plist, linux_unit, win_xml = _service_paths(label)
    system = platform.system().lower()

    try:
        if system == "darwin" and mac_plist:
            _run_cmd(["launchctl", "stop", label], check=False)
            _run_cmd(["launchctl", "unload", str(mac_plist)], check=False)
            if mac_plist.exists():
                mac_plist.unlink()
            console.print(f"[green]✓[/green] Uninstalled service {label}")
            return
        if system == "linux" and linux_unit:
            _run_cmd(["systemctl", "--user", "stop", label], check=False)
            _run_cmd(["systemctl", "--user", "disable", label], check=False)
            if linux_unit.exists():
                linux_unit.unlink()
            _run_cmd(["systemctl", "--user", "daemon-reload"], check=False)
            console.print(f"[green]✓[/green] Uninstalled service {label}")
            return
        if system == "windows" and win_xml:
            _run_cmd(["schtasks", "/end", "/tn", label], check=False)
            _run_cmd(["schtasks", "/delete", "/tn", label, "/f"], check=False)
            if win_xml.exists():
                win_xml.unlink()
            # Remove the hidden-launch VBScript next to the task XML.
            (win_xml.parent / f"{label}.vbs").unlink(missing_ok=True)
            # Also remove the Startup-folder fallback launcher, if present.
            startup_cmd = (
                Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
                / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{label}.cmd"
            )
            startup_cmd.unlink(missing_ok=True)
            console.print(f"[green]✓[/green] Uninstalled service {label}")
            return
    except Exception as e:
        console.print(f"[red]Failed to uninstall service: {e}[/red]")
        raise typer.Exit(1)

    console.print(f"[red]Unsupported platform: {platform.system()}[/red]")
    raise typer.Exit(1)


