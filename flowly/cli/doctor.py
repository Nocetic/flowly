"""
flowly doctor — configuration and runtime health checker.

Architecture:
  - Each check is an isolated function returning a DoctorResult
  - DoctorRunner collects results, prints a report, optionally auto-repairs
  - --fix flag auto-approves all repairs
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


def _doctor_config_path() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "config.json"


def _doctor_data_dir() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home()

from rich.console import Console

console = Console()


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class Status(str, Enum):
    OK      = "ok"
    WARN    = "warn"
    ERROR   = "error"
    FIXED   = "fixed"
    SKIPPED = "skipped"


@dataclass
class DoctorResult:
    name: str
    status: Status
    message: str
    detail: str = ""
    fixable: bool = False   # can be auto-repaired with --fix


@dataclass
class DoctorContext:
    fix: bool = False                       # --fix flag
    results: list[DoctorResult] = field(default_factory=list)
    config_path: Path = field(default_factory=lambda: _doctor_config_path())
    data_dir: Path = field(default_factory=lambda: _doctor_data_dir())
    raw_config: dict[str, Any] | None = None   # parsed JSON, set by config check

    def record(self, result: DoctorResult) -> None:
        self.results.append(result)

    def ok(self, name: str, message: str) -> None:
        self.record(DoctorResult(name, Status.OK, message))

    def warn(self, name: str, message: str, detail: str = "", fixable: bool = False) -> None:
        self.record(DoctorResult(name, Status.WARN, message, detail, fixable))

    def error(self, name: str, message: str, detail: str = "", fixable: bool = False) -> None:
        self.record(DoctorResult(name, Status.ERROR, message, detail, fixable))

    def fixed(self, name: str, message: str) -> None:
        self.record(DoctorResult(name, Status.FIXED, message))

    def skipped(self, name: str, message: str) -> None:
        self.record(DoctorResult(name, Status.SKIPPED, message))


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_state_directory(ctx: DoctorContext) -> None:
    """~/.flowly/ must exist and be readable/writable with correct permissions."""
    d = ctx.data_dir

    if not d.exists():
        if ctx.fix:
            d.mkdir(parents=True, mode=0o700)
            from flowly.utils.file_security import secure_dir
            secure_dir(d)  # POSIX mode above; real owner-only ACL on Windows
            ctx.fixed("state_dir", f"Created {d}")
        else:
            ctx.error("state_dir", f"{d} does not exist", fixable=True)
        return

    if not os.access(d, os.W_OK):
        ctx.error("state_dir", f"{d} is not writable")
        return

    ctx.ok("state_dir", str(d))


def check_config_file(ctx: DoctorContext) -> None:
    """Config file must exist, be valid JSON, and have correct permissions."""
    path = ctx.config_path

    if not path.exists():
        if ctx.fix:
            # Bootstrap minimal config
            from flowly.config.loader import save_config
            from flowly.config.schema import Config
            save_config(Config(), path)
            ctx.fixed("config_file", f"Created default config at {path}")
        else:
            ctx.error("config_file", f"{path} does not exist", fixable=True)
        return

    # Validate JSON
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        ctx.raw_config = raw
    except json.JSONDecodeError as e:
        ctx.error("config_file", f"Config is invalid JSON: {e}")
        return

    ctx.ok("config_file", str(path))


def check_config_validity(ctx: DoctorContext) -> None:
    """Config must parse cleanly against the Pydantic schema."""
    if ctx.raw_config is None:
        ctx.skipped("config_validity", "Skipped (config file check failed)")
        return

    try:
        from flowly.config.loader import convert_keys
        from flowly.config.schema import Config
        Config.model_validate(convert_keys(ctx.raw_config))
        ctx.ok("config_validity", "Config schema valid")
    except Exception as e:
        ctx.error("config_validity", f"Config schema validation failed: {e}")


def check_duplicate_keys(ctx: DoctorContext) -> None:
    """Detect camelCase/snake_case duplicate keys (e.g. apiKey + api_key).

    When both forms exist in the same object Python's json.load keeps the last
    one, so the earlier value silently wins — a hard-to-debug bug.
    """
    if ctx.raw_config is None:
        ctx.skipped("duplicate_keys", "Skipped (config not loaded)")
        return

    from flowly.config.loader import camel_to_snake

    def find_dupes(obj: Any, path: str = "") -> list[str]:
        if not isinstance(obj, dict):
            return []
        seen: dict[str, str] = {}   # snake_key → original key
        dupes = []
        for k in obj:
            norm = camel_to_snake(k)
            if norm in seen:
                dupes.append(f"{path}.{seen[norm]} + {path}.{k}" if path else f"{seen[norm]} + {k}")
            else:
                seen[norm] = k
        for k, v in obj.items():
            dupes.extend(find_dupes(v, f"{path}.{k}" if path else k))
        return dupes

    dupes = find_dupes(ctx.raw_config)
    if dupes:
        detail = "\n".join(f"  • {d}" for d in dupes)
        if ctx.fix:
            _remove_duplicate_keys(ctx.config_path)
            ctx.fixed("duplicate_keys", f"Removed duplicate keys:\n{detail}")
        else:
            ctx.error(
                "duplicate_keys",
                f"Found {len(dupes)} duplicate key(s) — last value silently wins",
                detail,
                fixable=True,
            )
    else:
        ctx.ok("duplicate_keys", "No duplicate camelCase/snake_case keys")


def _remove_duplicate_keys(config_path: Path) -> None:
    """Keep camelCase version of each key, remove snake_case duplicates."""
    import secrets
    from flowly.config.loader import camel_to_snake

    def dedup(obj: Any) -> Any:
        if not isinstance(obj, dict):
            return obj
        seen: dict[str, str] = {}   # snake_norm → first key found
        result = {}
        for k, v in obj.items():
            norm = camel_to_snake(k)
            if norm not in seen:
                seen[norm] = k
                result[k] = dedup(v)
            # else: drop duplicate
        return result

    with open(config_path, encoding="utf-8") as f:
        raw = json.load(f)

    deduped = dedup(raw)
    tmp = config_path.with_suffix(f".tmp.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(deduped, indent=4), encoding="utf-8")
    os.replace(str(tmp), str(config_path))


def check_api_keys(ctx: DoctorContext) -> None:
    """At least one LLM provider API key must be configured."""
    if ctx.raw_config is None:
        ctx.skipped("api_keys", "Skipped (config not loaded)")
        return

    providers = ctx.raw_config.get("providers", {})

    def key_set(section: str) -> bool:
        return bool(providers.get(section, {}).get("apiKey") or providers.get(section, {}).get("api_key"))

    def base_set(section: str) -> bool:
        return bool(
            providers.get(section, {}).get("apiBase")
            or providers.get(section, {}).get("api_base")
        )

    has_key = (
        key_set("anthropic")
        or key_set("openai")
        or key_set("openrouter")
        or key_set("gemini")
        or key_set("groq")
        or key_set("xai")
        or key_set("zhipu")
        or key_set("sakana")
        or base_set("vllm")
    )

    if has_key:
        ctx.ok("api_keys", "At least one provider API key is set")
    else:
        ctx.warn(
            "api_keys",
            "No LLM provider API key configured — agent will fail to respond",
            "Set openrouter.apiKey (or anthropic/openai/etc.) in ~/.flowly/config.json",
        )


def check_model(ctx: DoctorContext) -> None:
    """Model field must be non-empty and follow provider/model format."""
    if ctx.raw_config is None:
        ctx.skipped("model", "Skipped (config not loaded)")
        return

    model: str = (
        ctx.raw_config.get("agents", {})
        .get("defaults", {})
        .get("model", "")
    )

    if not model:
        ctx.error("model", "agents.defaults.model is empty — agent cannot start")
        return

    if "/" not in model:
        ctx.warn(
            "model",
            f"Model '{model}' has no provider prefix",
            "Expected format: provider/model-name (e.g. openrouter/anthropic/claude-haiku-4.5)",
        )
        return

    ctx.ok("model", model)


def check_workspace(ctx: DoctorContext) -> None:
    """Workspace directory must exist and be writable."""
    if ctx.raw_config is None:
        ctx.skipped("workspace", "Skipped (config not loaded)")
        return

    workspace_raw: str = (
        ctx.raw_config.get("agents", {})
        .get("defaults", {})
        .get("workspace", "~/.flowly/workspace")
    )
    workspace = Path(workspace_raw).expanduser().resolve()

    if not workspace.exists():
        if ctx.fix:
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "memory").mkdir(exist_ok=True)
            (workspace / "skills").mkdir(exist_ok=True)
            (workspace / "personas").mkdir(exist_ok=True)
            ctx.fixed("workspace", f"Created workspace at {workspace}")
        else:
            ctx.error("workspace", f"Workspace {workspace} does not exist", fixable=True)
        return

    if not os.access(workspace, os.W_OK):
        ctx.error("workspace", f"Workspace {workspace} is not writable")
        return

    # Ensure subdirectories exist
    missing = [d for d in ("memory", "skills", "personas") if not (workspace / d).exists()]
    if missing:
        if ctx.fix:
            for d in missing:
                (workspace / d).mkdir(exist_ok=True)
            ctx.fixed("workspace", f"Created missing subdirs: {', '.join(missing)}")
        else:
            ctx.warn(
                "workspace",
                f"Missing subdirectories: {', '.join(missing)}",
                fixable=True,
            )
        return

    ctx.ok("workspace", str(workspace))


def check_gateway(ctx: DoctorContext) -> None:
    """Gateway should be reachable on its configured port."""
    port = 18790
    if ctx.raw_config:
        port = ctx.raw_config.get("gateway", {}).get("port", 18790)

    import urllib.request
    import urllib.error
    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        body = json.loads(req.read().decode())
        status_str = body.get("status", "?")
        ctx.ok("gateway", f"Running on port {port} — status: {status_str}")
    except (urllib.error.URLError, OSError):
        ctx.warn("gateway", f"Gateway not responding on port {port}", "Run: flowly service start")


def check_service_installation(ctx: DoctorContext) -> None:
    """Background service should be installed."""
    system = platform.system().lower()

    if system == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "ai.flowly.gateway.plist"
        if plist.exists():
            ctx.ok("service_install", f"LaunchAgent installed: {plist.name}")
        else:
            ctx.warn(
                "service_install",
                "LaunchAgent not installed — gateway won't auto-start on login",
                "Run: flowly service install",
            )

    elif system == "linux":
        unit = Path.home() / ".config" / "systemd" / "user" / "ai.flowly.gateway.service"
        if unit.exists():
            ctx.ok("service_install", f"systemd unit installed: {unit.name}")
        else:
            ctx.warn(
                "service_install",
                "systemd unit not installed — gateway won't auto-start",
                "Run: flowly service install",
            )

    elif system == "windows":
        ctx.skipped("service_install", "Windows service check not implemented")

    else:
        ctx.skipped("service_install", f"Unsupported platform: {platform.system()}")


def check_service_executable(ctx: DoctorContext) -> None:
    """Service definition must point to a valid flowly executable."""
    system = platform.system().lower()

    if system == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "ai.flowly.gateway.plist"
        if not plist.exists():
            ctx.skipped("service_exec", "No LaunchAgent found")
            return
        try:
            import plistlib
            data = plistlib.loads(plist.read_bytes())
            args: list[str] = data.get("ProgramArguments", [])
            exe = args[0] if args else ""
            exe_path = Path(exe)
            if not exe_path.exists():
                ctx.error(
                    "service_exec",
                    f"Service executable not found: {exe}",
                    "Run: flowly service install  (reinstall to update path)",
                )
                return
            ctx.ok("service_exec", f"Executable OK: {exe}")
        except Exception as e:
            ctx.warn("service_exec", f"Could not read plist: {e}")

    elif system == "linux":
        unit = Path.home() / ".config" / "systemd" / "user" / "ai.flowly.gateway.service"
        if not unit.exists():
            ctx.skipped("service_exec", "No systemd unit found")
            return
        content = unit.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.startswith("ExecStart="):
                exe = line.split("=", 1)[1].strip().split()[0]
                if not Path(exe).exists():
                    ctx.error(
                        "service_exec",
                        f"Service executable not found: {exe}",
                        "Run: flowly service install",
                    )
                    return
                ctx.ok("service_exec", f"Executable OK: {exe}")
                return
        ctx.warn("service_exec", "Could not find ExecStart in systemd unit")

    else:
        ctx.skipped("service_exec", f"Unsupported platform: {platform.system()}")


def check_account_tokens(ctx: DoctorContext) -> None:
    """Keychain account credentials — presence + freshness.

    Read-only; surfaces guidance toward ``flowly login`` /
    ``flowly login --repair`` based on what's missing.
    """
    from flowly.account.health import check_token_state
    state = check_token_state()
    if not state.has_account:
        ctx.warn(
            "account_tokens",
            "Not signed in — Flowly hosted, iOS pairing, relay all unavailable",
            "Run: flowly login",
        )
        return
    who = state.email or state.user_id
    if not state.healthy:
        ctx.warn(
            "account_tokens",
            f"Account ({who}) is signed in but token has expired",
            "Tokens refresh transparently on next use; "
            "if errors persist run `flowly logout && flowly login`",
        )
        return
    mins = state.seconds_left // 60
    detail = f"{mins} min until refresh" if mins > 0 else f"{state.seconds_left}s until refresh"
    ctx.ok("account_tokens", f"Signed in as {who} ({detail})")


def check_relay_health(ctx: DoctorContext) -> None:
    """End-to-end relay readiness — every field the WS dial needs.

    Complements ``check_channel_config`` which only asks "is any
    channel enabled". This one asserts that ``channels.web`` is
    *complete* enough to actually connect.
    """
    from flowly.account.health import check_relay_state
    state = check_relay_state()
    if state.healthy:
        ctx.ok(
            "relay",
            f"server_id={state.server_id} · ready to dial",
        )
        return
    # Distinguish "user hasn't logged in yet" from "config got corrupted".
    # Both surface the same fix but with different urgency wording.
    from flowly.account.health import check_token_state
    if not check_token_state().has_account:
        ctx.warn(
            "relay",
            "Relay disabled — sign in to enable iOS / desktop / Android sync",
            "Run: flowly login",
        )
    else:
        ctx.warn(
            "relay",
            f"Relay config incomplete — {state.reason}",
            "Run: flowly login --repair  (re-wire using existing tokens)",
        )


def check_provider_corruption_check(ctx: DoctorContext) -> None:
    """Cross-slot leak detection — Flowly hosted creds in a BYOK slot.

    Caused by legacy desktop versions (or partial restore) writing
    ``serverId:gatewayAuthToken`` into ``providers.openrouter.apiKey``
    and ``useflowlyapp.com`` into the matching ``apiBase``. The
    runtime resolver works around it (see active_provider.py:135) but
    the data on disk is misleading and shows up as a fake "I have an
    OpenRouter key configured" signal to ``check_api_keys``.
    """
    from flowly.account.health import check_provider_corruption
    issues = check_provider_corruption()
    if not issues:
        ctx.ok("provider_corruption", "No cross-slot leaks detected")
        return
    slots = sorted({i.slot for i in issues})
    detail_lines = [
        f"providers.{i.slot}.{i.field} — {i.issue}" for i in issues
    ]
    fix_lines = [
        f"flowly setup byok {s} --key <real-{s}-key>"
        for s in slots
    ]
    ctx.warn(
        "provider_corruption",
        f"{len(issues)} stale field(s) in {len(slots)} BYOK slot(s) — "
        "likely leftover from a previous Flowly version",
        "\n".join(detail_lines + [""] + ["Fix:"] + fix_lines
                  + ["or edit ~/.flowly/config.json manually"]),
    )


def check_channel_config(ctx: DoctorContext) -> None:
    """At least one channel should be enabled."""
    if ctx.raw_config is None:
        ctx.skipped("channels", "Skipped (config not loaded)")
        return

    channels = ctx.raw_config.get("channels", {})
    enabled = [name for name, cfg in channels.items() if isinstance(cfg, dict) and cfg.get("enabled")]

    if enabled:
        ctx.ok("channels", f"Enabled: {', '.join(enabled)}")
    else:
        ctx.warn(
            "channels",
            "No channels enabled — bot won't receive messages from any platform",
            "Enable at least one channel in ~/.flowly/config.json or via flowly setup",
        )


def check_unknown_config_keys(ctx: DoctorContext) -> None:
    """Warn about top-level config keys that Flowly doesn't recognise."""
    if ctx.raw_config is None:
        ctx.skipped("unknown_keys", "Skipped (config not loaded)")
        return

    from flowly.config.loader import camel_to_snake
    from flowly.config.schema import Config

    known_snake = set(Config.model_fields.keys())
    unknown = []
    for k in ctx.raw_config:
        if camel_to_snake(k) not in known_snake:
            unknown.append(k)

    if unknown:
        ctx.warn(
            "unknown_keys",
            f"{len(unknown)} unrecognised top-level key(s): {', '.join(unknown)}",
            "These keys are preserved but ignored by Flowly — check for typos",
        )
    else:
        ctx.ok("unknown_keys", "All config keys recognised")


