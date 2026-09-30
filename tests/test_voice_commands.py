import asyncio

import pytest

from flowly.board.store import BoardError, BoardStore
from tests import test_voice_board as voice_board

board = voice_board.board
dispatch = voice_board.dispatch


def steer(store, card, **changes):
    return store.voice_commands.enqueue(**{
        'conversation_id': card.voice_conversation_id, 'card_id': card.id,
        'command_id': 'steer-1', 'expected_revision': card.revision,
        'text': 'Include the latest figures.', **changes,
    })


def test_initial_command_and_continuation_survive_restart(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    receipt = steer(store, card)
    assert receipt['status'] == 'queued_for_next_turn'
    reopened = BoardStore(store.db_path)
    commands = reopened.voice_commands.list(card.voice_conversation_id, card.id)
    assert [c['kind'] for c in commands] == ['initial', 'steer']
    assert commands[1]['commandId'] == 'steer-1'
    reopened.close()
    assert steer(store, card) == receipt  # A lost ACK is safe despite the old revision.
    with pytest.raises(BoardError, match='different request'):
        steer(store, card, text='Different work')


def test_stale_revision_and_other_conversation_never_queue(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    with pytest.raises(BoardError, match='revision'):
        steer(store, card, expected_revision=99)
    with pytest.raises(BoardError, match='conversation'):
        steer(store, card, conversation_id='other')
    assert len(store.voice_commands.list(card.voice_conversation_id, card.id)) == 1


@pytest.mark.asyncio
async def test_running_work_gets_one_next_turn_in_the_same_session(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def spawn(prompt, **kwargs):
        calls.append((prompt, kwargs))
        store.set_worker_run_id(card.id, kwargs['claim_token'], f'worker-{len(calls)}')
        started.set()
        await release.wait()
        return 'Finished this turn'

    orchestrator._spawn = spawn
    running = asyncio.create_task(orchestrator._execute(card.id))
    await asyncio.wait_for(started.wait(), 2)
    receipt = steer(store, store.get_card(card.id))
    assert receipt['status'] == 'queued_for_next_turn'
    assert len(calls) == 1
    release.set()
    await running
    assert store.get_card(card.id).status == 'ready'
    await orchestrator._execute(card.id)
    assert store.get_card(card.id).status == 'done'
    assert len(calls) == 2
    assert calls[1][0] == 'Include the latest figures.'
    assert calls[0][1]['task_id'] == calls[1][1]['task_id'] == card.id
    assert calls[0][1]['command_id'] != calls[1][1]['command_id']
    commands = store.voice_commands.list(card.voice_conversation_id, card.id)
    assert [c['status'] for c in commands] == ['completed', 'completed']
    assert [c['workerRunId'] for c in commands] == ['worker-1', 'worker-2']
    assert len([e for e in store.get_events(card.id) if e['kind'] == 'command_applied']) == 2


def test_expired_voice_claim_never_replays_uncertain_work(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claimed = store.claim_card(card.id, worker='creative')
    steer(store, claimed)
    store.recover_expired_claims(now=claimed.lease_expires_at + 1)
    assert store.get_card(card.id).status == 'blocked'
    assert store.voice_commands.list(card.voice_conversation_id, card.id)[0]['status'] == 'status_unknown'
    with pytest.raises(BoardError, match='unverified'):
        steer(store, store.get_card(card.id), command_id='steer-2')
    store.set_status(card.id, 'ready')
    assert store.claim_card(card.id, worker='creative') is None


def test_completed_initial_is_not_replayed_by_generic_board_ready(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claim = store.claim_card(card.id, worker='creative')
    store.finish_claim(card.id, claim.claim_token, outcome='done', result='Done')
    store.set_status(card.id, 'ready')
    assert store.claim_card(card.id, worker='creative') is None
    steer(store, store.get_card(card.id))
    assert store.claim_card(card.id, worker='creative') is not None


def test_failed_turn_does_not_silently_run_queued_instructions(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claim = store.claim_card(card.id, worker='creative')
    steer(store, claim)
    store.finish_claim(card.id, claim.claim_token, outcome='failed', error='Provider failed')
    assert store.get_card(card.id).status == 'blocked'
    commands = store.voice_commands.list(card.voice_conversation_id, card.id)
    assert [c['status'] for c in commands] == ['failed', 'held']


def test_terminal_task_can_accept_explicit_continuation(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claim = store.claim_card(card.id, worker='creative')
    store.finish_claim(card.id, claim.claim_token, outcome='done', result='Done')
    steer(store, store.get_card(card.id))
    assert store.get_card(card.id).status == 'ready'


@pytest.mark.parametrize('outcome,worker_status,expected', [
    ('cancelled', 'completed', 'cancelled'),
    ('done', 'aborted', 'done'),
    ('failed', 'completed', 'blocked'),
])
def test_task_terminal_proof_wins_over_an_earlier_turn_receipt(board, outcome, worker_status, expected):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claimed = store.claim_card(card.id, worker='creative')
    store.set_worker_run_id(card.id, claimed.claim_token, 'initial-worker')
    cancel(store, store.get_card(card.id))
    store.finish_claim(card.id, claimed.claim_token, outcome=outcome, result='Task result')
    # The control poll can still read the initial reply's terminal receipt
    # after the standing goal has produced a different, verified task outcome.
    store.voice_commands.settle_stop('cancel-1', status='stopped', worker_status=worker_status)
    assert store.get_card(card.id).status == expected
    assert store.get_card(card.id).result == 'Task result'


def test_cancel_goal_binding_is_durable_and_cannot_switch_to_a_replacement(board):
    import json

    store, orchestrator = board
    card = dispatch(orchestrator)
    claimed = store.claim_card(card.id, worker='creative')
    cancel(store, claimed)
    binding = {'goalId': 'first-goal', 'statusAtRequest': 'active'}
    assert store.voice_commands.bind_stop_goal('cancel-1', binding) == binding
    reopened = BoardStore(store.db_path)
    assert reopened.voice_commands.bind_stop_goal('cancel-1', {
        'goalId': 'replacement', 'statusAtRequest': 'active',
    }) == binding
    payload = json.loads(reopened.voice_commands.pending_stops()[0]['text'])
    assert payload['goalBinding'] == binding
    reopened.close()


def test_next_instruction_uses_the_live_claim_only_after_a_verified_turn(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claimed = store.claim_card(card.id, worker='creative')
    initial_id = f'voice:{card.id}:initial'
    steer(store, claimed)
    with pytest.raises(BoardError, match='not completed'):
        store.voice_commands.advance_boundary(card.id, claimed.claim_token, initial_id)
    store.set_worker_run_id(card.id, claimed.claim_token, 'worker-initial')
    command = store.voice_commands.advance_boundary(card.id, claimed.claim_token, initial_id)
    assert command['commandId'] == 'steer-1'
    assert command['status'] == 'delivering'
    assert store.voice_events(card.voice_conversation_id)['cards'][0]['latestInstruction'] == {
        'commandId': 'steer-1', 'status': 'delivering',
    }
    assert store.get_card(card.id).claim_token == claimed.claim_token
    assert store.get_card(card.id).status == 'in_progress'
    assert store.voice_commands.advance_boundary(card.id, claimed.claim_token, initial_id) is None
    store.finish_claim(card.id, claimed.claim_token, outcome='failed', uncertain=True)
    assert [c['status'] for c in store.voice_commands.list(card.voice_conversation_id, card.id)] == ['completed', 'status_unknown']


def test_unknown_goal_after_completed_turn_still_requires_reconciliation(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claimed = store.claim_card(card.id, worker='creative')
    store.set_worker_run_id(card.id, claimed.claim_token, 'initial')
    assert store.voice_commands.advance_boundary(card.id, claimed.claim_token, f'voice:{card.id}:initial') is None
    store.finish_claim(card.id, claimed.claim_token, outcome='failed', uncertain=True)
    assert store.voice_commands.list(card.voice_conversation_id, card.id)[0]['status'] == 'status_unknown'
    with pytest.raises(BoardError, match='unverified'):
        steer(store, store.get_card(card.id))


def cancel(store, card, **changes):
    return store.voice_commands.cancel(**{
        'conversation_id': card.voice_conversation_id, 'card_id': card.id,
        'command_id': 'cancel-1', 'expected_revision': card.revision, **changes,
    })


def test_cancelling_unstarted_work_is_atomic_and_prevents_dispatch(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    receipt = cancel(store, card)
    assert receipt['status'] == 'stopped'
    assert cancel(store, card) == receipt
    assert store.get_card(card.id).status == 'cancelled'
    assert store.claim_card(card.id, worker='creative') is None
    assert store.voice_commands.list(card.voice_conversation_id, card.id)[0]['status'] == 'cancelled'


def test_cancel_waits_for_terminal_and_holds_new_instructions(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claimed = store.claim_card(card.id, worker='creative')
    steer(store, claimed)
    command = cancel(store, store.get_card(card.id))
    assert command['status'] == 'stopping'
    with pytest.raises(BoardError, match='cancellation'):
        steer(store, store.get_card(card.id), command_id='steer-2')
    store.voice_commands.settle_stop(command['commandId'], status='stopped', worker_status='aborted')
    assert store.get_card(card.id).status == 'in_progress'
    assert store.voice_commands.pending_stops()
    store.finish_claim(card.id, claimed.claim_token, outcome='cancelled')
    store.voice_commands.settle_stop(command['commandId'], status='stopped', worker_status='aborted')
    assert store.get_card(card.id).status == 'cancelled'
    assert store.voice_commands.pending_stops() == []
    assert [c['status'] for c in store.voice_commands.list(card.voice_conversation_id, card.id)] == [
        'cancelled', 'cancelled', 'stopped',
    ]


def test_completion_wins_a_cancel_race_without_running_queued_work(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claimed = store.claim_card(card.id, worker='creative')
    steer(store, claimed)
    command = cancel(store, store.get_card(card.id))
    store.finish_claim(card.id, claimed.claim_token, outcome='done', result='Saved report')
    store.voice_commands.settle_stop(command['commandId'], status='stopped', worker_status='completed')
    final = store.get_card(card.id)
    assert final.status == 'done'
    assert final.result == 'Saved report'
    assert not store.list_dispatchable()


def test_pending_cancel_survives_restart_and_unknown_is_not_cancelled(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claimed = store.claim_card(card.id, worker='creative')
    command = cancel(store, claimed)
    reopened = BoardStore(store.db_path)
    assert reopened.voice_commands.pending_stops()[0]['commandId'] == command['commandId']
    reopened.voice_commands.settle_stop(command['commandId'], status='status_unknown')
    assert reopened.get_card(card.id).status == 'in_progress'
    reopened.close()


@pytest.mark.asyncio
async def test_cancellation_does_not_cancel_worker_waiter_before_confirmation(board):
    from flowly.profile_host_contract import ProfileHostError

    store, orchestrator = board
    card = dispatch(orchestrator)
    started, stop_received = asyncio.Event(), asyncio.Event()

    async def worker(_prompt, **kwargs):
        store.set_worker_run_id(card.id, kwargs['claim_token'], 'worker-1')
        started.set()
        await stop_received.wait()
        raise ProfileHostError('PROFILE_COLLABORATION_FAILED', 'Interrupted', terminal_state='aborted')

    async def control(**kwargs):
        assert kwargs['run_id'] == 'worker-1'
        if card.id in orchestrator._tasks:
            assert not orchestrator._tasks[card.id].cancelled()
        stop_received.set()
        return {'status': 'stopped', 'workerStatus': 'aborted'}

    orchestrator._spawn = worker
    orchestrator._voice_control = control
    running = asyncio.create_task(orchestrator._execute(card.id))
    await asyncio.wait_for(started.wait(), 2)
    command = cancel(store, store.get_card(card.id))
    await orchestrator._control_voice(command)
    await running
    # The first reply may arrive before the original waiter settles its claim.
    await orchestrator._control_voice(command)
    assert store.get_card(card.id).status == 'cancelled'
    assert store.voice_commands.pending_stops() == []


def test_two_connections_cannot_both_accept_a_stale_task_revision(board):
    from concurrent.futures import ThreadPoolExecutor

    store, orchestrator = board
    card = dispatch(orchestrator)
    second = BoardStore(store.db_path)

    def submit(connection, command_id):
        try:
            return steer(connection, card, command_id=command_id)
        except BoardError:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, connection, command) for connection, command in (
                (store, 'steer-1'), (second, 'steer-2'),
            )]
            assert sum(f.result() is not None for f in futures) == 1
        assert len(store.voice_commands.list(card.voice_conversation_id, card.id)) == 2
    finally:
        second.close()


def test_response_with_no_receipt_from_previous_process_is_not_redelivered(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    params = dict(conversation_id=card.voice_conversation_id, card_id=card.id, command_id='response-1',
                  expected_revision=card.revision, payload={'requestId': 'question-1', 'answer': 'Sales'})
    created, _ = store.voice_commands.response_intent(**params)
    assert created
    with store._conn:
        store._conn.execute("UPDATE card_commands SET response_owner = 'previous-process' WHERE kind = 'respond'")
    created, receipt = store.voice_commands.response_intent(**params)
    assert not created
    assert receipt['status'] == 'status_unknown'
    # A callback in a replacement process cannot invent an acknowledgement.
    assert store.voice_commands.settle_response('response-1', 'applied')['status'] == 'status_unknown'
