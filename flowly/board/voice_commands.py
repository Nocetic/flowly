"""Turn instructions in the Board's existing database and claim transaction.

A queued instruction is not an acknowledgement from the worker. Only the
worker receipt moves it to applied. Uncertain execution is never retried by
the Board dispatcher. All *_locked methods share their caller's transaction.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import TYPE_CHECKING

from flowly.board.store import _MAX_RESULT_CHARS, BoardError

if TYPE_CHECKING:
    from flowly.board.store import BoardStore, Card

_PROCESS_ID = uuid.uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS card_commands (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT NOT NULL UNIQUE,
    card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    claim_token TEXT,
    worker_run_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    response_owner TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_card_commands_queue ON card_commands(card_id, status, seq);
"""


def _wire(row) -> dict:
    status = row['status']
    if row['kind'] == 'respond' and status == 'delivering' and row['response_owner'] != _PROCESS_ID:
        status = 'status_unknown'
    return {
        'commandId': row['command_id'], 'cardId': row['card_id'],
        'kind': row['kind'], 'text': row['text'], 'status': status,
        'workerRunId': row['worker_run_id'], 'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
    }


class VoiceCommands:
    def __init__(self, store: BoardStore):
        self.store = store
        self.db = store._conn

    def migrate_locked(self) -> None:
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(card_commands)')}
        if 'response_owner' not in columns:
            self.db.execute("ALTER TABLE card_commands ADD COLUMN response_owner TEXT NOT NULL DEFAULT ''")

    def seed_locked(self, card_id: str, text: str, now: float) -> None:
        self.store._require_card_locked(card_id)
        self.db.execute(
            "INSERT INTO card_commands (command_id, card_id, kind, fingerprint, text, status, "
            "created_at, updated_at) VALUES (?, ?, 'initial', '', ?, 'queued_for_next_turn', ?, ?)",
            (f'voice:{card_id}:initial', card_id, text, now, now),
        )

    def _card_locked(self, conversation_id: str, card_id: str):
        card = self.store._get_card_locked(card_id)
        if card is None or card.execution_mode != 'voice' or card.voice_conversation_id != conversation_id:
            raise BoardError('task not found in this voice conversation')
        return card

    def _require_command_card_locked(self, command_id: str) -> None:
        row = self.db.execute('SELECT card_id FROM card_commands WHERE command_id = ?', (command_id,)).fetchone()
        if row is not None:
            self.store._require_card_locked(row['card_id'])

    def list(self, conversation_id: str, card_id: str) -> list[dict]:
        with self.store._lock:
            self._card_locked(conversation_id, card_id)
            return [_wire(r) for r in self.db.execute(
                'SELECT * FROM card_commands WHERE card_id = ? ORDER BY seq', (card_id,),
            )]

    def latest_instructions(self, conversation_id: str) -> dict[str, dict]:
        """Small UI projection; never include control payloads or private run claims."""
        visible, owner_args = self.store._visibility_sql('t')
        with self.store._lock:
            rows = self.db.execute(
                "SELECT c.card_id, c.command_id, c.status FROM card_commands c JOIN "
                "(SELECT MAX(c.seq) AS seq FROM card_commands c JOIN cards t ON t.id = c.card_id "
                f"WHERE t.voice_conversation_id = ? AND ({visible}) AND c.kind = 'steer' GROUP BY c.card_id) latest "
                "ON latest.seq = c.seq", (conversation_id, *owner_args),
            ).fetchall()
            return {r['card_id']: {'commandId': r['command_id'], 'status': r['status']} for r in rows}

    def enqueue(self, *, conversation_id: str, card_id: str, command_id: str,
                expected_revision: int, text: str) -> dict:
        if (not isinstance(command_id, str) or not 1 <= len(command_id) <= 128
                or any(not (c.isascii() and (c.isalnum() or c in '.-')) for c in command_id)):
            raise BoardError('command identity is invalid')
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise BoardError('task revision is invalid')
        if not isinstance(text, str) or not text.strip() or len(text) > 30000:
            raise BoardError('instruction must contain 1–30,000 characters')
        text = text.strip()
        fingerprint = hashlib.sha256(json.dumps({
            'conversation': conversation_id, 'card': card_id, 'text': text,
            'revision': expected_revision, 'kind': 'steer',
        }, sort_keys=True).encode()).hexdigest()
        with self.store._lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            card = self._card_locked(conversation_id, card_id)
            existing = self.db.execute('SELECT * FROM card_commands WHERE command_id = ?', (command_id,)).fetchone()
            if existing is not None:
                if existing['fingerprint'] != fingerprint:
                    raise BoardError('command identity already belongs to a different request')
                return _wire(existing)
            if card.revision != expected_revision:
                raise BoardError('task revision has changed; refresh the task before continuing')
            if card.status == 'archived':
                raise BoardError('archived task cannot receive instructions')
            if self._pending_stop_locked(card_id):
                raise BoardError('task cancellation is still being verified')
            if self.db.execute(
                "SELECT 1 FROM card_commands WHERE card_id = ? AND kind IN ('initial', 'steer') "
                "AND status = 'status_unknown'", (card_id,),
            ).fetchone():
                raise BoardError('task has an unverified worker outcome; reconcile it before continuing')
            count = self.db.execute('SELECT COUNT(*) FROM card_commands WHERE card_id = ?', (card_id,)).fetchone()[0]
            if count >= 200:
                raise BoardError('this task has reached its instruction limit; start a new task')
            now = time.time()
            self.db.execute(
                "INSERT INTO card_commands (command_id, card_id, kind, fingerprint, text, status, "
                "created_at, updated_at) VALUES (?, ?, 'steer', ?, ?, 'queued_for_next_turn', ?, ?)",
                (command_id, card_id, fingerprint, text, now, now),
            )
            # A new explicit instruction can resume a terminal task. Previously
            # held instructions remain held; they are never implicitly approved.
            self.db.execute(
                "UPDATE cards SET status = ?, scheduled_at = NULL, revision = revision + 1, "
                "updated_at = ? WHERE id = ?",
                ('in_progress' if card.claim_token else 'ready', now, card_id),
            )
            self.store._record_event_locked(card_id, 'command_queued', 'user', {
                'commandId': command_id, 'kind': 'steer', 'status': 'queued_for_next_turn',
            }, now=now)
            return _wire(self.db.execute('SELECT * FROM card_commands WHERE command_id = ?', (command_id,)).fetchone())

    def next_locked(self, card: Card):
        self.store._require_card_locked(card.id)
        if self._pending_stop_locked(card.id):
            return None
        if self.db.execute(
            "SELECT 1 FROM card_commands WHERE card_id = ? AND kind IN ('initial', 'steer') "
            "AND status IN ('status_unknown', 'delivering', 'applied')",
            (card.id,),
        ).fetchone():
            return None
        return self.db.execute(
            "SELECT * FROM card_commands WHERE card_id = ? AND status = 'queued_for_next_turn' ORDER BY seq LIMIT 1",
            (card.id,),
        ).fetchone()

    def claim_locked(self, command_id: str, claim_token: str, now: float) -> None:
        self._require_command_card_locked(command_id)
        self.db.execute(
            "UPDATE card_commands SET status = 'delivering', claim_token = ?, updated_at = ? "
            "WHERE command_id = ? AND status = 'queued_for_next_turn'", (claim_token, now, command_id),
        )

    def claimed(self, card_id: str, claim_token: str) -> dict:
        with self.store._lock:
            self.store._require_card_locked(card_id)
            row = self.db.execute(
                "SELECT * FROM card_commands WHERE card_id = ? AND claim_token = ? "
                "AND status IN ('delivering', 'applied')", (card_id, claim_token),
            ).fetchone()
            if row is None:
                raise BoardError('task instruction claim is stale')
            return _wire(row)

    def applied_locked(self, card_id: str, claim_token: str, worker_run_id: str, now: float) -> None:
        self.store._require_card_locked(card_id)
        row = self.db.execute(
            "SELECT command_id FROM card_commands WHERE card_id = ? AND claim_token = ? AND status = 'delivering'",
            (card_id, claim_token),
        ).fetchone()
        if row is None:
            return
        self.db.execute(
            "UPDATE card_commands SET status = 'applied', worker_run_id = ?, updated_at = ? WHERE command_id = ?",
            (worker_run_id, now, row['command_id']),
        )
        self.store._record_event_locked(card_id, 'command_applied', 'worker', {
            'commandId': row['command_id'], 'workerRunId': worker_run_id,
        }, now=now)

    def advance_boundary(self, card_id: str, claim_token: str, completed_command_id: str) -> dict | None:
        """Finish a verified user turn and claim at most one next instruction.

        The task's standing goal and heartbeat keep the same Board claim. A
        queued instruction becomes applied only after its own worker starts.
        """
        with self.store._lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            card = self.store._get_card_locked(card_id)
            if card is None or card.execution_mode != 'voice' or card.claim_token != claim_token:
                raise BoardError('task instruction claim is stale')
            row = self.db.execute(
                "SELECT * FROM card_commands WHERE card_id = ? AND claim_token = ? AND command_id = ? "
                "AND kind IN ('initial', 'steer')", (card_id, claim_token, completed_command_id),
            ).fetchone()
            if row is None or row['status'] not in {'applied', 'completed'}:
                raise BoardError('the worker turn has not completed')
            if self._pending_stop_locked(card_id):
                return None
            now = time.time()
            if row['status'] == 'applied':
                self.db.execute("UPDATE card_commands SET status = 'completed', updated_at = ? WHERE command_id = ?",
                                (now, completed_command_id))
                self.store._record_event_locked(card_id, 'command_completed', 'worker', {
                    'commandId': completed_command_id, 'workerRunId': row['worker_run_id'],
                }, now=now)
            next_command = self.next_locked(card)
            if next_command is None:
                return None
            self.claim_locked(next_command['command_id'], claim_token, now)
            return _wire(self.db.execute('SELECT * FROM card_commands WHERE command_id = ?',
                                        (next_command['command_id'],)).fetchone())

    def finish_locked(self, card: Card, outcome: str, now: float, *, uncertain: bool = False) -> bool:
        self.store._require_card_locked(card.id)
        status = 'status_unknown' if uncertain else ('completed' if outcome == 'done' else outcome)
        updated = self.db.execute(
            "UPDATE card_commands SET status = ?, updated_at = ? WHERE card_id = ? AND claim_token = ? "
            "AND status IN ('delivering', 'applied')", (status, now, card.id, card.claim_token),
        ).rowcount
        if uncertain and not updated:
            # The last user turn may be verified while its continuing goal
            # becomes unreachable. Preserve the gate against blind replay.
            self.db.execute(
                "UPDATE card_commands SET status = 'status_unknown', updated_at = ? WHERE seq = "
                "(SELECT MAX(seq) FROM card_commands WHERE card_id = ? AND claim_token = ? "
                "AND kind IN ('initial', 'steer') AND status = 'completed')",
                (now, card.id, card.claim_token),
            )
        if outcome != 'done' or uncertain:
            self.db.execute(
                "UPDATE card_commands SET status = 'held', updated_at = ? "
                "WHERE card_id = ? AND status = 'queued_for_next_turn'", (now, card.id),
            )
            return False
        return self.next_locked(card) is not None

    def pending_reconciliations(self, *, after_seq: int = 0, limit: int = 5) -> list[dict]:
        """Return fenced orphan observations in bounded, seekable order."""
        visible, owner_args = self.store._visibility_sql('c')
        with self.store._lock:
            rows = self.db.execute(f'''SELECT q.seq, q.command_id, q.card_id, q.claim_token,
                    q.worker_run_id, c.revision, c.attempt_count, c.assignee_profile,
                    c.assignee_bot_id, c.session_key
                FROM card_commands q JOIN cards c ON c.id = q.card_id
                JOIN card_runs r ON r.card_id = c.id AND r.claim_token = q.claim_token
                    AND r.attempt = c.attempt_count AND r.profile = c.assignee_profile
                WHERE q.kind IN ('initial', 'steer') AND q.status = 'status_unknown'
                    AND q.seq > ? AND ({visible}) AND c.execution_mode = 'voice' AND c.claim_token IS NULL
                    AND c.session_key = ('desktop:voice-work:' || c.id)
                    AND c.status IN ('blocked', 'ready') AND r.status IN ('expired', 'failed', 'cancelled')
                    AND NOT EXISTS (SELECT 1 FROM card_commands cancel WHERE cancel.card_id = c.id
                        AND cancel.kind = 'cancel' AND cancel.status IN ('stopping', 'status_unknown'))
                    AND NOT EXISTS (SELECT 1 FROM card_commands newer WHERE newer.card_id = c.id
                        AND newer.kind IN ('initial', 'steer') AND newer.claim_token IS NOT NULL AND newer.seq > q.seq)
                ORDER BY q.seq LIMIT ?''', (after_seq, *owner_args, max(1, min(limit, 100)))).fetchall()
            return [{
                'seq': row['seq'], 'commandId': row['command_id'], 'cardId': row['card_id'],
                'claimToken': row['claim_token'], 'runId': row['worker_run_id'] or row['command_id'],
                'revision': row['revision'], 'attempt': row['attempt_count'],
                'profile': row['assignee_profile'], 'expectedBotId': row['assignee_bot_id'],
                'sessionKey': row['session_key'],
            } for row in rows]

    def settle_reconciliation(self, candidate: dict, result: dict) -> Card | None:
        """Commit a verified result only if this exact orphan is still current."""
        if not isinstance(result, dict) or result.get('runId') != candidate['runId']:
            return None
        status = result.get('status')
        if status not in {'completed', 'aborted', 'error', 'paused'}:
            return None
        response = result.get('response') if status == 'completed' else None
        if status == 'completed' and (not isinstance(response, str) or not response.strip()):
            return None
        completed_run_id = result.get('completedRunId', candidate['runId'])
        if not isinstance(completed_run_id, str) or not 1 <= len(completed_run_id) <= 512:
            return None
        outcome = {'completed': 'done', 'aborted': 'cancelled', 'error': 'failed', 'paused': 'failed'}[status]
        card_status = {'completed': 'done', 'aborted': 'cancelled', 'error': 'blocked', 'paused': 'blocked'}[status]
        error = {'completed': None, 'aborted': 'The task was stopped.',
                 'error': 'The worker turn failed. Review the work conversation before continuing.',
                 'paused': 'The task goal is paused. Review the work conversation before continuing.'}[status]
        now = time.time()
        with self.store._lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            fresh = self.pending_reconciliations(after_seq=candidate['seq'] - 1, limit=1)
            if not fresh or fresh[0] != candidate:
                return None
            self.db.execute('UPDATE card_commands SET status = ?, worker_run_id = ?, updated_at = ? WHERE command_id = ?',
                            ('completed' if status == 'completed' else outcome, candidate['runId'], now, candidate['commandId']))
            # Held instructions stay held: observing completion is not a new
            # user instruction to execute deferred work.
            self.db.execute('''UPDATE cards SET status = ?, result = ?, error = ?, scheduled_at = NULL,
                revision = revision + 1, updated_at = ? WHERE id = ?''',
                (card_status, response[:_MAX_RESULT_CHARS] if response is not None else None, error, now, candidate['cardId']))
            self.db.execute('''UPDATE card_runs SET status = ?, worker_run_id = ?, completed_at = ?, error = ?
                WHERE card_id = ? AND claim_token = ? AND attempt = ?''',
                (outcome, candidate['runId'], now, error, candidate['cardId'], candidate['claimToken'], candidate['attempt']))
            self.store._record_event_locked(candidate['cardId'], 'run_finished', candidate['profile'], {
                'outcome': outcome, 'status': card_status, 'attempt': candidate['attempt'],
                'commandId': candidate['commandId'], 'workerRunId': candidate['runId'],
                'completedRunId': completed_run_id, 'reconciled': True,
            }, now=now)
            return self.store._get_card_locked(candidate['cardId'], with_notes=True)

    def _pending_stop_locked(self, card_id: str):
        self.store._require_card_locked(card_id)
        return self.db.execute(
            "SELECT * FROM card_commands WHERE card_id = ? AND kind = 'cancel' "
            "AND status IN ('stopping', 'status_unknown') ORDER BY seq LIMIT 1", (card_id,),
        ).fetchone()

    def response_intent(self, *, conversation_id: str, card_id: str, command_id: str,
                        expected_revision: int, payload: dict) -> tuple[bool, dict]:
        from flowly.live_voice.sessions import identity, integer

        identity(command_id, 'commandId')
        integer(expected_revision, 'expectedRevision')
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(encoded) > 8000:
            raise BoardError('request response is too large')
        fingerprint = hashlib.sha256(json.dumps({
            'conversation': conversation_id, 'card': card_id, 'revision': expected_revision,
            'kind': 'respond', 'payload': encoded,
        }, sort_keys=True).encode()).hexdigest()
        with self.store._lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            card = self._card_locked(conversation_id, card_id)
            existing = self.db.execute('SELECT * FROM card_commands WHERE command_id = ?', (command_id,)).fetchone()
            if existing is not None:
                if existing['fingerprint'] != fingerprint:
                    raise BoardError('command identity already belongs to a different request')
                return False, _wire(existing)
            if card.revision != expected_revision:
                raise BoardError('task revision has changed; refresh its requests before answering')
            if self._pending_stop_locked(card_id) or card.status in {'cancelled', 'archived'}:
                raise BoardError('task is stopping or closed')
            if self.db.execute('SELECT COUNT(*) FROM card_commands WHERE card_id = ?', (card_id,)).fetchone()[0] >= 200:
                raise BoardError('this task has reached its instruction limit; start a new task')
            now = time.time()
            self.db.execute(
                "INSERT INTO card_commands (command_id, card_id, kind, fingerprint, text, status, "
                "created_at, updated_at, response_owner) VALUES (?, ?, 'respond', ?, ?, 'delivering', ?, ?, ?)",
                (command_id, card_id, fingerprint, encoded, now, now, _PROCESS_ID),
            )
            self.db.execute('UPDATE cards SET revision = revision + 1, updated_at = ? WHERE id = ?', (now, card_id))
            self.store._record_event_locked(card_id, 'response_recorded', 'user', {
                'commandId': command_id, 'requestId': payload['requestId'],
            }, now=now)
            return True, _wire(self.db.execute('SELECT * FROM card_commands WHERE command_id = ?', (command_id,)).fetchone())

    def settle_response(self, command_id: str, status: str) -> dict:
        if status not in {'applied', 'rejected', 'status_unknown'}:
            raise BoardError('invalid response outcome')
        with self.store._lock, self.db:
            row = self.db.execute("SELECT * FROM card_commands WHERE command_id = ? AND kind = 'respond'", (command_id,)).fetchone()
            if row is None:
                raise BoardError('response command not found')
            self.store._require_card_locked(row['card_id'])
            if row['status'] == 'delivering' and row['response_owner'] == _PROCESS_ID:
                now = time.time()
                self.db.execute('UPDATE card_commands SET status = ?, updated_at = ? WHERE command_id = ?', (status, now, command_id))
                self.store._record_event_locked(row['card_id'], 'response_updated', 'worker', {
                    'commandId': command_id, 'status': status,
                }, now=now)
            return _wire(self.db.execute('SELECT * FROM card_commands WHERE command_id = ?', (command_id,)).fetchone())

    def cancel(self, *, conversation_id: str, card_id: str, command_id: str, expected_revision: int) -> dict:
        from flowly.live_voice.sessions import identity, integer

        identity(command_id, 'commandId')
        integer(expected_revision, 'expectedRevision')
        fingerprint = hashlib.sha256(json.dumps({
            'conversation': conversation_id, 'card': card_id, 'revision': expected_revision, 'kind': 'cancel',
        }, sort_keys=True).encode()).hexdigest()
        with self.store._lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            card = self._card_locked(conversation_id, card_id)
            old = self.db.execute('SELECT * FROM card_commands WHERE command_id = ?', (command_id,)).fetchone()
            if old is not None:
                if old['fingerprint'] != fingerprint:
                    raise BoardError('command identity already belongs to a different request')
                return _wire(old)
            if card.revision != expected_revision:
                raise BoardError('task revision has changed; refresh the task before cancelling')
            pending = self._pending_stop_locked(card_id)
            if pending is not None:
                raise BoardError('task cancellation is already being verified')
            if card.status == 'archived':
                raise BoardError('archived task cannot be cancelled')
            last = self.db.execute(
                "SELECT * FROM card_commands WHERE card_id = ? AND kind IN ('initial', 'steer') "
                "AND claim_token IS NOT NULL ORDER BY seq DESC LIMIT 1", (card_id,),
            ).fetchone()
            now = time.time()
            # Only a task that has never been claimed can be stopped locally.
            # Otherwise the worker and its standing goal must confirm stop.
            status = 'stopped' if card.attempt_count == 0 and not card.claim_token else 'stopping'
            payload = json.dumps({'runId': (last['worker_run_id'] or last['command_id']) if last else None})
            self.db.execute(
                "INSERT INTO card_commands (command_id, card_id, kind, fingerprint, text, status, "
                "created_at, updated_at) VALUES (?, ?, 'cancel', ?, ?, ?, ?, ?)",
                (command_id, card_id, fingerprint, payload, status, now, now),
            )
            self.db.execute(
                "UPDATE card_commands SET status = 'cancelled', updated_at = ? "
                "WHERE card_id = ? AND kind IN ('initial', 'steer') AND status IN ('queued_for_next_turn', 'held')",
                (now, card_id),
            )
            self.db.execute(
                'UPDATE cards SET status = ?, revision = revision + 1, updated_at = ? WHERE id = ?',
                ('cancelled' if status == 'stopped' else card.status, now, card_id),
            )
            self.store._record_event_locked(card_id, 'cancellation_requested', 'user', {
                'commandId': command_id, 'status': status,
            }, now=now)
            return _wire(self.db.execute('SELECT * FROM card_commands WHERE command_id = ?', (command_id,)).fetchone())

    def pending_stops(self) -> list[dict]:
        visible, owner_args = self.store._visibility_sql('c')
        with self.store._lock:
            return [_wire(r) for r in self.db.execute(
                "SELECT q.* FROM card_commands q JOIN cards c ON c.id = q.card_id "
                f"WHERE q.kind = 'cancel' AND q.status IN ('stopping', 'status_unknown') AND ({visible}) "
                "ORDER BY q.updated_at, q.seq LIMIT 5", owner_args,
            )]

    def bind_stop_goal(self, command_id: str, binding: dict) -> dict:
        """Persist the first observed goal before any remote stop mutation.

        A retry may update its revision precondition, but cannot adopt a new
        goal generation. An explicit no-goal binding is equally authoritative.
        """
        goal_id, status = binding.get('goalId'), binding.get('statusAtRequest')
        if (goal_id is not None and (not isinstance(goal_id, str) or not goal_id or len(goal_id) > 512)):
            raise BoardError('invalid cancellation goal binding')
        if status not in {None, 'active', 'paused', 'done', 'cleared'} or ((goal_id is None) != (status is None)):
            raise BoardError('invalid cancellation goal binding')
        binding = {'goalId': goal_id, 'statusAtRequest': status}
        with self.store._lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                "SELECT * FROM card_commands WHERE command_id = ? AND kind = 'cancel' "
                "AND status IN ('stopping', 'status_unknown')", (command_id,),
            ).fetchone()
            if row is None:
                raise BoardError('cancellation intent is no longer active')
            self.store._require_card_locked(row['card_id'])
            payload = json.loads(row['text'])
            if 'goalBinding' in payload:
                return payload['goalBinding']
            payload['goalBinding'] = binding
            self.db.execute('UPDATE card_commands SET text = ?, updated_at = ? WHERE command_id = ?',
                            (json.dumps(payload), time.time(), command_id))
            return binding

    def settle_stop(self, command_id: str, *, status: str, worker_status: str | None = None) -> None:
        if status not in {'stopping', 'status_unknown', 'stopped'}:
            raise BoardError('invalid cancellation outcome')
        with self.store._lock, self.db:
            row = self.db.execute(
                "SELECT * FROM card_commands WHERE command_id = ? AND kind = 'cancel' "
                "AND status IN ('stopping', 'status_unknown')", (command_id,),
            ).fetchone()
            if row is None:
                return
            self.store._require_card_locked(row['card_id'])
            card = self.store._get_card_locked(row['card_id'])
            # The worker turn owns its live claim. Its terminal handler must
            # settle before cancellation releases the task for a new command.
            if status == 'stopped' and card.claim_token:
                status = 'stopping'
            if status == 'stopped':
                requested_run = json.loads(row['text']).get('runId')
                settled = self.db.execute(
                    "SELECT r.status FROM card_runs r JOIN card_commands c "
                    "ON c.card_id = r.card_id AND c.claim_token = r.claim_token "
                    "WHERE r.card_id = ? AND r.worker_run_id = ? AND c.worker_run_id = ? "
                    "AND c.kind IN ('initial', 'steer') "
                    "AND c.status IN ('completed', 'failed', 'cancelled') "
                    "AND r.status IN ('done', 'failed', 'cancelled') ORDER BY r.attempt DESC LIMIT 1",
                    (card.id, requested_run, requested_run),
                ).fetchone()
                if settled is not None:
                    # The task observer may have followed several goal turns.
                    # Its verified outcome outranks the initial chat receipt;
                    # an uncertain/expired claim is deliberately excluded.
                    worker_status = {'done': 'completed', 'failed': 'error', 'cancelled': 'aborted'}[settled['status']]
            if row['status'] == status:
                self.db.execute('UPDATE card_commands SET updated_at = ? WHERE command_id = ?', (time.time(), command_id))
                return
            now = time.time()
            self.db.execute('UPDATE card_commands SET status = ?, updated_at = ? WHERE command_id = ?',
                            (status, now, command_id))
            if status == 'stopped':
                next_status = {'completed': 'done', 'error': 'blocked', 'aborted': 'cancelled'}.get(worker_status)
                if next_status is None:
                    raise BoardError('a verified terminal worker outcome is required')
                self.db.execute(
                    'UPDATE cards SET status = ?, revision = revision + 1, updated_at = ? WHERE id = ?',
                    (next_status, now, card.id),
                )
                # A verified stop also resolves an orphaned attempt's ambiguity.
                self.db.execute(
                    "UPDATE card_commands SET status = ?, updated_at = ? "
                    "WHERE card_id = ? AND kind IN ('initial', 'steer') AND status = 'status_unknown'",
                    ({'completed': 'completed', 'error': 'failed', 'aborted': 'cancelled'}[worker_status], now, card.id),
                )
            self.store._record_event_locked(card.id, 'cancellation_updated', 'worker', {
                'commandId': command_id, 'status': status, 'workerStatus': worker_status,
            }, now=now)