def check_gateway_security(ctx: DoctorContext) -> None:
    """Gateway with public binding should have auth token configured.

    A gateway bound to 0.0.0.0 without an auth token is openly accessible
    to anyone on the same network.
    """
    if ctx.raw_config is None:
        ctx.skipped("gateway_security", "Skipped (config not loaded)")
        return

    gw = ctx.raw_config.get("gateway", {})
    host: str = gw.get("host", "127.0.0.1")
    token: str = gw.get("token", "") or gw.get("auth_token", "") or gw.get("authToken", "")

    public_binding = host not in ("127.0.0.1", "localhost", "::1")

    if public_binding and not token:
        ctx.error(
            "gateway_security",
            f"Gateway bound to {host} without an auth token — OPEN TO NETWORK",
            "Set gateway.token in config.json or restrict host to 127.0.0.1",
        )
    elif public_binding and token:
        ctx.warn(
            "gateway_security",
            f"Gateway bound to {host} (public) — ensure firewall rules are in place",
        )
    else:
        ctx.ok("gateway_security", f"Gateway binding: {host} (localhost only)")


def check_memory_system(ctx: DoctorContext) -> None:
    """Check that the workspace memory directory and MEMORY.md index exist."""
    if ctx.raw_config is None:
        ctx.skipped("memory", "Skipped (config not loaded)")
        return

    workspace_raw: str = (
        ctx.raw_config.get("agents", {})
        .get("defaults", {})
        .get("workspace", "~/.flowly/workspace")
    )
    workspace = Path(workspace_raw).expanduser().resolve()
    memory_dir = workspace / "memory"
    memory_index = memory_dir / "MEMORY.md"

    if not memory_dir.exists():
        if ctx.fix:
            memory_dir.mkdir(parents=True, exist_ok=True)
            ctx.fixed("memory", f"Created memory directory: {memory_dir}")
        else:
            ctx.warn("memory", f"Memory directory missing: {memory_dir}", fixable=True)
        return

    if not memory_index.exists():
        ctx.warn(
            "memory",
            "MEMORY.md index not found — agent will start without persistent memory",
            f"Expected: {memory_index}",
        )
        return

    ctx.ok("memory", f"Memory index present: {memory_index}")


