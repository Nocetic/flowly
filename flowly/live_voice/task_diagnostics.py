"""Content-free projections of committed Board lifecycle evidence.

Event IDs are stable across retries; downstream consumers must deduplicate them.
Worker acceptance is not the worker's first token. Wall-clock durations that
cannot be established are absent, never zero-filled.
"""
from __future__ import annotations
import hashlib
import math
import re
from flowly.live_voice.telemetry_sink import emit_voice_diagnostic


def observe_voice_task(db, card_id: str, stage: str, *, expected_attempt: int | None = None) -> None:
    try:
        if stage not in {'accepted', 'worker_accepted', 'finished'}:
            return
        card = db.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
        if not card or card['execution_mode'] != 'voice':
            return
        attempt = 0 if stage == 'accepted' else expected_attempt if expected_attempt is not None else card['attempt_count']
        run = None if not attempt else db.execute(
            'SELECT * FROM card_runs WHERE card_id = ? AND attempt = ?', (card_id, attempt),
        ).fetchone()
        if stage != 'accepted' and not run:
            return
        # An open-only chat has no runnable instruction and is not task acceptance.
        command = db.execute(
            'SELECT * FROM card_commands WHERE card_id = ? ' +
            ("AND kind = 'initial' " if stage == 'accepted' else 'AND claim_token = ? ') +
            'ORDER BY seq LIMIT 1',
            (card_id,) if stage == 'accepted' else (card_id, run['claim_token']),
        ).fetchone()
        if not command:
            return
        applied = None if stage == 'accepted' else db.execute(
            "SELECT created_at FROM card_events WHERE card_id = ? AND kind = 'command_applied' "
            "AND json_extract(payload, '$.commandId') = ? ORDER BY id LIMIT 1",
            (card_id, command['command_id']),
        ).fetchone()
        started = applied['created_at'] if applied else None
        observed = command['created_at'] if stage == 'accepted' else started if stage == 'worker_accepted' else run['completed_at']
        if observed is None:
            return
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        row = {'event': 'live_voice_task_lifecycle', 'component': 'core', 'version': 1,
               'eventId': digest(f'{card_id}:{attempt}:{stage}'), 'taskRef': digest(card_id),
               'commandRef': digest(command['command_id']), 'stage': stage, 'attempt': attempt,
               'observedAtMs': round(observed * 1000), 'correlation': 'host_verified',
               'outcome': 'unknown' if command['status'] == 'status_unknown' else
                   run['status'] if stage == 'finished' else 'ok'}
        operation = card['idempotency_key']
        if isinstance(operation, str) and re.fullmatch(r'(?:[a-f0-9]{64}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})', operation, re.I):
            row['originOperationId'] = operation.lower()
        for name, begin, end in (
            ('acceptToWorkerMs', command['created_at'], started),
            ('workerToFinishMs', started, run['completed_at'] if run else None),
            ('acceptToFinishMs', command['created_at'], run['completed_at'] if run else None),
        ):
            if begin is not None and end is not None:
                ms = (end - begin) * 1000
                if math.isfinite(ms) and ms >= 0:
                    row[name] = min(604800000, round(ms))
                    if ms > 604800000:
                        row['durationCapped'] = True
                else:
                    row['clockInvalid'] = True
        if row['outcome'] not in {'ok', 'done', 'failed', 'cancelled', 'review', 'blocked', 'unknown'}:
            row['outcome'] = 'unknown'
        emit_voice_diagnostic(row)
    except Exception:
        pass  # Neither reads nor logging may affect committed work.
