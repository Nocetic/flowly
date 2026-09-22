"""Orphaned voice work is reconciled from durable evidence, never replayed."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
from flowly.board.orchestrator import BoardOrchestrator
from flowly.board.store import BoardStore
from flowly.goals.models import GoalState, GoalStatus
from flowly.goals.store import GoalStore
from flowly.profile_host import ProfileHost, ProfileHostError
from flowly.session.commands import ChatCommandStore
from flowly.session.manager import Session, SessionManager
from tests import test_voice_board

board = test_voice_board.board
dispatch = test_voice_board.dispatch


def orphan(board, *, acknowledged=True):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claim = store.claim_card(card.id, worker=card.assignee_profile)
    command = store.voice_commands.claimed(card.id, claim.claim_token)
    if acknowledged:
        store.set_worker_run_id(card.id, claim.claim_token, command['commandId'])
    store.recover_expired_claims(now=claim.lease_expires_at + 1)
    return card, command


def worker_evidence(card, command, *, goal=False, status='completed', response='Verified report', replace=False):
    profile = profiles.describe_profile(card.assignee_profile)
    commands = ChatCommandStore(profile.path / 'sessions' / 'chat_commands.sqlite3')
    manager = SessionManager(profile.path / 'workspace')
    manager.sessions_dir = profile.path / 'sessions'
    session = Session(card.session_key)
    run_id = command['commandId']
    commands.accept(card.session_key, run_id, {'message': command['text']})
    commands.begin_execution(card.session_key, run_id, {'available': True, 'goal': None})
    goals = GoalStore(profile.path)
    state = goals.save(GoalState(session_key=card.session_key, goal='Report', created_by_run_id=run_id)) if goal else None
    def observation():
        return {'available': True, 'goal': state.to_public_dict() if state else None}
    commands.finish_execution(card.session_key, run_id, observation(), 'completed' if goal else status)
    session.add_message('assistant', 'Working on the report' if goal else response, run_id=run_id)
    if goal and status in {'completed', 'aborted'}:
        final_id = 'final-run'
        commands.accept(card.session_key, final_id, {'turnOrigin': 'goal', 'goalId': state.goal_id})
        commands.begin_execution(card.session_key, final_id, observation())
        state.status = GoalStatus.DONE if status == 'completed' else GoalStatus.CLEARED
        state.last_run_id = final_id
        state = goals.save(state)
        commands.finish_execution(card.session_key, final_id, observation(), status)
        session.add_message('assistant', response, run_id=final_id, aborted=status == 'aborted')
    if state and status == 'paused':
        state.status = GoalStatus.PAUSED
        state = goals.save(state)
    manager.save(session)
    if replace:
        goals.save(GoalState(session_key=card.session_key, goal='An unrelated new goal'))
    commands.close()
    return profile, manager, goals


@pytest.mark.asyncio
@pytest.mark.parametrize('goal,replace,acknowledged', [(False, False, True), (True, False, True), (True, True, True), (True, True, False)])
async def test_restarted_host_recovers_real_worker_evidence_without_starting_a_runtime(board, goal, replace, acknowledged):
    store, _ = board
    card, command = orphan(board, acknowledged=acknowledged)
    worker_evidence(card, command, goal=goal, replace=replace)
    host = ProfileHost()
    host._ensure_runtime = AsyncMock(side_effect=AssertionError('Recovery must not start a runtime'))
    host._target_rpc = AsyncMock(side_effect=AssertionError('Recovery must not submit any RPC'))
    reopened = BoardStore(store.db_path)
    spawn = AsyncMock(side_effect=AssertionError('Recovery must not execute work'))
    finished = AsyncMock()
    orchestrator = BoardOrchestrator(reopened, spawn, voice_reconcile=host.reconcile_task, on_finished=finished)
    try:
        assert await orchestrator.dispatch_once() == 0
        await asyncio.gather(*list(orchestrator._recovery_tasks.values()))
        recovered = reopened.get_card(card.id)
        assert recovered.status == 'done'
        assert recovered.result == 'Verified report'
        assert recovered.attempt_count == 1
        assert recovered.session_key == card.session_key
        assert reopened.voice_commands.list(card.voice_conversation_id, card.id)[0]['status'] == 'completed'
        assert reopened.get_runs(card.id)[0]['status'] == 'done'
        snapshot = reopened.voice_events(card.voice_conversation_id)
        assert snapshot['cards'][0]['completionEvent']['attempt'] == 1
        await orchestrator.dispatch_once()
        assert not orchestrator._recovery_tasks
        assert finished.await_count == 1
        spawn.assert_not_awaited()
        host._ensure_runtime.assert_not_awaited()
        host._target_rpc.assert_not_awaited()
    finally:
        await orchestrator.stop_dispatcher()
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,expected', [('active', 'blocked'), ('paused', 'blocked'), ('aborted', 'cancelled')])
async def test_goal_acknowledgement_is_never_a_completed_task(board, status, expected):
    store, orchestrator = board
    card, command = orphan(board)
    worker_evidence(card, command, goal=True, status=status)
    orchestrator._voice_reconcile = ProfileHost().reconcile_task
    await orchestrator.dispatch_once()
    await asyncio.gather(*list(orchestrator._recovery_tasks.values()))
    assert store.get_card(card.id).status == expected
    assert store.get_card(card.id).result != 'Working on the report'
    commands = store.voice_commands.list(card.voice_conversation_id, card.id)
    assert commands[0]['status'] == {'active': 'status_unknown', 'paused': 'failed', 'aborted': 'cancelled'}[status]


@pytest.mark.asyncio
async def test_recovery_does_not_resume_held_followup_instructions(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claim = store.claim_card(card.id, worker=card.assignee_profile)
    command = store.voice_commands.claimed(card.id, claim.claim_token)
    store.voice_commands.enqueue(conversation_id=card.voice_conversation_id, card_id=card.id,
                                 command_id='later', text='Also publish it', expected_revision=claim.revision)
    store.recover_expired_claims(now=claim.lease_expires_at + 1)
    worker_evidence(card, command)
    orchestrator._voice_reconcile = ProfileHost().reconcile_task
    orchestrator._spawn = AsyncMock()
    await orchestrator.dispatch_once()
    await asyncio.gather(*list(orchestrator._recovery_tasks.values()))
    await orchestrator.dispatch_once()
    assert [c['status'] for c in store.voice_commands.list(card.voice_conversation_id, card.id)] == ['completed', 'held']
    orchestrator._spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_recovery_cannot_overwrite_a_changed_card_or_duplicate_completion(board):
    store, orchestrator = board
    card, command = orphan(board)
    worker_evidence(card, command)
    candidate = store.voice_commands.pending_reconciliations()[0]
    result = await ProfileHost().reconcile_task(profile=candidate['profile'], task_id=card.id,
                                              run_id=candidate['runId'], expected_bot_id=card.assignee_bot_id)
    store.set_status(card.id, 'review')
    assert store.voice_commands.settle_reconciliation(candidate, result) is None
    assert store.get_card(card.id).status == 'review'
    assert not [e for e in store.get_events(card.id) if e['kind'] == 'run_finished']


@pytest.mark.asyncio
async def test_missing_or_mismatched_evidence_stays_unknown(board):
    store, orchestrator = board
    card, command = orphan(board)
    host = ProfileHost()
    result = await host.reconcile_task(profile=card.assignee_profile, task_id=card.id,
                                       run_id=command['commandId'], expected_bot_id=card.assignee_bot_id)
    assert result['status'] == 'status_unknown'
    assert not (profiles.describe_profile(card.assignee_profile).path / 'sessions' / 'chat_commands.sqlite3').exists()
    with pytest.raises(ProfileHostError) as error:
        await host.reconcile_task(profile=card.assignee_profile, task_id=card.id,
                                  run_id=command['commandId'], expected_bot_id='different-agent')
    assert error.value.code == 'TASK_TARGET_CHANGED'
    assert store.get_card(card.id).status == 'blocked'


@pytest.mark.asyncio
async def test_pending_cancel_wins_over_a_recovery_observation(board):
    store, _ = board
    card, command = orphan(board)
    worker_evidence(card, command)
    candidate = store.voice_commands.pending_reconciliations()[0]
    result = await ProfileHost().reconcile_task(profile=card.assignee_profile, task_id=card.id,
                                              run_id=command['commandId'], expected_bot_id=card.assignee_bot_id)
    store.voice_commands.cancel(conversation_id=card.voice_conversation_id, card_id=card.id,
                                command_id='cancel-1', expected_revision=store.get_card(card.id).revision)
    assert store.voice_commands.settle_reconciliation(candidate, result) is None
    assert store.voice_commands.pending_reconciliations() == []


@pytest.mark.asyncio
async def test_only_one_process_can_settle_the_same_orphan(board):
    store, _ = board
    card, command = orphan(board)
    worker_evidence(card, command)
    candidate = store.voice_commands.pending_reconciliations()[0]
    result = await ProfileHost().reconcile_task(profile=card.assignee_profile, task_id=card.id,
                                              run_id=command['commandId'], expected_bot_id=card.assignee_bot_id)
    other = BoardStore(store.db_path)
    try:
        outcomes = await asyncio.gather(
            asyncio.to_thread(store.voice_commands.settle_reconciliation, candidate, result),
            asyncio.to_thread(other.voice_commands.settle_reconciliation, candidate, result),
        )
        assert sum(outcome is not None for outcome in outcomes) == 1
        assert len([event for event in store.get_events(card.id) if event['kind'] == 'run_finished']) == 1
    finally:
        other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['corrupt', 'hidden', 'aborted', 'failed', 'empty', 'different-run', 'tool-call', 'withdrawn'])
async def test_unusable_history_never_becomes_a_successful_handoff(board, invalid):
    import json

    from flowly.session.archive import EVENT_ID_KEY, transition_record

    store, _ = board
    card, command = orphan(board)
    profile, sessions, _ = worker_evidence(card, command)
    path = sessions._get_full_path(card.session_key)
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    reply = next(row for row in rows if row.get('role') == 'assistant')
    if invalid == 'corrupt':
        path.write_text(path.read_text() + '\n{corrupt record}\n')
    elif invalid == 'withdrawn':
        rows.append(transition_record([reply[EVENT_ID_KEY]], 'withdrawn', timestamp='2026-09-06T00:00:00'))
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    else:
        reply.update({
            'hidden': {'_display_hidden': True}, 'aborted': {'aborted': True},
            'failed': {'failed': True}, 'empty': {'content': '  '},
            'different-run': {'run_id': 'unrelated'},
            'tool-call': {'tool_calls': [{'name': 'still-working'}]},
        }[invalid])
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    result = await ProfileHost().reconcile_task(profile=card.assignee_profile, task_id=card.id,
                                              run_id=command['commandId'], expected_bot_id=card.assignee_bot_id)
    assert result['status'] == 'status_unknown'
    assert store.get_card(card.id).status == 'blocked'
    # No fallback through a repaired canonical session may resurrect a
    # withdrawn, hidden or corrupt display result.
    assert SessionManager.read_run_result(profile.path, 'another-session', command['commandId']) is None


@pytest.mark.asyncio
async def test_a_completed_goal_requires_its_final_runs_own_goal_binding(board):
    import json
    import sqlite3

    card, command = orphan(board)
    profile, _, _ = worker_evidence(card, command, goal=True)
    with sqlite3.connect(profile.path / 'sessions' / 'chat_commands.sqlite3') as db:
        db.execute('UPDATE chat_command_goals SET binding_json = ? WHERE run_id = ?',
                   (json.dumps({'version': 1, 'state': 'none'}), 'final-run'))
    result = await ProfileHost().reconcile_task(profile=card.assignee_profile, task_id=card.id,
                                              run_id=command['commandId'], expected_bot_id=card.assignee_bot_id)
    assert result['status'] == 'status_unknown'


@pytest.mark.asyncio
async def test_recovery_rechecks_profile_identity_after_reading(board, monkeypatch):
    from dataclasses import replace

    from flowly.live_voice import recovery

    card, command = orphan(board)
    worker_evidence(card, command)
    actual = profiles.describe_profile
    changed = False

    def describe(name):
        current = actual(name)
        return replace(current, bot_id='replacement') if changed else current

    def replaced_during_read(*_args):
        nonlocal changed
        changed = True
        return {'runId': command['commandId'], 'status': 'completed', 'response': 'Old result'}

    monkeypatch.setattr(profiles, 'describe_profile', describe)
    monkeypatch.setattr(recovery, 'read_task_result', replaced_during_read)
    with pytest.raises(ProfileHostError) as error:
        await ProfileHost().reconcile_task(profile=card.assignee_profile, task_id=card.id,
                                          run_id=command['commandId'], expected_bot_id=card.assignee_bot_id)
    assert error.value.code == 'TASK_TARGET_CHANGED'


@pytest.mark.asyncio
async def test_many_unresolved_cards_are_polled_fairly_without_new_execution(board):
    store, orchestrator = board
    cards = []
    for index in range(7):
        card = dispatch(orchestrator, command_id=f'work-{index}')
        claim = store.claim_card(card.id, worker=card.assignee_profile)
        store.recover_expired_claims(now=claim.lease_expires_at + 1)
        cards.append(card)
    seen = []

    async def reconcile(**params):
        seen.append(params['task_id'])
        return {'runId': params['run_id'], 'status': 'status_unknown'}

    orchestrator._voice_reconcile = reconcile
    orchestrator._spawn = AsyncMock()
    for _ in range(2):
        assert await orchestrator.dispatch_once() == 0
        assert len(orchestrator._recovery_tasks) <= orchestrator.MAX_PARALLEL
        await asyncio.gather(*list(orchestrator._recovery_tasks.values()))
    assert seen == [card.id for card in cards]
    orchestrator._spawn.assert_not_awaited()
    assert all(store.get_card(card.id).status == 'blocked' for card in cards)


@pytest.mark.asyncio
async def test_shutdown_cancels_the_observer_without_settling_or_stopping_work(board):
    store, orchestrator = board
    card, command = orphan(board)
    observing = asyncio.Event()

    async def reconcile(**_params):
        observing.set()
        await asyncio.Event().wait()

    orchestrator._voice_reconcile = reconcile
    await orchestrator.dispatch_once()
    await observing.wait()
    await orchestrator.stop_dispatcher()
    assert not orchestrator._recovery_tasks
    assert store.get_card(card.id).status == 'blocked'
    assert store.voice_commands.list(card.voice_conversation_id, card.id)[0]['status'] == 'status_unknown'
