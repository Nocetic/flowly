"""Command safety analysis and validation."""

import platform
import re
import shlex
import shutil
from pathlib import Path

from flowly.exec.types import CommandAnalysis
from flowly.protected_paths import find_protected_paths_in_command

# Safe binaries that only operate on stdin (no file args).
# is_safe_bin() rejects path-like arguments, so adding a tool here does NOT
# allow it to touch arbitrary files — only the tool name is whitelisted.
_COMMON_SAFE_BINS = frozenset([
    "jq", "grep", "cut", "sort", "uniq", "head", "tail", "tr", "wc",
    "cat", "echo", "date", "whoami", "pwd", "hostname", "uname",
])

# Windows-native read-only built-ins (safe: no file writes, no network).
# Unix names above stay available on Windows too (Git Bash / WSL).
_WINDOWS_EXTRA_SAFE_BINS = frozenset([
    "findstr",     # grep equivalent (stdin-capable)
    "where",       # which equivalent — PATH lookup, read-only
    "ver",         # Windows version string
    "type",        # cat equivalent (path args still rejected by is_safe_bin)
    "tasklist",    # process list — read-only
    "systeminfo",  # system info — read-only
])

DEFAULT_SAFE_BINS = (
    _COMMON_SAFE_BINS | _WINDOWS_EXTRA_SAFE_BINS
    if platform.system() == "Windows"
    else _COMMON_SAFE_BINS
)

# Dangerous shell metacharacters
SHELL_METACHARS = re.compile(r'[;&|`$<>]')
# Null byte is always a hard reject (shlex chokes; classic injection vector).
NULL_BYTE = re.compile(r'\x00')
# Newlines mean multi-line scripts / heredocs — risky but legitimate.
MULTILINE_CHARS = re.compile(r'[\r\n]')
# Back-compat alias (some callers still check this regex directly).
CONTROL_CHARS = re.compile(r'[\r\n\x00]')
QUOTE_CHARS = re.compile(r'["\']')

# Patterns for dangerous commands (Unix)
_UNIX_DANGEROUS_PATTERNS = [
    re.compile(r'\brm\s+(-[rf]+\s+)*/', re.IGNORECASE),  # rm -rf /
    re.compile(r'\brm\s+(-[rf]+\s+)*~', re.IGNORECASE),  # rm -rf ~
    re.compile(r'\brm\s+(-[rf]+\s+)*\$HOME', re.IGNORECASE),  # rm -rf $HOME
    re.compile(r'\bsudo\b', re.IGNORECASE),
    re.compile(r'\bchmod\s+777', re.IGNORECASE),
    re.compile(r'\bchown\b.*root', re.IGNORECASE),
    re.compile(r'\bmkfs\b', re.IGNORECASE),
    re.compile(r'\bdd\b.*of=/', re.IGNORECASE),
    # Redirecting to block devices wipes disks. /dev/null, /dev/stdin,
    # /dev/stdout, /dev/stderr, /dev/tty, /dev/zero, /dev/random, /dev/urandom
    # are safe and common — don't flag them.
    re.compile(r'>\s*/dev/(sd|nvme|hd|mapper|disk|rdisk|loop|ram)', re.IGNORECASE),
    re.compile(r'\bcurl\b.*\|\s*(ba)?sh', re.IGNORECASE),  # curl | sh
    re.compile(r'\bwget\b.*\|\s*(ba)?sh', re.IGNORECASE),  # wget | sh
    re.compile(r':(){.*};:', re.IGNORECASE),  # Fork bomb
    re.compile(r'\bkillall\b', re.IGNORECASE),  # killall
    re.compile(r'\bpkill\s+-9', re.IGNORECASE),  # pkill -9
    re.compile(r'\blaunchctl\s+unload', re.IGNORECASE),  # unload services
    re.compile(r'\bsystemctl\s+(stop|disable|mask)', re.IGNORECASE),  # stop services
    re.compile(r'\bnpm\s+publish\b', re.IGNORECASE),  # accidental publish
    re.compile(r'\bgit\s+push\s+.*--force', re.IGNORECASE),  # force push
]

