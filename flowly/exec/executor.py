"""Command executor with security checks."""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from loguru import logger

from flowly.exec.types import (
    ExecRequest,
    ExecResult,
    ExecConfig,
)
from flowly.exec.bash_finder import find_bash
from flowly.exec.safety import analyze_command
from flowly.exec.approvals import (
    ExecApprovalStore,
    check_allowlist,
    requires_approval,
)
from flowly.exec.env_scrub import sanitize_subprocess_env


_WIN_POWERSHELL_CACHE: str | None = None


def _resolve_windows_shell() -> str:
    """Pick the best available shell on Windows.

    Prefers PowerShell 7+ (pwsh) when present because its default output
    encoding is UTF-8 (so Turkish/Unicode text comes out clean). Falls back
    to legacy Windows PowerShell (powershell.exe), which ships with every
    Windows install. Last resort is cmd.exe — but by that point something
    is unusually broken.

    Cached on first call because shutil.which touches the disk.
    """
    global _WIN_POWERSHELL_CACHE
    if _WIN_POWERSHELL_CACHE is not None:
        return _WIN_POWERSHELL_CACHE
    for candidate in ("pwsh", "powershell"):
        if shutil.which(candidate):
            _WIN_POWERSHELL_CACHE = candidate
            return candidate
    _WIN_POWERSHELL_CACHE = "cmd"
    return "cmd"


async def _spawn_shell_subprocess(
    command: str,
    *,
    cwd: str,
    env: dict | None,
) -> asyncio.subprocess.Process:
    """Start a subprocess running `command` through the platform's best shell.

    macOS / Linux — unchanged: hands off to `/bin/sh -c <command>` via
    `create_subprocess_shell`. Every POSIX command the agent emits has
    been tuned against this behaviour; snapshot-free execution keeps
    stdout byte-for-byte identical to pre-refactor output.

    Windows — tiered strategy for Mac-parity semantics:

      1. bash — preferred. Bundled MinGit via `FLOWLY_BASH_PATH`,
         system Git for Windows, or PATH — `find_bash()` does the lookup.
         Under bash, `ls ~/Desktop`, pipes, `$HOME`, and globbing behave
         identically to Mac. This is the preferred path and covers the
         empty-Desktop regression where PowerShell's alias semantics
         returned no rows.

      2. PowerShell fallback (pwsh → powershell.exe). Retained only for
         installs without bash (legacy builds, broken bundles). Kept so
         we degrade gracefully instead of hard-failing on users whose
         environment we cannot predict. Release builds bundle MinGit, so
         this branch should effectively be dead in production.
    """
    if sys.platform == "win32":
        # Preferred path — bash gives Mac parity.
        bash = find_bash()
        if bash:
            return await asyncio.create_subprocess_exec(
                bash,
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

        # Fallback — PowerShell. Known-imperfect, retained for resilience.
        shell = _resolve_windows_shell()
        if shell == "cmd":
            # Paranoid fallback — should never happen on a real Windows box.
            return await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        # Prefix forces UTF-8 on legacy PowerShell 5.x — noop on pwsh (already
        # UTF-8 by default) but doesn't hurt. Ensures Turkish / non-ASCII
        # characters round-trip through the pipe.
        wrapped = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            + command
        )
        return await asyncio.create_subprocess_exec(
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    # macOS / Linux — unchanged behaviour (Mac parity baseline).
    return await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )


def _wrap_for_cwd_capture(command: str) -> tuple[str, str]:
    """Wrap a POSIX command so the shell's final directory is captured.

    The command runs via ``eval`` so any trailing operator in *command*
    can't bleed into the capture lines; ``pwd -P`` writes the resolved
    directory to a temp file; the command's real exit code is preserved
    (``exit $__flowly_ec``). stdout/stderr are untouched — the capture
    goes to a file, not the pipe, so no marker ever leaks into output.

    Returns ``(wrapped_command, cwd_file_path)``. The caller owns the temp
    file and must unlink it.
    """
    import os
    import tempfile

    fd, path = tempfile.mkstemp(prefix="flowly-cwd-", suffix=".txt")
    os.close(fd)
    escaped = command.replace("'", "'\\''")
    wrapped = (
        f"eval '{escaped}'\n"
        f"__flowly_ec=$?\n"
        f"pwd -P > '{path}' 2>/dev/null || true\n"
        f"exit $__flowly_ec\n"
    )
    return wrapped, path


