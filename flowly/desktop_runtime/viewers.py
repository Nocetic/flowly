"""Hashed, bounded viewer resume capabilities; IDs alone never grant control.

Expiry/revocation denies new admissions, never releases durable human ownership.
An authenticated owner can explicitly reclaim after losing an app capability.
"""

from __future__ import annotations

import hashlib
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

    def create(self, scope: StreamScope, bot: str) -> dict:
        values = (*self._database._validate(scope), _identity(bot))
        token, viewer = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._database._connection() as connection:
            now = self._database._clock()
            connection.execute("DELETE FROM viewers WHERE expires <= ?", (now,))
            if connection.execute("SELECT COUNT(*) FROM viewers").fetchone()[0] >= self._database._capacity:
                raise ScreenError("SCREEN_BUSY")
            connection.execute("INSERT INTO viewers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (digest, *values, viewer, now + self.TTL))
        return {"viewerId": viewer, "viewerToken": token}

    def authenticate(self, token: str, scope: StreamScope, bot: str) -> str:
        values = (*self._database._validate(scope), _identity(bot))
        digest = hashlib.sha256(_identity(token).encode()).hexdigest()
        with self._database._connection() as connection:
            row = connection.execute(
                "SELECT owner, host, profile, generation, role, bot, viewer, expires FROM viewers WHERE digest = ?",
                (digest,),
            ).fetchone()
            if row is None or tuple(row[:6]) != values or row[7] <= self._database._clock():
                raise ScreenError("VIEWER_EXPIRED")
            connection.execute("UPDATE viewers SET expires = ? WHERE digest = ?",
                               (self._database._clock() + self.TTL, digest))
        return row[6]

    def active(self, viewer: str, scope: StreamScope, bot: str) -> bool:
        values = (*self._database._validate(scope), _identity(bot))
        with self._database._connection() as connection:
            row = connection.execute(
                "SELECT owner, host, profile, generation, role, bot, expires FROM viewers WHERE viewer = ?",
                (_identity(viewer),),
            ).fetchone()
        return row is not None and tuple(row[:6]) == values and row[6] > self._database._clock()

    def revoke(self, viewer: str) -> None:
        # Internal only: the caller has authenticated and completed any handback.
        with self._database._connection() as connection:
            connection.execute("DELETE FROM viewers WHERE viewer = ?", (_identity(viewer),))
            connection.execute("DELETE FROM tickets WHERE viewer = ?", (viewer,))
