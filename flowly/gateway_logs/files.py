"""Which file is the gateway's log, and how to follow it across rotation.

Every gateway run — service, Desktop-managed or ``flowly gateway`` in a
terminal — writes ``<log dir>/gateway.log`` through its loguru file sink
(``flowly.cli.gateway_cmd._install_gateway_file_sink``), rotated daily. The
service manager's stdio captures (``flowly-gateway.err.log`` / ``.out.log``)
are only a fallback for installs older than that sink: they hold raw crash
output and, on many machines, months-old lines.

A byte offset alone cannot follow a rotating file: after midnight the new
``gateway.log`` can grow past yesterday's offset and a reader would resume in
the middle of it. The file's identity (``file_id``) travels with the offset,
and a different identity means "start again from the tail".
"""

from __future__ import annotations

import hashlib
import os
import platform
from pathlib import Path

CURRENT = "gateway.log"
LEGACY = ("flowly-gateway.err.log", "flowly-gateway.out.log")
FIRST_LINE_BYTES = 256


def log_dir() -> Path:
    if platform.system().lower() == "windows":
        return Path.home() / "AppData" / "Local" / "flowly" / "logs"
    from flowly.profile import get_flowly_home

    return get_flowly_home() / "logs"


def current_log_file() -> Path | None:
    """The live log: ``gateway.log`` when it exists, else a legacy capture."""
    directory = log_dir()
    for name in (CURRENT, *LEGACY):
        path = directory / name
        if path.is_file():
            return path
    return None


def file_id(path: Path) -> str:
    """Identity of one incarnation of the file; changes when it is rotated.

    Neither half is enough alone: Linux reuses a freed inode (the rotated file
    is compressed and deleted), and ctime/mtime move on every write. The first
    line of a log starts with its millisecond timestamp, so inode plus a hash
    of that line tells two incarnations apart. Until the first line is
    complete the hash is of nothing, which only costs one extra fresh read.
    """
    with path.open("rb") as handle:
        head = handle.read(FIRST_LINE_BYTES)
        inode = os.fstat(handle.fileno()).st_ino
    newline = head.find(b"\n")
    first = head[:newline] if newline >= 0 else (head if len(head) == FIRST_LINE_BYTES else b"")
    return f"{path.name}:{inode}:{hashlib.sha1(first).hexdigest()[:12]}"
