"""Offline, side-effect-free checks for ``flowly doctor``."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shlex
import sqlite3
import stat
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flowly.diagnostics.config import find_unknown_keys, read_config_snapshot
from flowly.diagnostics.models import DoctorCheck, DoctorContext, RepairRisk

_WORKSPACE_FILES = ("AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "HEARTBEAT.md")
_FLOWLY_BEARER_RE = re.compile(r"^[A-Za-z0-9_-]{12,32}:[0-9a-f]{32,128}$")
_BYOK_SLOTS = (
    "openrouter",
    "anthropic",
    "openai",
    "xai",
    "gemini",
    "groq",
    "zhipu",
    "sakana",
    "vllm",
)
_CREDENTIAL_STORE_PROVIDERS = {"openai_codex", "xai_oauth", "zai_coding"}
_MAX_SESSION_FILES = 2_000
_MAX_SESSION_LINES = 200_000
_PROVIDER_BASES = {
    "flowly": "https://useflowlyapp.com/api/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "xai": "https://api.x.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "sakana": "https://api.sakana.ai/v1",
}


def _mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _is_private(path: Path, *, directory: bool = False) -> bool:
    if os.name == "nt":
        # POSIX mode bits do not describe Windows ACLs. Do not claim an ACL is
        # private until the Windows-specific check is implemented.
        return False
    mode = _mode(path)
    if mode is None:
        return False
    forbidden = stat.S_IRWXG | stat.S_IRWXO
    required = stat.S_IRWXU if directory else stat.S_IRUSR | stat.S_IWUSR
    return not (mode & forbidden) and (mode & required) == required


def check_state_directory(ctx: DoctorContext) -> None:
    path = ctx.data_dir
    if not path.exists():
        ctx.error(
            "state_dir",
            f"State directory does not exist: {path}",
            fixable=True,
            risk=RepairRisk.LOW,
            repair_command="flowly doctor --fix",
        )
        return
    if not path.is_dir():
        ctx.error("state_dir", f"State path is not a directory: {path}")
        return
    missing = [
        label
        for label, flag in (("read", os.R_OK), ("write", os.W_OK), ("traverse", os.X_OK))
        if not os.access(path, flag)
    ]
    if missing:
        ctx.error("state_dir", f"State directory lacks {', '.join(missing)} access: {path}")
        return
    if path.is_symlink():
        ctx.warn(
            "state_dir",
            f"State directory is a symbolic link: {path}",
            "Verify that the target is trusted and private.",
        )
        return
    if os.name != "nt" and not _is_private(path, directory=True):
        ctx.warn(
            "state_dir",
            f"State directory permissions are broader than owner-only: {oct(_mode(path) or 0)}",
            fixable=True,
            risk=RepairRisk.LOW,
            repair_command="flowly doctor --fix",
        )
        return
    if os.name == "nt":
        ctx.warn(
            "state_dir",
            "Windows ACL privacy was not verified",
            "Filesystem access is available, but ACL inspection requires the Windows platform check.",
        )
        return
    ctx.ok("state_dir", str(path))


def check_config_file(ctx: DoctorContext) -> None:
    snapshot = read_config_snapshot(ctx.config_path)
    ctx.raw_config = snapshot.raw
    ctx.config = snapshot.config
    ctx.config_error = snapshot.error
    ctx.duplicate_keys = snapshot.duplicates

    if not ctx.config_path.exists():
        ctx.error(
            "config_file",
            f"Config does not exist: {ctx.config_path}",
            fixable=True,
            risk=RepairRisk.LOW,
            repair_command="flowly doctor --fix",
        )
        return
    if snapshot.raw is None:
        backup = ctx.config_path.with_suffix(ctx.config_path.suffix + ".bak")
        backup_snapshot = read_config_snapshot(backup) if backup.is_file() else None
        if backup_snapshot is not None and backup_snapshot.config is not None:
            ctx.error(
                "config_file",
                f"Config cannot be parsed: {ctx.config_path}",
                snapshot.error + f"\nValidated backup available: {backup}",
                fixable=True,
                risk=RepairRisk.HIGH,
                repair_command="flowly doctor --repair config_backup",
            )
        else:
            ctx.error("config_file", f"Config cannot be parsed: {ctx.config_path}", snapshot.error)
        return
    ctx.ok("config_file", str(ctx.config_path))


def check_config_permissions(ctx: DoctorContext) -> None:
    path = ctx.config_path
    if not path.exists() or not path.is_file():
        ctx.skipped("config_permissions", "Config file is unavailable")
        return
    if path.is_symlink():
        ctx.warn(
            "config_permissions",
            f"Config is a symbolic link: {path}",
            "Automatic permission repair must never follow this link.",
        )
        return
    if os.name == "nt":
        ctx.warn(
            "config_permissions",
            "Windows config ACL privacy was not verified",
            "Doctor will not claim the credential-bearing file is private without reading its ACL.",
        )
        return
    insecure: list[Path] = []
    if not _is_private(path):
        insecure.append(path)
    backup = path.with_suffix(path.suffix + ".bak")
    if backup.is_symlink():
        ctx.error("config_permissions", f"Config backup is a symbolic link: {backup}")
        return
    if backup.is_file() and not _is_private(backup):
        insecure.append(backup)
    if insecure:
        ctx.error(
            "config_permissions",
            f"{len(insecure)} config file(s) may expose credentials",
            "\n".join(f"• {item}: {oct(_mode(item) or 0)}" for item in insecure),
            fixable=True,
            risk=RepairRisk.LOW,
            repair_command="flowly doctor --fix",
        )
    else:
        count = 2 if backup.is_file() else 1
        ctx.ok("config_permissions", f"Private permissions on {count} config file(s)")


def check_config_validity(ctx: DoctorContext) -> None:
    if ctx.raw_config is None:
        ctx.skipped("config_validity", "Config could not be parsed")
    elif ctx.config is None:
        ctx.error("config_validity", "Config schema validation failed", ctx.config_error)
    else:
        ctx.ok("config_validity", "Config schema valid")


def check_duplicate_keys(ctx: DoctorContext) -> None:
    if ctx.raw_config is None:
        ctx.skipped("duplicate_keys", "Config could not be parsed")
        return
    if not ctx.duplicate_keys:
        ctx.ok("duplicate_keys", "No exact or schema-alias duplicate keys")
        return
    ctx.error(
        "duplicate_keys",
        f"Found {len(ctx.duplicate_keys)} duplicate key collision(s)",
        "\n".join(f"• {item}" for item in ctx.duplicate_keys),
        fixable=True,
        risk=RepairRisk.MEDIUM,
        repair_command="flowly doctor --repair config_duplicates",
    )


def check_unknown_config_keys(ctx: DoctorContext) -> None:
    if ctx.raw_config is None:
        ctx.skipped("unknown_keys", "Config could not be parsed")
        return
    unknown = find_unknown_keys(ctx.raw_config)
    if unknown:
        ctx.warn(
            "unknown_keys",
            f"Found {len(unknown)} unrecognized config key(s)",
            "\n".join(f"• {item}" for item in unknown[:50]),
        )
    else:
        ctx.ok("unknown_keys", "All schema-controlled keys are recognized")


def check_provider(ctx: DoctorContext) -> None:
    config = ctx.config
    if config is None:
        ctx.skipped("provider", "Config is not valid")
        return
    active = (config.providers.active or "").strip()
    resolved = _static_provider(ctx)
    if active:
        if resolved is not None and resolved[0] == active:
            ctx.ok("provider", f"Explicit provider '{active}' has a static credential")
        elif active in _CREDENTIAL_STORE_PROVIDERS:
            enabled = getattr(config.providers, active, None)
            if enabled is not None and not getattr(enabled, "enabled", True):
                ctx.error("provider", f"Explicit provider '{active}' is disabled")
            else:
                ctx.skipped(
                    "provider",
                    f"Explicit provider '{active}' uses an external credential store",
                    "Default Doctor does not open OS keychains. Use --online for credential probing.",
                )
        elif resolved is not None:
            ctx.warn(
                "provider",
                f"Explicit provider '{active}' is unavailable; runtime falls back to '{resolved[0]}'",
            )
        else:
            ctx.error(
                "provider",
                f"Explicit provider '{active}' has no usable config credential",
                "Runtime may fall back to another provider; verify the intended default.",
            )
    elif resolved is not None:
        ctx.ok("provider", f"Fallback cascade resolves from static credentials to '{resolved[0]}'")
    else:
        ctx.warn(
            "provider",
            "No provider credential was found in config",
            "A subscription credential may still exist in an OS keychain; use --online to inspect it.",
        )


def check_provider_corruption(ctx: DoctorContext) -> None:
    config = ctx.config
    if config is None:
        ctx.skipped("provider_corruption", "Config is not valid")
        return
    issues: list[str] = []
    for slot in _BYOK_SLOTS:
        provider = getattr(config.providers, slot, None)
        if provider is None:
            continue
        api_key = (getattr(provider, "api_key", "") or "").strip()
        api_base = (getattr(provider, "api_base", "") or "").strip()
        if api_key and _FLOWLY_BEARER_RE.match(api_key):
            issues.append(f"providers.{slot}.apiKey contains a Flowly server bearer")
        if "useflowlyapp.com" in api_base.lower():
            issues.append(f"providers.{slot}.apiBase points at the Flowly proxy")
    if issues:
        ctx.warn(
            "provider_corruption",
            f"Found {len(issues)} likely cross-provider credential leak(s)",
            "\n".join(f"• {item}" for item in issues),
        )
    else:
        ctx.ok("provider_corruption", "No high-confidence cross-provider leaks detected")


def check_model(ctx: DoctorContext) -> None:
    if ctx.config is None:
        ctx.skipped("model", "Config is not valid")
        return
    model = (ctx.config.agents.defaults.model or "").strip()
    if not model:
        ctx.error("model", "agents.defaults.model is empty")
    else:
        ctx.ok("model", model)


def check_gateway_security(ctx: DoctorContext) -> None:
    if ctx.config is None:
        ctx.skipped("gateway_security", "Config is not valid")
        return
    gateway = ctx.config.gateway
    public = gateway.host not in {"127.0.0.1", "localhost", "::1"}
    if public and not gateway.token:
        ctx.error(
            "gateway_security",
            f"Gateway is bound to {gateway.host} without an authentication token",
            "Restrict the host to loopback or configure gateway.token.",
        )
    elif public:
        ctx.warn(
            "gateway_security",
            f"Gateway is network-visible on {gateway.host}",
            "Authentication is configured; verify firewall exposure separately.",
        )
    else:
        ctx.ok("gateway_security", f"Loopback-only binding: {gateway.host}:{gateway.port}")


def check_channels(ctx: DoctorContext) -> None:
    if ctx.config is None:
        ctx.skipped("channels", "Config is not valid")
        return
    enabled = [
        name
        for name, channel in ctx.config.channels.model_dump().items()
        if isinstance(channel, dict) and channel.get("enabled")
    ]
    if not enabled:
        ctx.warn("channels", "No message channel is enabled")
        return
    problems: list[str] = []
    channels = ctx.config.channels
    if channels.telegram.enabled and not channels.telegram.token.strip():
        problems.append("telegram: bot token is missing")
    if channels.discord.enabled and not channels.discord.token.strip():
        problems.append("discord: bot token is missing")
    if channels.slack.enabled:
        if not channels.slack.bot_token.strip():
            problems.append("slack: bot token is missing")
        if not channels.slack.app_token.strip():
            problems.append("slack: app token is missing")
    if channels.web.enabled:
        if not channels.web.server_id.strip():
            problems.append("web relay: serverId is missing")
        if not channels.web.auth_token.strip():
            problems.append("web relay: authToken is missing")
        if not channels.web.relay_url.strip():
            problems.append("web relay: relayUrl is missing")
    if channels.whatsapp.enabled and not channels.whatsapp.bridge_url.strip():
        problems.append("whatsapp: bridge URL is missing")
    if channels.teams.enabled and not channels.teams.webhook_url.strip():
        problems.append("teams: webhook URL is missing")
    if channels.email.enabled:
        gmail = ctx.data_dir / "credentials" / "gmail.json"
        if gmail.is_symlink() or not gmail.is_file():
            problems.append("email: Gmail OAuth credential file is missing")
    if problems:
        ctx.warn(
            "channels",
            f"{len(enabled)} channel(s) enabled, but {len(problems)} config gap(s) found",
            "\n".join(f"• {problem}" for problem in problems),
        )
    else:
        ctx.ok("channels", f"Enabled channel config is structurally complete: {', '.join(enabled)}")


def _workspace(ctx: DoctorContext) -> Path | None:
    if ctx.config is None:
        return None
    return Path(ctx.config.agents.defaults.workspace).expanduser()


def check_workspace(ctx: DoctorContext) -> None:
    workspace = _workspace(ctx)
    if workspace is None:
        ctx.skipped("workspace", "Config is not valid")
        return
    if workspace.is_symlink():
        ctx.warn(
            "workspace",
            f"Workspace is a symbolic link: {workspace}",
            "Automatic workspace repair is disabled for symbolic targets.",
        )
        return
    try:
        profile_local = workspace.resolve().is_relative_to(ctx.data_dir.resolve())
    except OSError:
        profile_local = False
    if not workspace.exists():
        ctx.error(
            "workspace",
            f"Workspace does not exist: {workspace}",
            "External workspaces require manual creation."
            if not profile_local
            else "A profile-local workspace can be seeded with --fix.",
            fixable=profile_local,
            risk=RepairRisk.LOW if profile_local else RepairRisk.MEDIUM,
            repair_command="flowly doctor --fix" if profile_local else "",
        )
        return
    if not workspace.is_dir() or not os.access(workspace, os.R_OK | os.W_OK | os.X_OK):
        ctx.error("workspace", f"Workspace is not readable and writable: {workspace}")
        return
    required = [workspace / name for name in _WORKSPACE_FILES]
    required += [workspace / name for name in ("memory", "skills", "personas")]
    required.append(workspace / "memory" / "MEMORY.md")
    missing = [path for path in required if not path.exists()]
    if missing:
        ctx.warn(
            "workspace",
            f"Workspace is missing {len(missing)} standard item(s)",
            "\n".join(f"• {path}" for path in missing),
            fixable=profile_local,
            risk=RepairRisk.LOW if profile_local else RepairRisk.MEDIUM,
            repair_command="flowly doctor --fix" if profile_local else "",
        )
        return
    ctx.ok("workspace", str(workspace))


def check_profile_isolation(ctx: DoctorContext) -> None:
    workspace = _workspace(ctx)
    if workspace is None:
        ctx.skipped("profile_isolation", "Config is not valid")
        return
    default_home = (Path.home() / ".flowly").resolve()
    try:
        active_home = ctx.data_dir.resolve()
    except OSError:
        active_home = ctx.data_dir
    try:
        resolved_workspace = workspace.resolve()
    except OSError:
        resolved_workspace = workspace
    if active_home != default_home and resolved_workspace == default_home / "workspace":
        ctx.warn(
            "profile_isolation",
            "Custom profile points at the default profile workspace",
            "This can mix memory, skills, personas, and instructions across profiles.",
        )
    else:
        ctx.ok("profile_isolation", "Profile workspace is not implicitly shared")


def check_memory(ctx: DoctorContext) -> None:
    workspace = _workspace(ctx)
    if workspace is None:
        ctx.skipped("memory", "Config is not valid")
        return
    memory_dir = workspace / "memory"
    memory_file = memory_dir / "MEMORY.md"
    if not memory_dir.is_dir():
        ctx.warn("memory", f"Memory directory is missing: {memory_dir}")
    elif not memory_file.is_file():
        ctx.warn("memory", f"Long-term memory index is missing: {memory_file}")
    elif memory_file.is_symlink():
        ctx.warn("memory", f"MEMORY.md is a symbolic link: {memory_file}")
    else:
        ctx.ok("memory", str(memory_file))


def check_account_snapshot(ctx: DoctorContext) -> None:
    fallback = ctx.data_dir / "credentials" / "account.json"
    if not fallback.exists():
        ctx.skipped(
            "account",
            "No file-backed account credential found; OS keychain was not opened",
            "Use --online to inspect credential-backed integrations.",
        )
        return
    if fallback.is_symlink():
        ctx.error("account", f"Account credential file is a symbolic link: {fallback}")
        return
    if os.name != "nt" and not _is_private(fallback):
        ctx.error(
            "account",
            f"File-backed account credential permissions are too broad: {oct(_mode(fallback) or 0)}",
        )
        return
    try:
        payload = json.loads(fallback.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        ctx.error("account", "File-backed account credential is unreadable or invalid JSON")
        return
    if not isinstance(payload, dict):
        ctx.error("account", "File-backed account credential has an invalid root type")
        return
    identity = payload.get("email") or payload.get("user_id") or payload.get("userId")
    expires_at = payload.get("expires_at") or payload.get("expiresAt")
    try:
        expired = expires_at is not None and float(expires_at) <= time.time()
    except (TypeError, ValueError):
        expired = False
    if identity and expired:
        ctx.warn("account", "File-backed account credential is expired")
    elif identity:
        ctx.ok("account", "File-backed account credential is structurally readable")
    else:
        ctx.warn("account", "File-backed account credential has no account identity")


def check_relay(ctx: DoctorContext) -> None:
    if ctx.config is None:
        ctx.skipped("relay", "Config is not valid")
        return
    web = ctx.config.channels.web
    if not web.enabled:
        ctx.skipped("relay", "Web relay channel is disabled")
        return
    missing = [
        name
        for name, value in (
            ("serverId", web.server_id),
            ("authToken", web.auth_token),
            ("relayUrl", web.relay_url),
        )
        if not (value or "").strip()
    ]
    if missing:
        ctx.error("relay", f"Enabled relay is missing: {', '.join(missing)}")
    else:
        ctx.ok("relay", "Enabled relay has all dial-time config fields")


def _service_path() -> Path | None:
    system = platform.system().lower()
    if system == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / "ai.flowly.gateway.plist"
    if system == "linux":
        return Path.home() / ".config" / "systemd" / "user" / "ai.flowly.gateway.service"
    if system == "windows":
        return (
            Path.home()
            / "AppData"
            / "Local"
            / "flowly"
            / "ai.flowly.gateway.xml"
        )
    return None


def _windows_startup_path() -> Path:
    root = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    return (
        root
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "ai.flowly.gateway.cmd"
    )


def _xml_text(root: ET.Element, name: str) -> str:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == name:
            return (element.text or "").strip()
    return ""


def _windows_vbs_path(service_path: Path) -> Path:
    if service_path.suffix.lower() == ".xml":
        root = ET.fromstring(service_path.read_bytes())
        command = _xml_text(root, "Command").lower()
        arguments = _xml_text(root, "Arguments")
        if "wscript" not in command or not arguments:
            raise ValueError("Windows task does not invoke a VBS supervisor")
        value = arguments.strip().strip('"')
    else:
        content = service_path.read_text(encoding="utf-8")
        match = re.search(r"wscript(?:\.exe)?\s+\"([^\"]+\.vbs)\"", content, re.IGNORECASE)
        if match is None:
            raise ValueError("Startup launcher does not reference a VBS supervisor")
        value = match.group(1)
    path = Path(value)
    return path if path.is_absolute() else service_path.parent / path


def _service_command_and_home(path: Path, system: str) -> tuple[list[str], str]:
    """Read the gateway argv and FLOWLY_HOME from a production service file."""
    normalized = system.lower()
    if normalized == "darwin":
        payload = plistlib.loads(path.read_bytes())
        argv = [str(item) for item in payload.get("ProgramArguments", [])]
        home = str(payload.get("EnvironmentVariables", {}).get("FLOWLY_HOME", ""))
        return argv, home
    if normalized == "linux":
        content = path.read_text(encoding="utf-8")
        exec_line = next(
            (
                line.split("=", 1)[1].strip()
                for line in content.splitlines()
                if line.startswith("ExecStart=")
            ),
            "",
        )
        argv = shlex.split(exec_line) if exec_line else []
        home = ""
        for line in content.splitlines():
            if line.startswith("Environment=") and "FLOWLY_HOME=" in line:
                home = line.split("FLOWLY_HOME=", 1)[1].strip().strip('"')
                break
        return argv, home
    if normalized == "windows":
        vbs_path = _windows_vbs_path(path)
        if vbs_path.is_symlink() or not vbs_path.is_file():
            raise ValueError(f"Windows VBS supervisor is missing: {vbs_path}")
        content = vbs_path.read_text(encoding="utf-8")
        home_match = re.search(
            r'env\.Item\("FLOWLY_HOME"\)\s*=\s*"((?:""|[^"])*)"',
            content,
            re.IGNORECASE,
        )
        command_match = re.search(
            r'sh\.Run\s+"((?:""|[^"])*)"\s*,',
            content,
            re.IGNORECASE,
        )
        if home_match is None or command_match is None:
            raise ValueError("Windows VBS supervisor lacks FLOWLY_HOME or gateway command")
        home = home_match.group(1).replace('""', '"')
        command = command_match.group(1).replace('""', '"')
        argv = [item.strip('"') for item in shlex.split(command, posix=False)]
        return argv, home
    return [], ""


def check_service_definition(ctx: DoctorContext) -> None:
    system = platform.system().lower()
    path = _service_path()
    if path is None:
        ctx.skipped("service", f"Unsupported platform: {platform.system()}")
        return
    if system == "windows" and not path.exists():
        startup = _windows_startup_path()
        if startup.exists():
            path = startup
    if not path.exists():
        ctx.warn("service", f"Background service is not installed: {path}")
        return
    try:
        argv, service_home = _service_command_and_home(path, system)
    except (OSError, ValueError, ET.ParseError, plistlib.InvalidFileException) as exc:
        ctx.error("service", f"Service definition cannot be parsed ({type(exc).__name__})")
        return
    if not argv:
        ctx.error("service", "Service definition has no executable command")
        return
    executable = Path(argv[0]).expanduser()
    if not executable.exists():
        ctx.error("service", f"Service executable does not exist: {executable}")
        return
    if "gateway" not in argv:
        ctx.warn("service", "Service command does not visibly invoke the Flowly gateway")
        return
    if service_home and Path(service_home).expanduser().resolve() != ctx.data_dir.resolve():
        ctx.error(
            "service",
            "Service FLOWLY_HOME does not match the active profile",
            f"Service: {service_home}\nDoctor: {ctx.data_dir}",
        )
        return
    ctx.ok("service", f"Service definition is structurally valid: {path}")


def check_linux_linger(ctx: DoctorContext) -> None:
    if platform.system().lower() != "linux":
        return
    unit = _service_path()
    if unit is None or not unit.exists():
        ctx.skipped("linux_linger", "systemd user service is not installed")
        return
    user = os.environ.get("USER") or os.environ.get("LOGNAME", "")
    if not user:
        ctx.warn("linux_linger", "Could not determine the current user")
        return
    linger = Path("/var/lib/systemd/linger") / user
    if linger.exists():
        ctx.ok("linux_linger", f"systemd linger is enabled for {user}")
    else:
        ctx.warn("linux_linger", f"systemd linger is not enabled for {user}")


def _tool_sequence_error(records: list[tuple[int, dict[str, Any]]]) -> str:
    expected: set[str] = set()
    issuing_line = 0
    for line_no, record in records:
        if record.get("_type") == "metadata":
            continue
        role = record.get("role")
        if expected:
            if role != "tool":
                missing = ", ".join(sorted(expected))
                return f"line {issuing_line}: tool call result(s) missing before line {line_no}: {missing}"
            tool_id = record.get("tool_call_id")
            if not isinstance(tool_id, str) or tool_id not in expected:
                return f"line {line_no}: orphan or duplicate tool result"
            expected.remove(tool_id)
            continue
        if role == "tool":
            return f"line {line_no}: tool result has no preceding assistant tool call"
        calls = record.get("tool_calls") if role == "assistant" else None
        if calls:
            if not isinstance(calls, list):
                return f"line {line_no}: tool_calls is not a list"
            ids = [call.get("id") for call in calls if isinstance(call, dict)]
            if len(ids) != len(calls) or any(not isinstance(item, str) or not item for item in ids):
                return f"line {line_no}: tool call id is missing or invalid"
            if len(set(ids)) != len(ids):
                return f"line {line_no}: duplicate tool call id"
            expected = set(ids)
            issuing_line = line_no
    if expected:
        return f"line {issuing_line}: transcript ends before all tool results"
    return ""


def _scan_session(path: Path) -> tuple[int, list[str]]:
    errors: list[str] = []
    records: list[tuple[int, dict[str, Any]]] = []
    line_count = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line_count, line in enumerate(handle, start=1):
                if line_count > _MAX_SESSION_LINES:
                    errors.append(f"scan limit exceeded ({_MAX_SESSION_LINES} lines)")
                    break
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    errors.append(f"line {line_count}: invalid JSON")
                    continue
                if not isinstance(record, dict):
                    errors.append(f"line {line_count}: record root is not an object")
                    continue
                records.append((line_count, record))
    except (OSError, UnicodeError) as exc:
        return 0, [f"cannot read ({type(exc).__name__})"]
    semantic = _tool_sequence_error(records)
    if semantic:
        errors.append(semantic)
    return line_count, errors


def check_sessions(ctx: DoctorContext) -> None:
    sessions_dir = ctx.data_dir / "sessions"
    if not sessions_dir.exists():
        ctx.skipped("sessions", "No session directory exists yet")
        return
    if sessions_dir.is_symlink():
        ctx.error("sessions", f"Session directory is a symbolic link: {sessions_dir}")
        return
    if not sessions_dir.is_dir():
        ctx.error("sessions", f"Session path is not a directory: {sessions_dir}")
        return
    paths = sorted(
        path
        for path in sessions_dir.glob("*.jsonl")
        if not path.name.endswith(".full.jsonl")
    )
    if len(paths) > _MAX_SESSION_FILES:
        ctx.warn(
            "sessions",
            f"Only the first {_MAX_SESSION_FILES} of {len(paths)} sessions were inspected",
        )
        paths = paths[:_MAX_SESSION_FILES]
    problems: list[str] = []
    total_lines = 0
    for path in paths:
        if path.is_symlink():
            problems.append(f"{path.name}: symbolic link was not followed")
            continue
        lines, errors = _scan_session(path)
        total_lines += lines
        problems.extend(f"{path.name}: {error}" for error in errors)
    if problems:
        ctx.error(
            "sessions",
            f"Found {len(problems)} transcript integrity issue(s)",
            "\n".join(f"• {item}" for item in problems[:100]),
            fixable=True,
            risk=RepairRisk.HIGH,
            repair_command="flowly doctor --repair session_salvage",
        )
    else:
        ctx.ok("sessions", f"{len(paths)} canonical transcript(s), {total_lines} line(s)")


def _sqlite_quick_check(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return True, "not present"
    uri = f"file:{quote(str(path))}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return False, type(exc).__name__
    result = str(row[0]) if row else "no result"
    return result.lower() == "ok", result


def check_runtime_stores(ctx: DoctorContext) -> None:
    problems: list[str] = []
    notes: list[str] = []
    cron = ctx.data_dir / "cron" / "jobs.json"
    if cron.exists():
        if cron.is_symlink():
            problems.append("cron/jobs.json is a symbolic link")
        else:
            try:
                jobs = json.loads(cron.read_text(encoding="utf-8"))
                if not isinstance(jobs, (dict, list)):
                    problems.append("cron/jobs.json has an invalid root type")
                else:
                    notes.append("cron store parses")
            except (OSError, UnicodeError, json.JSONDecodeError):
                problems.append("cron/jobs.json is unreadable or invalid JSON")
    index = ctx.data_dir / "session_index.sqlite"
    index_ok, index_detail = _sqlite_quick_check(index)
    if not index_ok:
        problems.append(f"session_index.sqlite quick_check failed ({index_detail})")
    elif index.exists():
        notes.append("session index passes read-only quick_check")
    if problems:
        ctx.warn(
            "runtime_stores",
            f"Found {len(problems)} derived/runtime store issue(s)",
            "\n".join(f"• {item}" for item in problems),
        )
    elif notes:
        ctx.ok("runtime_stores", "; ".join(notes))
    else:
        ctx.skipped("runtime_stores", "No optional runtime stores exist yet")


def _flowly_file_credential(ctx: DoctorContext) -> str:
    path = ctx.data_dir / "credentials" / "account.json"
    if path.is_symlink() or not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    server_id = str(payload.get("server_id") or payload.get("serverId") or "").strip()
    token = str(
        payload.get("gateway_auth_token") or payload.get("gatewayAuthToken") or ""
    ).strip()
    return f"{server_id}:{token}" if server_id and token else ""


def _static_provider(ctx: DoctorContext) -> tuple[str, str, str] | None:
    config = ctx.config
    if config is None:
        return None

    def build(name: str) -> tuple[str, str, str] | None:
        if name == "flowly":
            flowly = config.providers.flowly
            if not flowly.enabled:
                return None
            key = (flowly.account_key or "").strip()
            if not key and flowly.server_id and flowly.auth_token:
                key = f"{flowly.server_id.strip()}:{flowly.auth_token.strip()}"
            web = config.channels.web
            if not key and web.enabled and web.server_id and web.auth_token:
                key = f"{web.server_id.strip()}:{web.auth_token.strip()}"
            if not key:
                key = _flowly_file_credential(ctx)
            return (name, key, _PROVIDER_BASES[name]) if key else None
        if name in _CREDENTIAL_STORE_PROVIDERS:
            return None
        provider = getattr(config.providers, name, None)
        if provider is None:
            return None
        key = (getattr(provider, "api_key", "") or "").strip()
        if not key:
            return None
        base = (getattr(provider, "api_base", "") or "").strip()
        base = base or _PROVIDER_BASES.get(name, "")
        return (name, key, base) if base else None

    active = (config.providers.active or "").strip()
    if active:
        explicit = build(active)
        if explicit is not None:
            return explicit
    for name in ("flowly", *_BYOK_SLOTS):
        candidate = build(name)
        if candidate is not None:
            return candidate
    return None


def check_online_gateway(ctx: DoctorContext) -> None:
    if ctx.config is None:
        ctx.skipped("gateway_online", "Config is not valid")
        return
    import httpx

    port = ctx.config.gateway.port
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=ctx.timeout)
    except Exception as exc:
        ctx.warn(
            "gateway_online",
            f"Gateway did not answer on loopback port {port} ({type(exc).__name__})",
        )
        return
    if response.status_code != 200:
        ctx.warn("gateway_online", f"Gateway health returned HTTP {response.status_code}")
        return
    try:
        payload = response.json()
    except Exception:
        ctx.error("gateway_online", "Gateway health response is not JSON")
        return
    if payload.get("status") != "ok":
        ctx.error("gateway_online", "Gateway health response is not healthy")
        return
    capabilities = payload.get("capabilities")
    count = len(capabilities) if isinstance(capabilities, list) else 0
    auth = "required" if payload.get("auth_required") else "not required"
    ctx.ok(
        "gateway_online",
        f"Gateway handshake passed on port {port}; auth {auth}; {count} capability flag(s)",
    )


def check_online_provider(ctx: DoctorContext) -> None:
    if ctx.config is None:
        ctx.skipped("provider_online", "Config is not valid")
        return
    resolved = _static_provider(ctx)
    if resolved is None:
        active = (ctx.config.providers.active or "").strip()
        if active in _CREDENTIAL_STORE_PROVIDERS or not active:
            ctx.skipped(
                "provider_online",
                "No config/file-backed provider credential can be probed safely",
                "OS keychains are not opened by Doctor; subscription credentials remain unverified.",
            )
        else:
            ctx.error("provider_online", f"Provider '{active}' has no usable static credential")
        return
    name, key, base = resolved
    if name == "anthropic":
        ctx.warn(
            "provider_online",
            "Anthropic credential is present but was not sent",
            "Anthropic has no free read-only auth endpoint; Doctor will not create a paid message.",
        )
        return

    import httpx

    try:
        if name == "gemini":
            response = httpx.get(
                f"{base.rstrip('/')}/models",
                params={"key": key},
                timeout=ctx.timeout,
                headers={"User-Agent": "flowly-doctor/1"},
            )
        else:
            response = httpx.get(
                f"{base.rstrip('/')}/models",
                timeout=ctx.timeout,
                headers={"Authorization": f"Bearer {key}", "User-Agent": "flowly-doctor/1"},
            )
    except Exception as exc:
        ctx.warn("provider_online", f"Provider network probe failed ({type(exc).__name__})")
        return
    if response.status_code in {401, 403}:
        ctx.error("provider_online", f"Provider '{name}' rejected the resolved credential")
    elif response.status_code == 200:
        ctx.ok("provider_online", f"Provider '{name}' passed read-only network authentication")
    else:
        ctx.warn("provider_online", f"Provider '{name}' returned HTTP {response.status_code}")


CHECKS = [
    DoctorCheck("state_dir", "installation", check_state_directory),
    DoctorCheck("config_file", "config", check_config_file),
    DoctorCheck("config_permissions", "config", check_config_permissions),
    DoctorCheck("config_validity", "config", check_config_validity),
    DoctorCheck("duplicate_keys", "config", check_duplicate_keys),
    DoctorCheck("unknown_keys", "config", check_unknown_config_keys),
    DoctorCheck("provider", "provider", check_provider),
    DoctorCheck("provider_corruption", "provider", check_provider_corruption),
    DoctorCheck("model", "provider", check_model),
    DoctorCheck("gateway_security", "security", check_gateway_security),
    DoctorCheck("channels", "channels", check_channels),
    DoctorCheck("workspace", "workspace", check_workspace),
    DoctorCheck("profile_isolation", "workspace", check_profile_isolation),
    DoctorCheck("memory", "workspace", check_memory),
    DoctorCheck("account", "credentials", check_account_snapshot),
    DoctorCheck("relay", "channels", check_relay),
    DoctorCheck("service", "service", check_service_definition),
    DoctorCheck("linux_linger", "service", check_linux_linger),
    DoctorCheck("sessions", "data", check_sessions),
    DoctorCheck("runtime_stores", "data", check_runtime_stores),
    DoctorCheck("gateway_online", "online", check_online_gateway, online_only=True),
    DoctorCheck("provider_online", "online", check_online_provider, online_only=True),
]