def _persist_tracked_cwd(cwd_file: str, session_key: str) -> None:
    """Pin the captured directory for *session_key* (live ``cd`` tracking).

    The next exec in this session resolves to it via ``resolve_runtime_cwd``.
    A missing/empty/non-directory value is ignored — e.g. the command deleted
    its own cwd — and the resolver re-validates and falls back next call.
    """
    import os

    try:
        with open(cwd_file, encoding="utf-8") as f:
            captured = f.read().strip()
    except OSError:
        return
    if not captured or not os.path.isdir(captured):
        return
    try:
        from flowly.runtime_cwd import set_session_cwd

        set_session_cwd(session_key, captured)
    except ValueError:
        pass


async def execute_command(
    request: ExecRequest,
    config: ExecConfig,
    store: ExecApprovalStore,
) -> ExecResult:
    """
    Execute a command with full security checks.

    Security flow:
    1. Check if exec is enabled
    2. Analyze command for safety
    3. Check security mode (deny/allowlist/full)
    4. Check allowlist if in allowlist mode
    5. Request approval if needed
    6. Execute command with timeout
    """
    # Check if enabled
    if not config.enabled:
        return ExecResult(
            success=False,
            denied=True,
            error="Command execution is disabled"
        )

    # Analyze command
    analysis = analyze_command(request.command)

    # Only hard-reject when the command is structurally unusable (empty,
    # null-byte, unparseable). Dangerous patterns, multi-line scripts,
    # subshells, redirects and similar are now recorded on
    # ``analysis.risk_reasons`` and routed through the policy below, so
    # the user's chosen security/ask combination decides what happens.
    if not analysis.ok:
        return ExecResult(
            success=False,
            denied=True,
            error=f"Command rejected: {analysis.reason}"
        )

    # Pick up any policy edit made since we last looked (TUI policy editor,
    # `flowly approvals set`, hand edit) so a long-lived gateway doesn't keep
    # enforcing a stale security/ask until restart.
    store.refresh_if_changed()

    # Get store config
    store_config = store.config

    # Security mode check
    if store_config.security == "deny":
        return ExecResult(
            success=False,
            denied=True,
            error="Command execution denied by security policy"
        )

    # Check if approval is required (works in any security mode)
    allowlist_ok = False
    if store_config.security == "allowlist":
        allowlist_ok = check_allowlist(store, analysis.resolved_path, analysis.executable)

    # Risky commands force an approval prompt unless the security policy is
    # explicitly "full" with "ask=off" (the user signed off on full trust).
    # Surface the risk, let the user decide.
    force_approval = (
        analysis.has_dangerous_chars
        and not (store_config.security == "full" and store_config.ask == "off")
    )

    if force_approval or requires_approval(store_config, analysis.ok, allowlist_ok):
        # Create pending approval and request a decision via callback
        # (Telegram/Desktop/TUI/iOS). The approval manager owns the cron
        # policy centrally: for a scheduled run (no user present) it applies
        # ``tools.exec.cron_mode`` and resolves synchronously instead of
        # hanging on an unanswerable prompt — deny by default, "allow-once"
        # when cron_mode=approve. Keeping that gate in one place (the manager)
        # means exec and every other tool share the same cron behaviour.
        pending = store.create_pending(request, config.approval_timeout_seconds)

        decision = await store.request_approval(pending)

        if decision is None:
            # Timeout or no callback
            return ExecResult(
                success=False,
                denied=True,
                error="Approval timed out or not available"
            )

        if decision == "deny":
            store.resolve_pending(pending.id, decision)
            return ExecResult(
                success=False,
                denied=True,
                error="Command denied by user"
            )

        # Allow-once or allow-always
        store.resolve_pending(pending.id, decision)

    elif store_config.security == "allowlist" and not allowlist_ok and not analysis.is_safe_bin:
        # Not in allowlist and not a safe bin
        return ExecResult(
            success=False,
            denied=True,
            error=f"Command not in allowlist: {analysis.executable}"
        )

    # Execute the command
    timeout = request.timeout or config.timeout_seconds
    cwd = request.cwd or str(Path.home())

    # Live cwd tracking: on POSIX, when the call is session-scoped, wrap the
    # command so we capture the directory it ends in. A `cd` then persists to
    # the next exec in the same session (the resolver reads the pinned session
    # cwd). Windows stays unwrapped for v1. Non-session calls (cron, one-offs)
    # are never wrapped, so their behaviour is byte-for-byte unchanged.
    track_cwd = bool(request.session_key) and sys.platform != "win32"
    cwd_file: str | None = None
    command_to_run = request.command
    if track_cwd:
        command_to_run, cwd_file = _wrap_for_cwd_capture(request.command)

    try:
        # Build environment. We always pass an explicit dict (never
        # env=None / full inheritance) so the Flowly-managed credential
        # blocklist gets applied — without scrubbing, an LLM-emitted
        # command could exfiltrate provider API keys / channel tokens
        # via ``env | curl ...`` or ``$OPENAI_API_KEY`` interpolation.
        # See flowly/exec/env_scrub.py for the rationale and the list
        # of names stripped (user-owned creds like AWS_* and GH_TOKEN
        # pass through; only Flowly-managed names are blocked).
        import os
        env = sanitize_subprocess_env(os.environ, request.env)

        # Run command via the platform's best shell.
        # macOS/Linux: POSIX shell (unchanged).
        # Windows: PowerShell so agent's POSIX-style commands work via aliases.
        process = await _spawn_shell_subprocess(
            command_to_run,
            cwd=cwd,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return ExecResult(
                success=False,
                exit_code=-1,
                error=f"Command timed out after {timeout} seconds",
                timed_out=True
            )

        # Decode output
        stdout_str = stdout.decode('utf-8', errors='replace')
        stderr_str = stderr.decode('utf-8', errors='replace')

        # Truncate if too long
        max_output = config.max_output_chars
        if len(stdout_str) > max_output:
            stdout_str = stdout_str[:max_output] + f"\n... (truncated, {len(stdout_str)} total chars)"
        if len(stderr_str) > max_output:
            stderr_str = stderr_str[:max_output] + f"\n... (truncated, {len(stderr_str)} total chars)"

        # Pin the directory the command ended in for this session, so a `cd`
        # carries over to the next exec. A stale/deleted path is ignored — the
        # resolver re-validates and falls back to the workspace next call.
        if track_cwd and cwd_file is not None and request.session_key:
            _persist_tracked_cwd(cwd_file, request.session_key)

        return ExecResult(
            success=process.returncode == 0,
            exit_code=process.returncode,
            stdout=stdout_str,
            stderr=stderr_str,
        )

    except Exception as e:
        # Log class name alongside str(e) — some exceptions (notably
        # asyncio's NotImplementedError from SelectorEventLoop on Windows)
        # stringify to empty, which made the original "{e}"-only log read
        # "Command execution error:" with nothing useful after it. The
        # class name is always present, so the telemetry stays actionable.
        logger.error(f"Command execution error: {type(e).__name__}: {e!r}")
        error_msg = str(e) or type(e).__name__
        return ExecResult(
            success=False,
            error=error_msg
        )
    finally:
        if cwd_file is not None:
            import os
            try:
                os.unlink(cwd_file)
            except OSError:
                pass
