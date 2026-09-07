"""Shared short config-write lease and optimistic raw-editor snapshots."""

import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from flowly.mcp.oauth_state import state_lock


@contextmanager
def config_write_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # SQLite's process lock is also available in Electron's bundled Node.
    # No lock rows, credentials, expiry heuristic or stale-PID deletion: the OS
    # releases the transaction when either runtime closes or exits.
    lease_path = path.with_suffix(".write-lock.sqlite")
    descriptor = os.open(lease_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Invalid config write lease")
    finally:
        os.close(descriptor)
    connection = sqlite3.connect(lease_path, timeout=0.25)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise TimeoutError("Configuration is busy; retry the save") from None
            raise
        # Retain compatibility with earlier core-only setup transactions.
        with state_lock(path.with_suffix(".mcp.lock"), timeout=0.25):
            yield
    finally:
        connection.close()


class ConfigSnapshot(dict):
    """A normal JSON mapping that remembers what its editor actually read."""

    def __init__(self, values: dict, path: Path, source: str | None):
        super().__init__(values)
        self.source_path = path.resolve()
        self.source_text = source

    def check_current(self, path: Path) -> None:
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        if path.resolve() != self.source_path or current != self.source_text:
            raise ValueError("Configuration changed while editing; reload settings and try again")