# Patterns for dangerous commands (Windows)
_WINDOWS_DANGEROUS_PATTERNS = [
    re.compile(r'\bformat\b\s+[a-z]:', re.IGNORECASE),  # format C:
    re.compile(r'\bdiskpart\b', re.IGNORECASE),
    re.compile(r'\breg\b\s+delete', re.IGNORECASE),  # reg delete
    re.compile(r'\bcipher\b\s+/w', re.IGNORECASE),  # cipher /w (wipe)
    re.compile(r'\bdel\b\s+/[sfq]', re.IGNORECASE),  # del /s /f /q
    re.compile(r'\brd\b\s+/s', re.IGNORECASE),  # rd /s (recursive delete)
    re.compile(r'\brmdir\b\s+/s', re.IGNORECASE),  # rmdir /s
    re.compile(r'\bnet\b\s+user\b.*\b/delete\b', re.IGNORECASE),  # net user /delete
    re.compile(r'\bbcdedit\b', re.IGNORECASE),  # boot config
    re.compile(r'\brunas\b\s+/user:administrator', re.IGNORECASE),
]

DANGEROUS_PATTERNS = (
    _UNIX_DANGEROUS_PATTERNS + _WINDOWS_DANGEROUS_PATTERNS
    if platform.system() == "Windows"
    else _UNIX_DANGEROUS_PATTERNS
)

# Pipeline operators that are not allowed in allowlist mode
DISALLOWED_PIPELINE_OPS = {'||', '|&', '`', '$(', '\n', '\r', '(', ')'}


def is_safe_executable(value: str | None) -> bool:
    """Check if a string is safe to use as an executable name."""
    if not value:
        return False

    trimmed = value.strip()
    if not trimmed:
        return False

    # Check for dangerous characters
    if '\0' in trimmed:
        return False
    if CONTROL_CHARS.search(trimmed):
        return False
    if SHELL_METACHARS.search(trimmed):
        return False

    return True


def resolve_executable(name: str) -> str | None:
    """Resolve an executable name to its full path."""
    import os

    # If it's already a path (check both Unix and Windows separators)
    if '/' in name or '\\' in name or os.sep in name:
        path = Path(name).expanduser().resolve()
        if path.exists() and path.is_file():
            return str(path)
        return None

    # Use shutil.which to find in PATH
    return shutil.which(name)


def is_safe_bin(executable: str, args: list[str]) -> bool:
    """Check if command is a safe stdin-only binary with safe args."""
    import os

    # Get basename (handle both Unix and Windows paths)
    name = Path(executable).name if ('/' in executable or '\\' in executable or os.sep in executable) else executable

    if name not in DEFAULT_SAFE_BINS:
        return False

    # Check args don't reference files
    for arg in args:
        # Skip flags
        if arg.startswith('-'):
            continue
        # Check if arg looks like a path
        if '/' in arg or '\\' in arg or arg.startswith('~'):
            return False
        # Check if arg is an existing file
        if Path(arg).exists():
            return False

    return True


def has_dangerous_pattern(command: str) -> bool:
    """Check if command matches any dangerous patterns."""
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return True
    return False


def split_pipeline(command: str) -> tuple[bool, str | None, list[str]]:
    """
    Split a command into pipeline segments.

    Returns (ok, reason, segments). `ok=False` now means the pipeline is
    structurally unusable (empty segment); advanced operators like `||`,
    subshells and command substitution are flagged by ``analyze_pipeline_risk``
    instead of hard-rejected here, so the approval flow can decide.
    """
    # Split by pipe
    segments = [s.strip() for s in command.split('|') if s.strip() != '']

    if not segments:
        return False, "Empty pipeline", []

    return True, None, segments


def analyze_pipeline_risk(command: str) -> list[str]:
    """Return human-readable risk reasons for a pipeline-like command."""
    reasons: list[str] = []
    for op in DISALLOWED_PIPELINE_OPS:
        if op in command:
            reasons.append(f"Uses shell operator: {op!r}")
            break
    if '$(' in command or '`' in command:
        reasons.append("Uses command substitution")
    if re.search(r'[<>]', command):
        reasons.append("Uses shell redirection")
    return reasons


def parse_command(command: str) -> tuple[str | None, list[str]]:
    """Parse a command into executable and arguments."""
    try:
        parts = shlex.split(command)
        if not parts:
            return None, []
        return parts[0], parts[1:]
    except ValueError:
        return None, []


