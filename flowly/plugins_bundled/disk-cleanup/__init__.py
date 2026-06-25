"""disk-cleanup plugin — auto-cleanup of ephemeral Flowly session files.

Wires three behaviours via the v1 plugin API:

1. ``post_tool_call`` hook — inspects ``write_file`` / ``exec`` /
   ``edit_file`` results for newly-created paths matching test/temp
   patterns under FLOWLY_HOME or /tmp/flowly-* and tracks them
   silently.  Zero agent compliance required.

2. ``on_session_end`` hook — when test files were tracked during the
   just-finished turn, runs :func:`disk_cleanup.quick` and logs a
   single line to ``$FLOWLY_HOME/disk-cleanup/cleanup.log``.

3. ``/disk-cleanup`` slash command — manual ``status``, ``dry-run``,
   ``quick``, ``track``, ``forget``.
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Set

from . import disk_cleanup as dg

logger = logging.getLogger(__name__)


# Per-session set of "test files newly tracked this turn".  Keyed by
# session_id so on_session_end can decide whether to run cleanup.
_recent_test_tracks: Dict[str, Set[str]] = {}
_lock = threading.Lock()


_TERMINAL_PATH_REGEX = re.compile(r"(?:^|\s)(/[^\s'\"`]+|\~/[^\s'\"`]+)")


# ── Helpers ─────────────────────────────────────────────────────


def _record_track(session_id: str, path: Path, category: str) -> None:
    """Record that we tracked *path* as *category* during this turn."""
    if category != "test":
        return
    key = session_id or "default"
    with _lock:
        _recent_test_tracks.setdefault(key, set()).add(str(path))


def _drain(session_id: str) -> Set[str]:
    """Pop the set of test paths tracked during this turn."""
    key = session_id or "default"
    with _lock:
        return _recent_test_tracks.pop(key, set())


def _attempt_track(path_str: str, session_id: str) -> None:
    """Best-effort auto-track. Never raises.

    ``p.exists()`` and the downstream ``guess_category``/``track`` calls
    can hit ``PermissionError`` when the agent runs under the OS sandbox
    (SECURITY.md §2.2) and the LLM emits a tool call touching a
    sandbox-denied path like ``~/.ssh``. The pre-execute safety check
    rejects the command itself, but the ``post_tool_call`` hook still
    fires with the path in the params — and ``stat()`` on a denied path
    raises rather than returning False. We catch OSError broadly so a
    sandbox or filesystem hiccup never bubbles up through the hook
    runner.
    """
    try:
        p = Path(path_str).expanduser()
        if not p.exists():
            return
        category = dg.guess_category(p)
        if category is None:
            return
        newly = dg.track(str(p), category, silent=True)
        if newly:
            _record_track(session_id, p, category)
    except OSError:
        # Sandbox-denied path, broken symlink, permission gap, etc.
        # Silently drop — disk-cleanup is best-effort observation.
        return
    except Exception:
        # Defensive: any other unexpected error from the guess/track
        # pipeline. Hook contract is "never break the agent loop".
        return


def _extract_paths_from_write_file(params: Dict[str, Any]) -> Set[str]:
    """write_file uses 'path' parameter."""
    path = params.get("path") or params.get("file_path")
    return {path} if isinstance(path, str) and path else set()


def _extract_paths_from_exec(
    params: Dict[str, Any], result: str,
) -> Set[str]:
    """Pull candidate filesystem paths from a shell command and its output.
    ``guess_category`` / ``is_safe_path`` filter the results.
    """
    paths: Set[str] = set()
    cmd = params.get("command") or params.get("cmd") or ""
    if isinstance(cmd, str) and cmd:
        try:
            for tok in shlex.split(cmd, posix=True):
                if tok.startswith(("/", "~")):
                    paths.add(tok)
        except ValueError:
            pass
    # Only scan the result text if it's reasonably small.
    if isinstance(result, str) and len(result) < 4096:
        for match in _TERMINAL_PATH_REGEX.findall(result):
            paths.add(match)
    return paths


# ── Hooks ──────────────────────────────────────────────────────


def _on_post_tool_call(ctx: Any) -> None:
    """Auto-track ephemeral files created by recent tool calls.

    *ctx* is a :class:`flowly.agent.hooks.ToolHookContext`.  We treat it
    duck-typed so this plugin still works if the dataclass evolves.
    """
    tool_name = getattr(ctx, "tool_name", "") or ""
    params = getattr(ctx, "params", {}) or {}
    result = getattr(ctx, "result", "") or ""
    session_id = getattr(ctx, "session_id", "") or ""

    if not isinstance(params, dict):
        return

    candidates: Set[str] = set()
    if tool_name in ("write_file", "edit_file"):
        candidates = _extract_paths_from_write_file(params)
    elif tool_name == "exec":
        candidates = _extract_paths_from_exec(params, result if isinstance(result, str) else "")
    else:
        return

    for path_str in candidates:
        _attempt_track(path_str, session_id)


def _on_session_end(ctx: Any) -> None:
    """Run quick cleanup if any test files were tracked during this turn."""
    session_id = getattr(ctx, "session_id", "") or ""
    drained = _drain(session_id)

    # Sweep stale buckets from other sessions too.
    with _lock:
        all_keys = list(_recent_test_tracks.keys())
    for k in all_keys:
        if k != session_id:
            _recent_test_tracks.pop(k, None)

    if not drained:
        return

    try:
        summary = dg.quick()
    except Exception as exc:
        logger.debug("disk-cleanup quick cleanup failed: %s", exc)
        return

    if summary["deleted"] or summary["empty_dirs"]:
        dg._log(
            f"AUTO_QUICK (session_end): deleted={summary['deleted']} "
            f"dirs={summary['empty_dirs']} freed={dg.fmt_size(summary['freed'])}"
        )


# ── Slash command ──────────────────────────────────────────────


_HELP_TEXT = """\
/disk-cleanup — ephemeral file cleanup

