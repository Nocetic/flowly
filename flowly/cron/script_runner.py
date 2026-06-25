"""Cron job script pre-execution.

Runs a user-supplied Python script before the agent turn so the agent
can reason over fresh data (e.g. "check Reddit for new posts → summarize").

Security boundary:
    * Scripts must live under the Flowly workspace (`~/.flowly/workspace/`
      by default). Absolute paths, `~` prefixes and path-traversal are
      blocked at both the API layer (`validate_script_path()`) and the
      execution layer (inside `run()`).
    * The interpreter is `sys.executable` — Python only. Shell/JS/other
      need a Python wrapper.
    * Working directory is the script's own directory, so script-local
      imports and config files work.

Why workspace and not a separate `scripts/` dir?
    Flowly's existing conventions already put user/agent-authored files
    in `~/.flowly/workspace/`. The agent's `write_file` tool writes there,
    `read_file` reads from there — so a script the agent composed is
    automatically addressable as a cron `script` value without a second
    file-layout concept.

Wake-gate:
    If the script's LAST line of stdout parses as JSON `{"wakeAgent": false}`,
    the caller should skip the agent turn entirely (used for silent data-
    collection / change-detection runs).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from flowly.profile import get_flowly_home


DEFAULT_SCRIPT_TIMEOUT_S = 120
MAX_OUTPUT_CHARS = 64_000  # Keep prompt-sized; prevents runaway scripts from exploding context.


def scripts_dir() -> Path:
    """Return the directory cron scripts must live under.

    Currently the Flowly workspace (`~/.flowly/workspace/`). The agent's
    `write_file` tool targets the same root, so any script the agent
    composes is addressable as a cron `script` path directly.
    """
    d = get_flowly_home() / "workspace"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Lightweight secret redaction for script output. Not a replacement for
# keeping secrets out of scripts — just a best-effort scrub so accidental
# prints of `OPENROUTER_API_KEY=sk-or-...` don't land in the prompt.
_REDACT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'(?i)(api[_-]?key|token|secret|password|bearer)\s*[:=]\s*([^\s"\']+)'),
    re.compile(r'sk-[A-Za-z0-9_-]{16,}'),          # OpenAI/OpenRouter-style keys
    re.compile(r'xox[abprs]-[A-Za-z0-9-]{10,}'),    # Slack tokens
    re.compile(r'ghp_[A-Za-z0-9]{30,}'),            # GitHub PATs
]


def _redact(text: str) -> str:
    if not text:
        return text
    for pat in _REDACT_PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


@dataclass
class ScriptResult:
    success: bool
    stdout: str
    stderr: str
    error: str | None        # Human-readable error when success=False
    wake_agent: bool         # False only when the script emitted {"wakeAgent": false}
    script_path: str         # Resolved absolute path (for logging)


def validate_script_path(script_path: str) -> str | None:
    """Validate a script path at the API boundary (tool / CLI).

    Returns an error string if the path should be rejected, or None if it's
    safe to store. Runtime validation re-runs inside `run()` to cover the
    case where the stored path is manipulated on disk.
    """
    if not script_path or not str(script_path).strip():
        return "Script path is empty."

    path_str = str(script_path).strip()

    # Reject absolute POSIX paths, home-relative paths, and Windows drive
    # letters. Only bare relative paths under scripts/ are allowed.
    if path_str.startswith(("/", "~")) or (len(path_str) >= 2 and path_str[1] == ":"):
        return (
            f"Script path must be relative to the Flowly workspace "
            f"({scripts_dir()}). Got: {path_str}"
        )

    # Resolve and double-check the result lands inside the workspace.
    try:
        candidate = (scripts_dir() / path_str).resolve()
        candidate.relative_to(scripts_dir().resolve())
    except (ValueError, OSError) as e:
        return f"Script path escapes the workspace: {path_str} ({e})"

    return None


def _parse_wake_gate(stdout: str) -> bool:
    """Return True unless the script's last JSON line is `{"wakeAgent": false}`.

    Scripts opt into no-op runs by emitting a single JSON object as the last
    non-empty stdout line. Non-JSON stdout → always wake the agent.
    """
    if not stdout:
        return True
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("wakeAgent") is False:
                    return False
            except json.JSONDecodeError:
                pass
        break  # Only inspect the actual last non-empty line.
    return True


def run(script_path: str, *, timeout_s: int = DEFAULT_SCRIPT_TIMEOUT_S) -> ScriptResult:
    """Execute a cron script under the Flowly script sandbox.

    Returns a ScriptResult. Never raises — errors are captured in the
    `error` field so the caller can inject them into the prompt.
    """
    err = validate_script_path(script_path)
    if err:
        return ScriptResult(
            success=False,
            stdout="",
            stderr="",
            error=err,
            wake_agent=True,
            script_path=str(script_path),
        )

    resolved = (scripts_dir() / script_path).resolve()
    if not resolved.is_file():
        return ScriptResult(
            success=False,
            stdout="",
            stderr="",
            error=f"Script not found: {script_path}",
            wake_agent=True,
            script_path=str(resolved),
        )

    try:
        proc = subprocess.run(
            [sys.executable, str(resolved)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(resolved.parent),
        )
    except subprocess.TimeoutExpired:
        return ScriptResult(
            success=False,
            stdout="",
            stderr="",
            error=f"Script timed out after {timeout_s}s: {script_path}",
            wake_agent=True,
            script_path=str(resolved),
        )
    except Exception as e:
        return ScriptResult(
            success=False,
            stdout="",
            stderr="",
            error=f"Failed to launch script ({type(e).__name__}: {e}): {script_path}",
            wake_agent=True,
            script_path=str(resolved),
        )

    stdout = _redact(proc.stdout or "")
    stderr = _redact(proc.stderr or "")

    # Truncate oversized output so one runaway script can't blow the prompt.
    if len(stdout) > MAX_OUTPUT_CHARS:
        stdout = stdout[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(stdout)} total chars)"
    if len(stderr) > MAX_OUTPUT_CHARS:
        stderr = stderr[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(stderr)} total chars)"

    if proc.returncode != 0:
        message = stderr.strip() or stdout.strip() or f"exit code {proc.returncode}"
        return ScriptResult(
            success=False,
            stdout=stdout,
            stderr=stderr,
            error=f"Script exited {proc.returncode}: {message}",
            wake_agent=True,
            script_path=str(resolved),
        )

    return ScriptResult(
        success=True,
        stdout=stdout,
        stderr=stderr,
        error=None,
        wake_agent=_parse_wake_gate(stdout),
        script_path=str(resolved),
    )


def format_for_prompt(result: ScriptResult) -> str:
    """Render a ScriptResult as the preamble to inject into the cron prompt.

    Uses "## Script Output" / "## Script Error" markdown sections so the
    agent has a consistent, easy-to-recognize marker for data the scheduler
    collected on its behalf.
    """
    if not result.success:
        return (
            "## Script Error\n"
            "The pre-run data-collection script failed. Report this to the user.\n\n"
            "```\n"
            f"{result.error}\n"
            "```\n"
        )
    if not result.stdout.strip():
        return "[Pre-run script completed but produced no output.]\n"
    return (
        "## Script Output\n"
        "The following data was collected by a pre-run script. "
        "Use it as context for your analysis.\n\n"
        "```\n"
        f"{result.stdout}\n"
        "```\n"
    )
