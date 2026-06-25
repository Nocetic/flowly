"""Shared stderr log file for MCP stdio subprocesses.

The MCP Python SDK's ``stdio_client(server, errlog=...)`` parameter
defaults to ``sys.stderr``, which means anything the subprocess writes
to its stderr stream (FastMCP startup banners, JSON debug logs from
non-spec-compliant servers, npm warnings, etc.) lands directly on the
parent terminal. Inside the Textual TUI, that corrupts the screen and
can wedge the input loop.

We redirect every stdio MCP server's stderr to a single shared file at
``$FLOWLY_HOME/logs/mcp-stderr.log`` so the output is preserved for
debugging without polluting the TUI. Each server-start writes a
human-readable header line so operators can find a particular server's
output.

If opening the log file fails we fall back to ``/dev/null`` (and as a
last resort to the real stderr — the TUI may corrupt but the agent
won't crash).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime
from typing import Any


logger = logging.getLogger(__name__)


_log_fh: Any | None = None
_log_lock = threading.Lock()


def get_stderr_log() -> Any:
    """Return a shared, line-buffered file handle for MCP subprocess stderr.

    The handle is opened once per process and reused for every spawn.
    The MCP SDK requires a real OS file descriptor (``.fileno()``); a
    bare ``StringIO`` will not work.
    """
    global _log_fh
    with _log_lock:
        if _log_fh is not None:
            return _log_fh
        _log_fh = _open_log()
        return _log_fh


def write_stderr_log_header(server_name: str) -> None:
    """Emit a session marker before launching *server_name*.

    Lets operators grep the shared log for a particular server's output
    range without needing per-line prefixes (which would force a pipe +
    reader thread and complicate shutdown).
    """
    fh = get_stderr_log()
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fh.write(f"\n===== [{ts}] starting MCP server '{server_name}' =====\n")
        fh.flush()
    except Exception:
        # Worst-case: log header just doesn't appear. The subprocess
        # output itself still flows.
        pass


def _open_log() -> Any:
    try:
        from flowly.profile import get_flowly_home
        log_dir = get_flowly_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "mcp-stderr.log"
        fh = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        fh.fileno()  # sanity check — must be a real fd
        return fh
    except Exception as exc:
        logger.debug("MCP stderr log open failed, falling back to devnull: %s", exc)
    try:
        return open(os.devnull, "w", encoding="utf-8")
    except Exception:
        return sys.stderr
