"""Secure shell execution tool with approval system."""

import asyncio
import re
import sys
from typing import Any, Callable, Awaitable

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.exec import (
    ExecConfig,
    ExecRequest,
    ExecResult,
    ExecApprovalStore,
    ExecApprovalDecision,
    analyze_command,
    execute_command,
)
from flowly.exec.types import PendingApproval


def _interpret_exit_code(command: str, exit_code: int) -> str | None:
    """Map well-known benign non-zero exits to a "not an error" note.

    Guard against false-alarm investigations: a weak model
    that sees ``grep`` exit 1 treats it as a failure and either retries
    forever or reports a phantom problem to the user. Exit semantics are
    derived from the LAST command in a pipeline (that's whose status the
    shell reports) and the FIRST command otherwise.
    """
    try:
        # Pipeline → shell reports the last stage's status.
        tail = command.rsplit("|", 1)[-1].strip()
        tokens = tail.split()
        # Skip leading env assignments (FOO=bar cmd …).
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        if not tokens:
            return None
        base = tokens[0].rsplit("/", 1)[-1]
    except Exception:
        return None

    if exit_code == 1 and base in ("grep", "egrep", "fgrep", "rg", "pgrep", "ag"):
        return "no matches found — this is normal for search commands, not an error"
    if exit_code == 1 and base == "diff":
        return "files differ — expected diff output, not an error"
    if exit_code == 1 and base in ("test", "["):
        return "condition evaluated to false — not an error"
    if exit_code == 124 and "timeout" in command:
        return "the `timeout` wrapper killed the command after its time limit"
    return None