def analyze_command(command: str) -> CommandAnalysis:
    """
    Analyze a shell command for safety.

    Returns a CommandAnalysis with details about the command.

    Policy: the only hard rejects are things the executor truly cannot run
    (empty command, null bytes, unparseable syntax). Everything else — dangerous
    patterns, multi-line scripts, subshells, redirects — is recorded in
    ``risk_reasons`` and routed through the security/ask policy by the executor.
    This lets users operating in ``security=full`` actually get full access,
    while ``ask=always`` (or ``allowlist`` + ``on-miss``) still surfaces a prompt
    they can approve or deny.
    """
    command = command.strip()

    if not command:
        return CommandAnalysis(ok=False, reason="Empty command")

    # Null bytes are non-recoverable: shlex.split raises, /bin/sh truncates.
    if NULL_BYTE.search(command):
        return CommandAnalysis(
            ok=False,
            reason="Null byte in command",
            has_dangerous_chars=True,
            risk_reasons=["Null byte in command"],
        )

    # Hardcoded-protected paths — SSH keys, AWS creds, macOS Keychain,
    # /etc/shadow, etc. Hard reject regardless of security mode. The user
    # cannot whitelist these and no approval flow grants access. See
    # ``flowly.protected_paths`` for rationale.
    flagged = find_protected_paths_in_command(command)
    if flagged:
        # Show only the first flagged path in the reason — keeps the error
        # message compact while still telling the user what triggered it.
        return CommandAnalysis(
            ok=False,
            reason=(
                f"Command touches a protected path ({flagged[0]}). "
                "Sensitive locations like SSH keys, cloud credentials, and "
                "the system keychain are always blocked."
            ),
            has_dangerous_chars=True,
            risk_reasons=[f"Touches protected path: {p}" for p in flagged],
        )

    risk_reasons: list[str] = []

    # Multi-line script: heredocs, `python -c "...\n..."`, etc. Not fatal,
    # but worth surfacing so the user/policy can decide.
    if MULTILINE_CHARS.search(command):
        risk_reasons.append("Multi-line command (newlines / heredoc)")

    # Dangerous patterns: rm -rf /, curl | sh, disk-device redirects, etc.
    if has_dangerous_pattern(command):
        risk_reasons.append("Matches dangerous command pattern")

    # Subshell / substitution / redirect — surface even on single commands.
    pipeline_reasons = analyze_pipeline_risk(command)
    for reason in pipeline_reasons:
        if reason not in risk_reasons:
            risk_reasons.append(reason)

    # Check if it's a pipeline (plain `|`, not `||`).
    is_pipeline = '|' in command and '||' not in command

    if is_pipeline:
        ok, reason, segments = split_pipeline(command)
        if not ok:
            # Structural failure (empty segment) — still unusable.
            return CommandAnalysis(
                ok=False,
                reason=reason,
                is_pipeline=True,
                has_dangerous_chars=True,
                risk_reasons=risk_reasons + ([reason] if reason else []),
            )

        for seg in segments:
            if has_dangerous_pattern(seg):
                msg = f"Dangerous pattern in pipeline segment: {seg[:60]}"
                if msg not in risk_reasons:
                    risk_reasons.append(msg)

        executable, args = parse_command(segments[0])
        resolved = resolve_executable(executable) if executable else None

        return CommandAnalysis(
            ok=True,
            executable=executable,
            resolved_path=resolved,
            args=args,
            is_pipeline=True,
            segments=segments,
            has_dangerous_chars=bool(risk_reasons),
            risk_reasons=risk_reasons,
            is_safe_bin=is_safe_bin(executable, args) if executable else False,
        )

    # Single command
    try:
        executable, args = parse_command(command)
    except Exception:
        executable, args = None, []

    if not executable:
        # shlex couldn't parse — unusable.
        return CommandAnalysis(
            ok=False,
            reason="Could not parse command",
            has_dangerous_chars=bool(risk_reasons),
            risk_reasons=risk_reasons or ["Unparseable command"],
        )

    # Shell metachars in the executable name itself (not in args) is a real
    # red flag — the LLM shouldn't be constructing executable names that way.
    if SHELL_METACHARS.search(executable):
        risk_reasons.append("Shell metacharacters in executable name")

    resolved = resolve_executable(executable)

    return CommandAnalysis(
        ok=True,
        executable=executable,
        resolved_path=resolved,
        args=args,
        is_pipeline=False,
        has_dangerous_chars=bool(risk_reasons),
        risk_reasons=risk_reasons,
        is_safe_bin=is_safe_bin(executable, args),
    )
