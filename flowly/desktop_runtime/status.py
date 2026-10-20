"""Durable ordering of sampled public screen snapshots across gateway workers.

This sequence orders runtime phase/capability changes as well as control changes.
It is not a control epoch and never authorizes input. Only a fingerprint and a
counter are persisted; the callback must produce the closed public projection.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Callable

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import private_directory


class StatusSequence:
    def __init__(self, directory: Path):
        if os.name == "posix":
            private_directory(directory)
        else:
            directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "status.sqlite3"
        self.marker = directory / ".status-initialized"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)

    def snapshot(self, sample: Callable[[], dict]) -> dict:
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS status (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                revision INTEGER NOT NULL, fingerprint TEXT NOT NULL
            )""")
            row = connection.execute("SELECT revision, fingerprint FROM status WHERE singleton = 1").fetchone()
            if row is None and self.marker.exists():
                raise ScreenError("STATUS_STATE_INVALID")
            if row is not None and (type(row[0]) is not int or not 1 <= row[0] < 2**53
                                    or not isinstance(row[1], str) or len(row[1]) != 64):
                raise ScreenError("STATUS_STATE_INVALID")
            # Sampling outside this transaction would let a slow old snapshot
            # acquire a newer revision after another process sampled fresh state.
            value = sample()
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            if len(encoded) > 65536 or "statusRevision" in value:
                raise ScreenError("STATUS_STATE_INVALID")
            fingerprint = hashlib.sha256(encoded).hexdigest()
            revision = row[0] if row else 0
            if row is None or fingerprint != row[1]:
                revision += 1
                if revision >= 2**53:
                    raise ScreenError("STATUS_STATE_INVALID")
                connection.execute("INSERT OR REPLACE INTO status VALUES (1, ?, ?)", (revision, fingerprint))
            connection.execute("COMMIT")
            fd = os.open(self.marker, os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            return {**value, "statusRevision": revision}
        except (sqlite3.Error, OSError) as exc:
            raise ScreenError("CONTROL_UNAVAILABLE") from exc
        finally:
            if connection is not None:
                connection.close()