def check_session_store(ctx: DoctorContext) -> None:
    """Session transcript files referenced in sessions.json should exist on disk."""
    sessions_file = ctx.data_dir / "sessions.json"
    if not sessions_file.exists():
        ctx.skipped("session_store", "No sessions.json found (no sessions yet)")
        return

    try:
        with open(sessions_file, encoding="utf-8") as f:
            sessions = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        ctx.warn("session_store", f"Could not parse sessions.json: {e}")
        return

    if not isinstance(sessions, list):
        ctx.skipped("session_store", "sessions.json format unrecognised")
        return

    missing = []
    for s in sessions:
        if isinstance(s, dict):
            transcript = s.get("transcript") or s.get("transcriptPath")
            if transcript and not Path(transcript).expanduser().exists():
                missing.append(str(transcript))

    if missing:
        ctx.warn(
            "session_store",
            f"{len(missing)} session transcript(s) missing from disk",
            "\n".join(f"  • {p}" for p in missing[:5]) + ("  …" if len(missing) > 5 else ""),
        )
    else:
        count = len(sessions)
        ctx.ok("session_store", f"{count} session record(s) — transcripts intact")


def check_linux_linger(ctx: DoctorContext) -> None:
    """On Linux, systemd linger should be enabled so the user service survives logout."""
    if platform.system().lower() != "linux":
        return  # silently skip on non-Linux

    unit = Path.home() / ".config" / "systemd" / "user" / "ai.flowly.gateway.service"
    if not unit.exists():
        ctx.skipped("linux_linger", "systemd unit not installed — skipping linger check")
        return

    user = os.environ.get("USER") or os.environ.get("LOGNAME", "")
    linger_file = Path(f"/var/lib/systemd/linger/{user}")
    if linger_file.exists():
        ctx.ok("linux_linger", f"systemd linger enabled for {user}")
    else:
        if ctx.fix:
            result = subprocess.run(
                ["loginctl", "enable-linger", user],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                ctx.fixed("linux_linger", f"Enabled systemd linger for {user}")
            else:
                ctx.warn("linux_linger", f"Could not enable linger: {result.stderr.strip()}")
        else:
            ctx.warn(
                "linux_linger",
                f"systemd linger not enabled — service stops on logout",
                f"Run: loginctl enable-linger {user}",
                fixable=True,
            )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

CHECKS = [
    check_state_directory,
    check_config_file,
    check_config_validity,
    check_duplicate_keys,
    check_unknown_config_keys,
    check_api_keys,
    check_provider_corruption_check,
    check_model,
    check_workspace,
    check_memory_system,
    check_account_tokens,
    check_relay_health,
    check_gateway_security,
    check_service_installation,
    check_service_executable,
    check_linux_linger,
    check_gateway,
    check_channel_config,
    check_session_store,
]


def run_doctor(fix: bool = False) -> int:
    """Run all checks. Returns exit code (0 = all ok/fixed, 1 = errors remain)."""
    ctx = DoctorContext(fix=fix)

    if fix:
        console.print("[bold cyan]flowly doctor --fix[/bold cyan]  (auto-repair mode)\n")
    else:
        console.print("[bold cyan]flowly doctor[/bold cyan]\n")

    for check in CHECKS:
        check(ctx)

    _print_report(ctx)

    errors = [r for r in ctx.results if r.status == Status.ERROR]
    return 1 if errors else 0


def _print_report(ctx: DoctorContext) -> None:
    STATUS_ICON = {
        Status.OK:      "[green]  ✓[/green]",
        Status.WARN:    "[yellow]  ⚠[/yellow]",
        Status.ERROR:   "[red]  ✗[/red]",
        Status.FIXED:   "[cyan]  ✦[/cyan]",
        Status.SKIPPED: "[dim]  -[/dim]",
    }

    for r in ctx.results:
        icon = STATUS_ICON[r.status]
        console.print(f"{icon}  [bold]{r.name}[/bold]  {r.message}")
        if r.detail:
            for line in r.detail.splitlines():
                console.print(f"      [dim]{line}[/dim]")
        if r.fixable and not ctx.fix and r.status in (Status.WARN, Status.ERROR):
            console.print("      [dim]→ run with --fix to auto-repair[/dim]")

    # Summary line
    counts = {s: sum(1 for r in ctx.results if r.status == s) for s in Status}
    parts = []
    if counts[Status.ERROR]:
        parts.append(f"[red]{counts[Status.ERROR]} error(s)[/red]")
    if counts[Status.WARN]:
        parts.append(f"[yellow]{counts[Status.WARN]} warning(s)[/yellow]")
    if counts[Status.FIXED]:
        parts.append(f"[cyan]{counts[Status.FIXED]} fixed[/cyan]")
    if counts[Status.OK]:
        parts.append(f"[green]{counts[Status.OK]} ok[/green]")

    console.print()
    console.print("  " + "  ·  ".join(parts) if parts else "  [green]All checks passed.[/green]")

    if not ctx.fix:
        fixable = [r for r in ctx.results if r.fixable and r.status in (Status.WARN, Status.ERROR)]
        if fixable:
            console.print(f"\n  [dim]Run [bold]flowly doctor --fix[/bold] to auto-repair {len(fixable)} issue(s)[/dim]")
