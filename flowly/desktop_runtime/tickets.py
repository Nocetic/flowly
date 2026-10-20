"""Single-use, role-scoped stream admission shared across gateway workers."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal

from flowly.desktop_runtime.authority import ScreenError, _identity


@dataclass(frozen=True)
class StreamScope:
    owner: str
    host_id: str
    profile_id: str
    generation: str
    role: Literal["viewer", "agent"] = "viewer"


class StreamTickets:
    """No raw ticket is stored. Wrong-scope redemption also consumes the ticket.

    `owner` must come from server authentication, not client RPC parameters.
    The host runtime generation is required even when a relay ticket is used.
    """

    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.time,
                 capacity: int = 64):
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise ValueError("Invalid ticket capacity")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self._path = directory / "tickets.sqlite3"
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        self._clock, self._capacity = clock, capacity
        with self._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS tickets (
                digest TEXT PRIMARY KEY, owner TEXT NOT NULL, host TEXT NOT NULL,
                profile TEXT NOT NULL, generation TEXT NOT NULL, role TEXT NOT NULL,
                viewer TEXT NOT NULL, expires REAL NOT NULL
            )""")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            raise ScreenError("CONTROL_UNAVAILABLE") from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _validate(scope: StreamScope) -> tuple[str, ...]:
        values = (scope.owner, scope.host_id, scope.profile_id, scope.generation, scope.role)
        for value in values:
            _identity(value)
        if scope.role not in ("viewer", "agent"):
            raise ScreenError("INVALID_PARAMS")
        return values

    def issue(self, scope: StreamScope, viewer: str, *, ttl: int = 30) -> str:
        values = self._validate(scope)
        _identity(viewer)
        if type(ttl) is not int or not 1 <= ttl <= 30:
            raise ScreenError("INVALID_PARAMS")
        ticket = secrets.token_urlsafe(32)
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        with self._connection() as connection:
            now = self._clock()
            connection.execute("DELETE FROM tickets WHERE expires <= ?", (now,))
            count = connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            if count >= self._capacity:
                raise ScreenError("SCREEN_BUSY")
            connection.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (digest, *values, viewer, now + ttl))
        return ticket

    def consume(self, ticket: str, scope: StreamScope) -> str:
        values = self._validate(scope)
        _identity(ticket)
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT owner, host, profile, generation, role, viewer, expires FROM tickets WHERE digest = ?",
                (digest,),
            ).fetchone()
            connection.execute("DELETE FROM tickets WHERE digest = ?", (digest,))
        if row is None or row[6] <= self._clock() or tuple(row[:5]) != values:
            raise ScreenError("TICKET_EXPIRED")
        return row[5]

    def revoke_generation(self, generation: str) -> None:
        _identity(generation)
        with self._connection() as connection:
            connection.execute("DELETE FROM tickets WHERE generation = ?", (generation,))
