"""Regression coverage for privacy and lifecycle projection (no provider calls)."""
import hashlib
import json
import sqlite3
from unittest.mock import patch
from flowly.live_voice.task_diagnostics import observe_voice_task


def fixture():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE cards (id TEXT, execution_mode TEXT, attempt_count INTEGER, idempotency_key TEXT);
      CREATE TABLE card_runs (card_id TEXT, attempt INTEGER, claim_token TEXT, completed_at REAL, status TEXT);
      CREATE TABLE card_commands (card_id TEXT, seq INTEGER, kind TEXT, command_id TEXT, claim_token TEXT, created_at REAL, status TEXT);
      CREATE TABLE card_events (id INTEGER, card_id TEXT, kind TEXT, created_at REAL, payload TEXT);
    ''')
    db.execute('INSERT INTO cards VALUES (?, ?, ?, ?)', ('card-private', 'voice', 1, 'a' * 64))
    db.execute('INSERT INTO card_runs VALUES (?, ?, ?, ?, ?)', ('card-private', 1, 'secret-claim', 15, 'done'))
    db.execute('INSERT INTO card_commands VALUES (?, ?, ?, ?, ?, ?, ?)', ('card-private', 1, 'initial', 'private-command', 'secret-claim', 10, 'completed'))
    db.execute('INSERT INTO card_events VALUES (?, ?, ?, ?, ?)', (1, 'card-private', 'command_applied', 12, json.dumps({'commandId': 'private-command', 'text': 'private speech'})))
    db.commit()
    return db


def test_committed_lifecycle_has_durations_without_content_or_claims():
    db = fixture()
    try:
        with patch('flowly.live_voice.task_diagnostics.logger') as log:
            for stage in ('accepted', 'worker_accepted', 'finished'):
                observe_voice_task(db, 'card-private', stage)
            rows = [json.loads(call.args[1]) for call in log.info.call_args_list]
            assert [r['stage'] for r in rows] == ['accepted', 'worker_accepted', 'finished']
            assert rows[-1]['acceptToWorkerMs'] == 2000
            assert rows[-1]['workerToFinishMs'] == 3000
            assert rows[-1]['acceptToFinishMs'] == 5000
            assert rows[-1]['taskRef'] == hashlib.sha256(b'card-private').hexdigest()
            assert rows[-1]['originOperationId'] == 'a' * 64
            assert 'private' not in json.dumps(rows)
            assert 'secret-claim' not in json.dumps(rows)
            observe_voice_task(db, 'card-private', 'finished')
            assert json.loads(log.info.call_args.args[1])['eventId'] == rows[-1]['eventId']
    finally:
        db.close()


def test_missing_start_is_unknown_not_zero_and_broken_sink_is_nonfatal():
    db = fixture()
    try:
        db.execute('DELETE FROM card_events')
        with patch('flowly.live_voice.task_diagnostics.logger') as log:
            observe_voice_task(db, 'card-private', 'finished')
            row = json.loads(log.info.call_args.args[1])
            assert 'workerToFinishMs' not in row
            assert 'acceptToWorkerMs' not in row
            log.info.side_effect = RuntimeError('sink offline')
            observe_voice_task(db, 'card-private', 'finished')
        db.execute("UPDATE cards SET execution_mode = 'normal'")
        with patch('flowly.live_voice.task_diagnostics.logger') as log:
            observe_voice_task(db, 'card-private', 'finished')
            log.info.assert_not_called()
    finally:
        db.close()


def test_reconciliation_pins_old_attempt_and_invalid_clock_is_not_zero():
    db = fixture()
    try:
        db.execute('UPDATE cards SET attempt_count = 2')
        db.execute('UPDATE card_runs SET completed_at = 9')
        with patch('flowly.live_voice.task_diagnostics.logger') as log:
            observe_voice_task(db, 'card-private', 'finished', expected_attempt=1)
            row = json.loads(log.info.call_args.args[1])
            assert row['attempt'] == 1
            assert row['clockInvalid'] is True
            assert 'workerToFinishMs' not in row
            assert 'acceptToFinishMs' not in row
    finally:
        db.close()