Subcommands:
  status                     Per-category breakdown + top-10 largest
  dry-run                    Preview what quick would delete
  quick                      Run safe cleanup now (no prompts)
  track <path> <category>    Manually add a path to tracking
  forget <path>              Stop tracking a path (does not delete)

Categories: temp | test | research | download | chrome-profile | cron-output | other

All operations are scoped to FLOWLY_HOME and /tmp/flowly-*.
Test files are auto-tracked on write_file / exec and auto-cleaned at session end.
"""


def _fmt_summary(summary: Dict[str, Any]) -> str:
    base = (
        f"[disk-cleanup] Cleaned {summary['deleted']} files + "
        f"{summary['empty_dirs']} empty dirs, freed {dg.fmt_size(summary['freed'])}."
    )
    if summary.get("errors"):
        base += f"\n  {len(summary['errors'])} error(s); see cleanup.log."
    return base


def _handle_slash(raw_args: str) -> Optional[str]:
    argv = raw_args.strip().split()
    if not argv or argv[0] in ("help", "-h", "--help"):
        return _HELP_TEXT

    sub = argv[0]

    if sub == "status":
        return dg.format_status(dg.status())

    if sub == "dry-run":
        auto, prompt = dg.dry_run()
        auto_size = sum(i["size"] for i in auto)
        prompt_size = sum(i["size"] for i in prompt)
        lines = [
            "Dry-run preview (nothing deleted):",
            f"  Auto-delete : {len(auto)} files ({dg.fmt_size(auto_size)})",
        ]
        for item in auto:
            lines.append(f"    [{item['category']}] {item['path']}")
        lines.append(
            f"  Needs prompt: {len(prompt)} files ({dg.fmt_size(prompt_size)})"
        )
        for item in prompt:
            lines.append(f"    [{item['category']}] {item['path']}")
        lines.append(f"\n  Total potential: {dg.fmt_size(auto_size + prompt_size)}")
        return "\n".join(lines)

    if sub == "quick":
        return _fmt_summary(dg.quick())

    if sub == "track":
        if len(argv) < 3:
            return "Usage: /disk-cleanup track <path> <category>"
        path_arg = argv[1]
        category = argv[2]
        if category not in dg.ALLOWED_CATEGORIES:
            return (
                f"Unknown category {category!r}. "
                f"Allowed: {sorted(dg.ALLOWED_CATEGORIES)}"
            )
        if dg.track(path_arg, category, silent=True):
            return f"Tracked {path_arg} as {category!r}."
        return (
            f"Not tracked (already present, missing, or outside FLOWLY_HOME): "
            f"{path_arg}"
        )

    if sub == "forget":
        if len(argv) < 2:
            return "Usage: /disk-cleanup forget <path>"
        n = dg.forget(argv[1])
        if n:
            plural = "y" if n == 1 else "ies"
            return f"Removed {n} tracking entr{plural} for {argv[1]}."
        return f"Not found in tracking: {argv[1]}"

    return f"Unknown subcommand: {sub}\n\n{_HELP_TEXT}"


# ── Plugin registration ───────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point — wire hooks and slash command."""
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command(
        "disk-cleanup",
        handler=_handle_slash,
        description="Track and clean up ephemeral Flowly session files.",
    )
