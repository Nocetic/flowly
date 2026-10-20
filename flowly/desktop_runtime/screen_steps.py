"""Screen steps: the step the agent handed the person on its screen, as task state.

A step (sign in, a code, a CAPTCHA, a payment) is a record of the conversation,
not a question in the agent's turn: it outlives the turn, the window, the
gateway process and a stopped screen, and ends only when the person has taken
the screen and handed it back (``done``), the agent takes it back
(``withdrawn``), a later step replaces it (``superseded``), or nobody did it
within the horizon (``not_done``). One step waits per conversation. The card a
client shows is a projection of this record; the watcher that resolves it is
rebuilt from this store when the gateway starts.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenError, _identity
from flowly.desktop_runtime.launcher import private_directory

STATES = ("waiting", "done", "not_done", "withdrawn", "superseded")
TERMINAL = frozenset(STATES) - {"waiting"}
# A forgotten step ends by itself after this long (Grok: an active chat's request
# waits; this is the housekeeping bound, not a deadline the person is held to).
HORIZON_SECONDS = 30 * 24 * 60 * 60
# Settled steps kept per conversation, and in all.
HISTORY_PER_CONVERSATION = 16
HISTORY_TOTAL = 256
_PUBLIC = ("id", "sessionKey", "task", "profileId", "state", "createdAt", "takenAt", "resolvedAt")
_COLUMNS = ("id", "session_key", "channel", "chat_id", "task", "host_id", "bot_id", "profile_id",
            "created_at", "taken_at", "taken_generation", "watch_generation", "watch_target", "state", "resolved_at")


def normalized_task(task: object) -> str:
    return " ".join(str(task or "").split())[:300]


class ScreenSteps:
    """The durable step store of one agent's screen (its profile's ``desktop`` directory)."""

    def __init__(self, directory: Path, *, clock=time.time):
        if os.name == "posix":
            private_directory(directory)
        else:
            directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "steps.sqlite3"
        self._clock = clock
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)

    @contextmanager
    def _connection(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS steps (
                serial INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, session_key TEXT NOT NULL,
                channel TEXT NOT NULL, chat_id TEXT NOT NULL, task TEXT NOT NULL, host_id TEXT NOT NULL,
                bot_id TEXT NOT NULL, profile_id TEXT NOT NULL, created_at REAL NOT NULL, taken_at REAL,
                taken_generation TEXT, watch_generation TEXT, watch_target INTEGER, state TEXT NOT NULL,
                resolved_at REAL)""")
            yield connection
            connection.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            raise ScreenError("CONTROL_UNAVAILABLE") from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _row(values) -> dict:
        return dict(zip(_COLUMNS, values))

    @staticmethod
    def public(row: dict) -> dict:
        return {"id": row["id"], "sessionKey": row["session_key"], "task": row["task"], "profileId": row["profile_id"],
                "state": row["state"], "createdAt": row["created_at"], "takenAt": row["taken_at"],
                "resolvedAt": row["resolved_at"]}

    def _select(self, connection, where: str, params: tuple) -> list[dict]:
        columns = ", ".join(_COLUMNS)
        return [self._row(values) for values in
                connection.execute(f"SELECT {columns} FROM steps WHERE {where} ORDER BY serial", params)]

    def open(self, identity: dict, session_key: str, channel: str, chat_id: str, task: str) -> tuple[dict, dict | None]:
        """Open a waiting step; the conversation's earlier waiting step is superseded.

        Returns the new step and the superseded one (or None).
        """
        if not isinstance(identity, dict) or set(identity) != {"hostId", "botId", "profileId"}:
            raise ScreenError("INVALID_PARAMS")
        for value in (*identity.values(), session_key):
            _identity(value)
        task = normalized_task(task)
        if not task or not isinstance(channel, str) or not isinstance(chat_id, str):
            raise ScreenError("INVALID_PARAMS")
        now = self._clock()
        with self._connection() as connection:
            previous = self._select(connection, "session_key = ? AND state = 'waiting'", (session_key,))
            superseded = None
            for old in previous:
                connection.execute("UPDATE steps SET state = 'superseded', resolved_at = ? WHERE id = ?", (now, old["id"]))
                superseded = {**old, "state": "superseded", "resolved_at": now}
            step = {"id": secrets.token_hex(8), "session_key": session_key, "channel": channel[:256], "chat_id": chat_id[:256],
                    "task": task, "host_id": identity["hostId"], "bot_id": identity["botId"],
                    "profile_id": identity["profileId"], "created_at": now, "taken_at": None, "taken_generation": None,
                    "watch_generation": None, "watch_target": None, "state": "waiting", "resolved_at": None}
            connection.execute(f"INSERT INTO steps ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
                               tuple(step[column] for column in _COLUMNS))
            self._prune(connection, session_key)
        return step, superseded

    def _prune(self, connection, session_key: str) -> None:
        connection.execute("""DELETE FROM steps WHERE state != 'waiting' AND session_key = ? AND serial NOT IN (
            SELECT serial FROM steps WHERE state != 'waiting' AND session_key = ? ORDER BY serial DESC LIMIT ?)""",
                           (session_key, session_key, HISTORY_PER_CONVERSATION))
        connection.execute("""DELETE FROM steps WHERE state != 'waiting' AND serial NOT IN (
            SELECT serial FROM steps WHERE state != 'waiting' ORDER BY serial DESC LIMIT ?)""", (HISTORY_TOTAL,))

    def waiting(self, session_key: str) -> dict | None:
        with self._connection() as connection:
            rows = self._select(connection, "session_key = ? AND state = 'waiting'", (session_key,))
        return rows[-1] if rows else None

    def all_waiting(self) -> list[dict]:
        with self._connection() as connection:
            return self._select(connection, "state = 'waiting'", ())

    def get(self, step_id: str) -> dict | None:
        with self._connection() as connection:
            rows = self._select(connection, "id = ?", (step_id,))
        return rows[0] if rows else None

    def recent(self, session_key: str, limit: int = HISTORY_PER_CONVERSATION) -> list[dict]:
        """The conversation's steps, oldest first, as clients see them."""
        with self._connection() as connection:
            rows = self._select(connection, "session_key = ?", (session_key,))
        return [self.public(row) for row in rows[-limit:]]

    def mark_taken(self, step_id: str, generation: str) -> dict | None:
        """The person took the screen while this step waited."""
        _identity(generation)
        with self._connection() as connection:
            rows = self._select(connection, "id = ? AND state = 'waiting'", (step_id,))
            if not rows:
                return None
            connection.execute("UPDATE steps SET taken_at = ?, taken_generation = ? WHERE id = ?",
                               (self._clock(), generation, step_id))
            rows = self._select(connection, "id = ?", (step_id,))
        return rows[0]

    def watch(self, step_id: str, generation: str, target: int) -> dict | None:
        """What ends this step: the screen generation and the control epoch that
        means the person took the screen and handed it back (a takeover and a
        release each move the epoch by one). Durable, so a restarted watcher
        decides from the same mark; a new generation gets a new mark."""
        _identity(generation)
        if type(target) is not int or target < 0:
            raise ScreenError("INVALID_PARAMS")
        with self._connection() as connection:
            changed = connection.execute(
                "UPDATE steps SET watch_generation = ?, watch_target = ? WHERE id = ? AND state = 'waiting'",
                (generation, target, step_id)).rowcount
            rows = self._select(connection, "id = ?", (step_id,)) if changed else []
        return rows[0] if rows else None

    def resolve(self, step_id: str, state: str) -> dict | None:
        """End a waiting step; None when it had ended already (the first end wins)."""
        if state not in TERMINAL:
            raise ScreenError("INVALID_PARAMS")
        with self._connection() as connection:
            changed = connection.execute("UPDATE steps SET state = ?, resolved_at = ? WHERE id = ? AND state = 'waiting'",
                                         (state, self._clock(), step_id)).rowcount
            rows = self._select(connection, "id = ?", (step_id,)) if changed else []
            if rows:
                self._prune(connection, rows[0]["session_key"])
        return rows[0] if rows else None
