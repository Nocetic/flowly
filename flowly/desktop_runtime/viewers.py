"""Hashed, bounded viewer resume capabilities; IDs alone never grant control.

Expiry/revocation denies new admissions, never releases durable human ownership.
An authenticated owner can explicitly reclaim after losing an app capability.
"""

from __future__ import annotations

import hashlib
import json
import secrets

from flowly.desktop_runtime.authority import ScreenError, _identity
from flowly.desktop_runtime.tickets import StreamScope, StreamTickets


class ViewerCredentials:
    TTL = 24 * 60 * 60

    def __init__(self, tickets: StreamTickets):
        self._database = tickets
        with tickets._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS viewers (
                digest TEXT PRIMARY KEY, owner TEXT NOT NULL, host TEXT NOT NULL,
                profile TEXT NOT NULL, generation TEXT NOT NULL, role TEXT NOT NULL,
                bot TEXT NOT NULL, viewer TEXT UNIQUE NOT NULL, expires REAL NOT NULL
            )""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(viewers)")}
            for name, kind in (("close_operation", "TEXT"), ("close_epoch", "INTEGER"), ("close_result", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE viewers ADD COLUMN {name} {kind}")

    def create(self, scope: StreamScope, bot: str) -> dict:
        values = (*self._database._validate(scope), _identity(bot))
        token, viewer = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._database._connection() as connection:
            now = self._database._clock()
            connection.execute("DELETE FROM viewers WHERE expires <= ?", (now,))
            if connection.execute("SELECT COUNT(*) FROM viewers WHERE close_result IS NULL").fetchone()[0] >= self._database._capacity:
                raise ScreenError("SCREEN_BUSY")
            connection.execute("""INSERT INTO viewers
                               (digest, owner, host, profile, generation, role, bot, viewer, expires)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                               (digest, *values, viewer, now + self.TTL))
        return {"viewerId": viewer, "viewerToken": token}

    def authenticate(self, token: str, scope: StreamScope, bot: str, *, close: tuple[str, int] | None = None) -> str:
        values = (*self._database._validate(scope), _identity(bot))
        digest = hashlib.sha256(_identity(token).encode()).hexdigest()
        with self._database._connection() as connection:
            row = connection.execute(
                """SELECT owner, host, profile, generation, role, bot, viewer, expires,
                          close_operation, close_epoch FROM viewers WHERE digest = ?""",
                (digest,),
            ).fetchone()
            if row is None or tuple(row[:6]) != values or row[7] <= self._database._clock():
                raise ScreenError("VIEWER_EXPIRED")
            if row[8] is not None and close != tuple(row[8:10]):
                raise ScreenError("VIEWER_REVOKED" if close is None else "OPERATION_CONFLICT")
            connection.execute("UPDATE viewers SET expires = ? WHERE digest = ?",
                               (self._database._clock() + self.TTL, digest))
        return row[6]

    def active(self, viewer: str, scope: StreamScope, bot: str) -> bool:
        values = (*self._database._validate(scope), _identity(bot))
        with self._database._connection() as connection:
            row = connection.execute(
                """SELECT owner, host, profile, generation, role, bot, expires FROM viewers
                   WHERE viewer = ? AND close_operation IS NULL""",
                (_identity(viewer),),
            ).fetchone()
        return row is not None and tuple(row[:6]) == values and row[6] > self._database._clock()

    def available(self, viewer: str, generation: str) -> bool:
        """Supervisor rechecks an already authenticated actor at execution time."""
        with self._database._connection() as connection:
            return connection.execute(
                """SELECT 1 FROM viewers WHERE viewer = ? AND generation = ?
                   AND expires > ? AND close_operation IS NULL""",
                (_identity(viewer), _identity(generation), self._database._clock()),
            ).fetchone() is not None

    def close_record(self, viewer: str, generation: str, operation: str, epoch: int) -> dict | None:
        with self._database._connection() as connection:
            row = connection.execute(
                """SELECT close_operation, close_epoch, close_result FROM viewers
                   WHERE viewer = ? AND generation = ? AND expires > ?""",
                (_identity(viewer), _identity(generation), self._database._clock()),
            ).fetchone()
        if row is None:
            raise ScreenError("VIEWER_EXPIRED")
        if row[0] is None:
            return None
        if row[:2] != (operation, epoch):
            raise ScreenError("OPERATION_CONFLICT")
        return {"result": json.loads(row[2]) if row[2] is not None else None}

    def begin_close(self, viewer: str, operation: str, epoch: int) -> None:
        # Called only under the supervisor's command lock. This durable intent
        # fences late control/input/admission before draining existing sockets.
        with self._database._connection() as connection:
            changed = connection.execute(
                """UPDATE viewers SET close_operation = ?, close_epoch = ?
                   WHERE viewer = ? AND close_operation IS NULL AND expires > ?""",
                (_identity(operation), epoch, _identity(viewer), self._database._clock()),
            ).rowcount
            if changed != 1:
                raise ScreenError("VIEWER_REVOKED")
            connection.execute("DELETE FROM tickets WHERE viewer = ?", (viewer,))

    def complete_close(self, viewer: str, operation: str, epoch: int, result: dict) -> None:
        with self._database._connection() as connection:
            changed = connection.execute(
                """UPDATE viewers SET close_result = ?
                   WHERE viewer = ? AND close_operation = ? AND close_epoch = ?""",
                (json.dumps(result, separators=(",", ":")), viewer, operation, epoch),
            ).rowcount
            if changed != 1:
                raise ScreenError("VIEWER_REVOKED")
            # Keep bounded historical receipts, separate from active capacity.
            # An evicted token can only fail; it can never initiate another close.
            connection.execute("""DELETE FROM viewers WHERE viewer IN (
                SELECT viewer FROM viewers WHERE close_result IS NOT NULL
                ORDER BY rowid DESC LIMIT -1 OFFSET ?
            )""", (self._database._capacity * 4,))

    def revoke(self, viewer: str) -> None:
        # Internal only: the caller has authenticated and completed any handback.
        with self._database._connection() as connection:
            connection.execute("DELETE FROM viewers WHERE viewer = ?", (_identity(viewer),))
            connection.execute("DELETE FROM tickets WHERE viewer = ?", (viewer,))