class SecureExecTool(Tool):
    """
    Secure shell command execution tool.

    Features:
    - Security modes: deny, allowlist, full
    - Ask modes: off, on-miss, always
    - Command analysis for dangerous patterns
    - Safe bins (jq, grep, etc.) always allowed
    - Allowlist with glob pattern matching
    - Approval system via callback (Telegram)
    """

    def __init__(
        self,
        config: ExecConfig,
        approval_callback: Callable[[PendingApproval], Awaitable[ExecApprovalDecision | None]] | None = None,
        working_dir: str | None = None,
        main_config: Any = None,
    ):
        self.config = config
        # ``working_dir`` is the workspace base; the *runtime* cwd is
        # resolved per call (explicit > session > FLOWLY_CWD >
        # agents.defaults.cwd > workspace). ``main_config`` lets the
        # resolver read agents.defaults.cwd; None is fine (falls through).
        self.working_dir = working_dir
        self.main_config = main_config
        self._store = ExecApprovalStore()
        self._store.load()

        if approval_callback:
            self._store.set_approval_callback(approval_callback)

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        if not self.config.enabled:
            return "Execute shell commands. CURRENTLY DISABLED."

        security = self._store.config.security
        ask = self._store.config.ask

        desc = "Execute a shell command and return its output.\n\n"

        # Platform hint is critical for small models (Haiku in particular):
        # without it they emit the wrong dialect and get silent failures.
        # On Windows we now ship a bundled bash (see flowly/exec/bash_finder.py)
        # so POSIX semantics match Mac/Linux — the description has to reflect
        # the shell actually in use, otherwise the model keeps emitting
        # PowerShell syntax (Get-ChildItem, $LASTEXITCODE, 2>$null) which
        # bash can't parse and which doesn't round-trip Windows paths cleanly.
        if sys.platform == "win32":
            from flowly.exec.bash_finder import find_bash
            if find_bash():
                desc += (
                    "PLATFORM: Windows (shell: GNU bash via bundled MinGit). "
                    "Semantics match macOS/Linux exactly — emit POSIX commands:\n"
                    "  • list dir:       `ls ~/Desktop`, `ls -la /tmp`\n"
                    "  • search text:    `grep -r 'pattern' .`\n"
                    "  • find files:     `find . -name '*.ext'`\n"
                    "  • pipes / subst:  `foo | grep bar`, `$(cmd)`, `<(proc)`\n"
                    "  • env vars:       `$HOME`, `$USER` (USERPROFILE maps to $HOME)\n"
                    "  • home dir:       `~` or `$HOME` — both resolve to "
                    "`C:\\Users\\<name>`\n"
                    "  • paths:          forward slashes preferred (`/c/Users/...` "
                    "or `~/Documents`). Backslashes also work inside quotes.\n"
                    "DO NOT use PowerShell syntax. `Get-ChildItem`, `$LASTEXITCODE`, "
                    "`2>$null`, `Select-String`, `Expand-Archive` all fail under bash. "
                    "The shell is bash — treat it exactly like Linux.\n"
                    "NOTE on Turkish locale: the Desktop folder is always at "
                    "`~/Desktop` regardless of the Windows display language. "
                    "`~/Masaüstü` does not exist — the localized name is only "
                    "shown in File Explorer, the real folder name is `Desktop`.\n\n"
                )
            else:
                # Fallback path: bash bundle missing, running under PowerShell.
                # Keep the old translation hint — legacy installs and dev
                # machines without Git for Windows land here.
                desc += (
                    "PLATFORM: Windows (shell: PowerShell — bundled bash not "
                    "available). POSIX aliases exist for ls/cat/pwd/echo/cp/mv/"
                    "rm/clear but Unix pipelines and flag shapes do NOT carry "
                    "over. Translate before calling:\n"
                    "  • search text in files → `Select-String 'pattern' path\\*` "
                    "(NOT `grep`)\n"
                    "  • recursive file search → `Get-ChildItem -Recurse -Filter '*.ext'` "
                    "(NOT `find`)\n"
                    "  • locate executable → `Get-Command foo` (NOT `which foo`)\n"
                    "  • env vars → `$env:NAME` and `Get-ChildItem env:`\n"
                    "  • paths: backslashes or forward slashes both accepted. "
                    "Home is `$HOME` or `~`.\n\n"
                )
        elif sys.platform == "darwin":
            desc += "PLATFORM: macOS. Standard BSD userland. Use normal POSIX commands.\n\n"
        else:
            desc += "PLATFORM: Linux. Standard GNU userland. Use normal POSIX commands.\n\n"

        desc += f"Security: {security}, Ask: {ask}\n"

        if security == "deny":
            desc += "WARNING: Command execution is currently denied."
        elif security == "allowlist":
            desc += "Only allowlisted commands and safe bins (grep, jq, etc.) are permitted.\n"
            desc += "Other commands require user approval."
        elif security == "full":
            desc += "Full access mode - all commands allowed."

        return desc

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Optional timeout in seconds (default: {self.config.timeout_seconds})"
                }
            },
            "required": ["command"]
        }

    def set_approval_callback(
        self,
        callback: Callable[[PendingApproval], Awaitable[ExecApprovalDecision | None]]
    ) -> None:
        """Set the approval callback (for Telegram integration)."""
        self._store.set_approval_callback(callback)

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        session_key: str | None = None,
        **kwargs: Any
    ) -> str:
        """Execute a command with full security checks."""

        # Resolve the runtime cwd: an explicit per-call ``working_dir``
        # wins, then any cwd pinned for this session (e.g. a desktop/TUI
        # project folder), then FLOWLY_CWD / agents.defaults.cwd, finally
        # the workspace. See flowly/runtime_cwd.py.
        from flowly.runtime_cwd import resolve_runtime_cwd

        resolved_cwd = resolve_runtime_cwd(
            session_key=session_key,
            explicit=working_dir,
            config=self.main_config,
            workspace=self.working_dir,
        )

        # Create request
        request = ExecRequest(
            command=command.strip(),
            cwd=str(resolved_cwd),
            timeout=timeout,
            session_key=session_key,
        )

        # Analyze command first (for logging)
        analysis = analyze_command(command)
        logger.info(f"Exec request: {command[:50]}... (safe_bin={analysis.is_safe_bin}, resolved={analysis.resolved_path})")

        # Execute with security checks
        result = await execute_command(request, self.config, self._store)

        # Format result
        if result.denied:
            return f"❌ Command denied: {result.error}"

        if result.timed_out:
            return f"⏰ Command timed out after {timeout or self.config.timeout_seconds} seconds"

        if result.error:
            return f"❌ Error: {result.error}"

        # Build output
        output_parts = []

        if result.stdout:
            output_parts.append(result.stdout)

        if result.stderr:
            output_parts.append(f"STDERR:\n{result.stderr}")

        if result.exit_code is not None and result.exit_code != 0:
            hint = _interpret_exit_code(command, result.exit_code)
            if hint:
                output_parts.append(f"\nExit code: {result.exit_code} ({hint})")
            else:
                output_parts.append(f"\nExit code: {result.exit_code}")

        return "\n".join(output_parts) if output_parts else "(no output)"

    @property
    def store(self) -> ExecApprovalStore:
        """Get the approval store for external management."""
        return self._store


# Keep backward compatibility alias
ExecTool = SecureExecTool
