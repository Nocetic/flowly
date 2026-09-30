"""Durable acceptance receipts for chat turns, shared by gateway and relay.

This is a command ledger, not a task scheduler. A receipt from a previous
process whose outcome is unknown must never silently execute again. Durable
Board dispatch owns recovery for scheduled work.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

_PROCESS_ID = uuid.uuid4().hex
_TERMINAL = {"completed", "aborted", "error"}


def _goal_observation(value: dict) -> dict:
    """Keep only the runtime evidence needed to identify a goal generation."""
    unknown = {'available': False, 'goal': None}
    if not isinstance(value, dict) or value.get('available') is not True:
        return unknown
    goal = value.get('goal')
    if goal is None:
        return {'available': True, 'goal': None}
    if (not isinstance(goal, dict) or not isinstance(goal.get('goalId'), str)
            or not 1 <= len(goal['goalId']) <= 512
            or goal.get('status') not in {'active', 'paused', 'done', 'cleared'}
            or type(goal.get('revision')) is not int or goal['revision'] < 0):
        return unknown
    return {'available': True, 'goal': {
        'goalId': goal['goalId'], 'status': goal['status'], 'revision': goal['revision'],
        'createdByRunId': goal.get('createdByRunId') if isinstance(goal.get('createdByRunId'), str) else None,
    }}


def _goal_binding(before: dict, after: dict, run_id: str) -> dict:
    unknown = {'version': 1, 'state': 'unknown'}
    if not before['available']:
        return unknown
    previous, current = before['goal'], after['goal']
    if previous and previous['status'] in {'active', 'paused'}:
        # Replacing or deleting the goal during a turn never changes the
        # generation that this command was executing when it started.
        bound = previous
    elif not after['available']:
        return unknown
    elif current and current.get('createdByRunId') == run_id:
        bound = current
    elif current is None or (previous and current['goalId'] == previous['goalId']
                             and current['status'] in {'done', 'cleared'}):
        return {'version': 1, 'state': 'none'}
    else:
        return unknown
    return {'version': 1, 'state': 'goal', 'goalId': bound['goalId'], 'revision': bound['revision']}


class ChatCommandConflictError(ValueError):
    """A run identity was reused for different content or a different session."""


def validate_chat_target(params: dict) -> None:
    """Check pinned identity at the destination, after profile routing."""
    if 'expectedBotId' not in params:
        return
    from flowly.profile import current_profile_name, ensure_profile_bot_id

    expected = params['expectedBotId']
    if not isinstance(expected, str) or not expected or len(expected) > 128:
        raise ValueError('Assigned agent identity is invalid.')
    if ensure_profile_bot_id(current_profile_name()).bot_id != expected:
        raise ValueError('The assigned agent identity has changed. Select the agent again.')


def command_status(store: ChatCommandStore, params: dict) -> dict:
    values = [params.get('sessionKey'), params.get('runId')]
    if any(not isinstance(v, str) or not v or len(v) > 512 or any(ord(c) < 32 for c in v) for v in values):
        raise ValueError('A valid sessionKey and runId are required.')
    validate_chat_target(params)
    receipt = store.lookup(values[0], values[1])
    return receipt or {'runId': values[1], 'status': 'not_found', 'replayed': False}


def validate_command_control(store: ChatCommandStore, params: dict) -> None:
    if 'expectedBotId' in params and command_status(store, params)['status'] == 'not_found':
        raise ValueError('The worker run does not belong to this task conversation.')


class ChatCommandStore:
    @staticmethod
    def read_existing(path: Path, session_key: str, run_id: str) -> dict | None:
        """Read durable evidence without creating a store or adopting its owner."""
        if not path.is_file():
            return None
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('''SELECT c.status, g.binding_json FROM chat_commands c
                LEFT JOIN chat_command_goals g ON g.run_id = c.run_id
                WHERE c.session_key = ? AND c.run_id = ?''', (session_key, run_id)).fetchone()
            if row is None:
                return None
            return {'runId': run_id, 'status': row['status'] if row['status'] in _TERMINAL else 'status_unknown',
                    'goalBinding': json.loads(row['binding_json']) if row['binding_json'] else None}

    def __init__(self, path: Path | str, *, owner_id: str = _PROCESS_ID):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.owner_id = owner_id
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS chat_commands (
                run_id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL,
                accepted_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )""")
            self._db.execute("CREATE INDEX IF NOT EXISTS chat_commands_session ON chat_commands(session_key)")
            columns = {row['name'] for row in self._db.execute('PRAGMA table_info(chat_commands)')}
            if 'voice_owner_json' not in columns:
                self._db.execute('ALTER TABLE chat_commands ADD COLUMN voice_owner_json TEXT')
            self._db.execute('''CREATE TABLE IF NOT EXISTS chat_command_goals (
                run_id TEXT PRIMARY KEY REFERENCES chat_commands(run_id) ON DELETE CASCADE,
                before_json TEXT NOT NULL,
                binding_json TEXT NOT NULL
            )''')

    def accept(self, session_key: str, run_id: str, params: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        for value in (session_key, run_id):
            if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
                raise ValueError("Session and command identities must be non-empty strings of at most 512 characters.")
        from flowly.session.control_access import SessionControlScope

        scope = SessionControlScope.capture(session_key)
        canonical = {**params, "sessionKey": session_key}
        canonical.pop("idempotencyKey", None)
        fingerprint = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        now = time.time()
        with scope.guard(session_key) as allowed:
            if not allowed:
                from flowly.session.ownership import SessionAccessError

                raise SessionAccessError()
            with self._lock, self._db:
                # INSERT OR IGNORE holds the SQLite write lock until the receipt
                # has been checked. Two connections cannot both win acceptance.
                inserted = self._db.execute(
                    """INSERT OR IGNORE INTO chat_commands
                        (run_id, session_key, fingerprint, owner_id, status, accepted_at, updated_at, voice_owner_json)
                        VALUES (?, ?, ?, ?, 'accepted', ?, ?, ?)""",
                    (run_id, session_key, fingerprint, self.owner_id, now, now, json.dumps(scope.owner)),
                ).rowcount == 1
                row = self._db.execute("SELECT * FROM chat_commands WHERE run_id = ?", (run_id,)).fetchone()
                if self._row_control_scope(row, sessions_dir=scope.path.parent).owner != scope.owner:
                    from flowly.session.ownership import SessionAccessError

                    raise SessionAccessError()
                if row["session_key"] != session_key or row["fingerprint"] != fingerprint:
                    raise ChatCommandConflictError("This command identity already belongs to a different request.")
                return inserted, self._receipt(row, replayed=not inserted)

    def _receipt(self, row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
        status = row["status"]
        if row["owner_id"] != self.owner_id and status not in _TERMINAL:
            status = "status_unknown"
        receipt = {"runId": row["run_id"], "status": status, "replayed": replayed}
        binding = self._db.execute('SELECT binding_json FROM chat_command_goals WHERE run_id = ?',
                                   (row['run_id'],)).fetchone()
        if binding:
            receipt['goalBinding'] = json.loads(binding['binding_json'])
        return receipt

    def begin_execution(self, session_key: str, run_id: str, goal: dict) -> None:
        """Capture the actual goal before executing, inside the session turn lock."""
        observation = _goal_observation(goal)
        pending = {'version': 1, 'state': 'pending'}
        previous = observation['goal']
        if previous and previous['status'] in {'active', 'paused'}:
            pending.update(goalId=previous['goalId'], revision=previous['revision'])
        with self._lock, self._db:
            row = self._db.execute(
                """SELECT 1 FROM chat_commands WHERE run_id = ? AND session_key = ?
                   AND owner_id = ? AND status IN ('accepted', 'running')""",
                (run_id, session_key, self.owner_id),
            ).fetchone()
            if not row:
                return
            self._db.execute('INSERT OR IGNORE INTO chat_command_goals VALUES (?, ?, ?)',
                             (run_id, json.dumps(observation), json.dumps(pending)))
            self._db.execute("UPDATE chat_commands SET status = 'running', updated_at = ? WHERE run_id = ?",
                             (time.time(), run_id))

    def finish_execution(self, session_key: str, run_id: str, goal: dict, status: str) -> None:
        """Atomically bind the turn's goal and outcome before releasing its lock."""
        if status not in _TERMINAL:
            raise ValueError('Invalid execution outcome')
        observation = _goal_observation(goal)
        with self._lock, self._db:
            row = self._db.execute(
                """SELECT g.before_json FROM chat_commands AS c
                   LEFT JOIN chat_command_goals AS g ON c.run_id = g.run_id
                   WHERE c.run_id = ? AND c.session_key = ? AND c.owner_id = ?
                   AND c.status IN ('accepted', 'running')""", (run_id, session_key, self.owner_id),
            ).fetchone()
            if not row:
                return
            before = json.loads(row['before_json']) if row['before_json'] else _goal_observation({})
            binding = _goal_binding(before, observation, run_id)
            self._db.execute('''INSERT INTO chat_command_goals VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET binding_json = excluded.binding_json''',
                (run_id, json.dumps(before), json.dumps(binding)))
            self._db.execute('UPDATE chat_commands SET status = ?, updated_at = ? WHERE run_id = ?',
                             (status, time.time(), run_id))

    def lookup(self, session_key: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM chat_commands WHERE run_id = ? AND session_key = ?",
                (run_id, session_key),
            ).fetchone()
        if row is None:
            return None
        with self._row_control_scope(row).guard(session_key) as allowed:
            if not allowed:
                return None
            with self._lock:
                return self._receipt(row, replayed=True)

    def control_scope(self, run_id: str, *, sessions_dir: Path | None = None):
        """Internal target lookup; the caller must apply its control guard."""
        with self._lock:
            row = self._db.execute('SELECT session_key, voice_owner_json FROM chat_commands WHERE run_id = ?',
                                   (run_id,)).fetchone()
        if row is None:
            return None
        return self._row_control_scope(row, sessions_dir=sessions_dir)

    @staticmethod
    def _row_control_scope(row, *, sessions_dir: Path | None = None):
        from flowly.session.control_access import SessionControlScope
        from flowly.session.ownership import SessionAccessError, is_owned_session

        try:
            owner = json.loads(row['voice_owner_json']) if row['voice_owner_json'] is not None else (
                {'kind': 'host'} if is_owned_session(row['session_key'], {}) else None)
            if owner is None and is_owned_session(row['session_key'], {}):
                raise SessionAccessError()
            return SessionControlScope.bind(row['session_key'], owner, sessions_dir=sessions_dir)
        except (ValueError, TypeError):
            raise SessionAccessError() from None

    def settle(self, session_key: str, run_id: str, status: str) -> None:
        if status not in _TERMINAL | {"running"}:
            raise ValueError("Invalid command status")
        with self._lock, self._db:
            self._db.execute(
                """UPDATE chat_commands SET status = ?, updated_at = ?
                   WHERE run_id = ? AND session_key = ? AND owner_id = ?
                     AND status IN ('accepted', 'running')""",
                (status, time.time(), run_id, session_key, self.owner_id),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
