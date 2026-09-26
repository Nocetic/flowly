"""A voice task owns the standing goal, not just its first chat response."""
import asyncio

import pytest

import flowly.profile as profiles
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError
from tests import test_profile_host, test_voice_board

profile_roots = test_profile_host.profile_roots
board = test_voice_board.board


class Worker:
    def __init__(self, host, goal=None):
        self.host = host
        self.goal = goal
        self.calls = []
        self.sent = asyncio.Event()
        self.observed = asyncio.Event()
        self.sent_once = False
        self.messages = []
        self.replayed = False
        self.historical = {}
        self.binding = ({'version': 1, 'state': 'goal', 'goalId': goal['goalId'], 'revision': goal['revision']}
                        if goal and goal['status'] in {'active', 'paused'} else {'version': 1, 'state': 'none'})

    async def rpc(self, profile, method, params, timeout):
        self.calls.append((method, params))
        assert params['sessionKey'] == 'desktop:voice-work:task-1'
        assert params['expectedBotId'] == profiles.ensure_profile_bot_id('default').bot_id
        if method == 'runtime.voice.reserve':
            return {'sessionKey': params['sessionKey'], 'reserved': True}
        if method == 'goal.get':
            if self.sent_once:
                self.observed.set()
            if params.get('goalId') and (not self.goal or self.goal['goalId'] != params['goalId']):
                return {'goal': self.historical.get(params['goalId'])}
            return {'goal': dict(self.goal) if self.goal else None}
        if method == 'chat.send':
            self.sent_once = True
            await self.host._handle_profile_event(profile, 'chat', {
                'sessionKey': params['sessionKey'], 'runId': 'initial', 'state': 'final',
                'message': {'content': 'I have started working.'},
            })
            self.sent.set()
            return {'runId': 'initial', 'status': 'completed' if self.replayed else 'accepted'}
        if method == 'chat.history':
            return {'messages': list(self.messages)}
        if method == 'chat.command':
            return {'runId': params['runId'], 'status': 'completed', 'goalBinding': self.binding}
        raise AssertionError(method)

    def start(self):
        self.host._target_rpc = self.rpc
        return asyncio.create_task(self.host.run_task(
            'default', task_id='task-1', prompt='Complete the report',
            idempotency_key='initial', interactive=True,
            expected_bot_id=profiles.ensure_profile_bot_id('default').bot_id,
        ))


def goal(status='active', revision=1, **extra):
    return {'goalId': 'goal-1', 'status': status, 'revision': revision, **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,old_status,expected', [
    ('none', None, {'status': 'stopped', 'workerStatus': 'completed'}),
    ('goal', 'done', {'status': 'stopped', 'workerStatus': 'completed'}),
    ('goal', 'cleared', {'status': 'stopped', 'workerStatus': 'aborted'}),
    ('goal', 'active', {'status': 'status_unknown'}),
    ('missing', None, {'status': 'status_unknown'}),
])
async def test_new_cancellation_never_adopts_a_later_goal(profile_roots, kind, old_status, expected):
    host = ProfileHost()
    calls = []
    binding = {'version': 1, 'state': kind, **({'goalId': 'original', 'revision': 1} if kind == 'goal' else {})}

    async def rpc(profile, method, params, timeout):
        calls.append(method)
        if method == 'chat.command':
            return {'runId': params['runId'], 'status': 'completed', **({'goalBinding': binding} if kind != 'missing' else {})}
        if method == 'goal.get':
            return {'goal': goal(old_status, 4, goalId='original', lastRunId='verified-final') if params.get('goalId') == 'original'
                    else goal(goalId='unrelated-later-goal')}
        if method == 'chat.inflight':
            return {'goal': goal(goalId='unrelated-later-goal'), 'inflight': {'runId': 'unrelated-later-run', 'goalRun': True}}
        raise AssertionError(f'Cancellation must not mutate the later goal: {method}')

    host._target_rpc = rpc
    result = await host.stop_task(profile='default', task_id='task-1', run_id='initial',
                                  expected_bot_id=profiles.ensure_profile_bot_id('default').bot_id)
    assert result == expected
    assert 'goal.stop' not in calls and 'chat.abort' not in calls


@pytest.mark.asyncio
async def test_running_command_can_cancel_the_goal_it_just_created(profile_roots):
    host = ProfileHost()
    current = goal(createdByRunId='initial')
    calls = []

    async def rpc(profile, method, params, timeout):
        nonlocal current
        calls.append(method)
        if method == 'chat.command':
            return {'runId': 'initial', 'status': 'running', 'goalBinding': {'version': 1, 'state': 'pending'}}
        if method == 'chat.abort':
            return {'ok': True}
        if method == 'goal.get':
            return {'goal': current}
        if method == 'goal.stop':
            assert params['expectedGoalId'] == 'goal-1' and params['expectedRevision'] == 1
            current = goal('cleared', 2, createdByRunId='initial')
            return {'goal': current}
        assert method == 'chat.inflight'
        return {'goal': current, 'inflight': {'runId': 'initial', 'goalRun': False}}

    host._target_rpc = rpc
    result = await host.stop_task(profile='default', task_id='task-1', run_id='initial',
                                  expected_bot_id=profiles.ensure_profile_bot_id('default').bot_id)
    assert result == {'status': 'stopping'}
    assert calls.count('goal.stop') == 1


@pytest.mark.asyncio
async def test_active_goal_keeps_task_and_audit_open_until_verified_final(profile_roots, monkeypatch):
    host = ProfileHost()
    monkeypatch.setattr(host, 'TASK_GOAL_POLL_SECONDS', .001, raising=False)
    worker = Worker(host, goal())
    task = worker.start()
    await asyncio.wait_for(worker.observed.wait(), 1)
    assert not task.done()
    assert host.task_audit('default', 'initial')['endedAt'] is None
    worker.messages = [
        {'role': 'assistant', 'runId': 'initial', 'content': 'I have started working.'},
        {'role': 'assistant', 'runId': 'verified-run', 'content': 'The report is complete.'},
        {'role': 'assistant', 'runId': 'unrelated', 'content': 'An unrelated reply'},
    ]
    worker.goal = goal('done', 4, lastRunId='verified-run')
    result = await asyncio.wait_for(task, 1)
    assert result['runId'] == 'initial'  # Original command acknowledgement stays stable.
    assert result['completedRunId'] == 'verified-run'
    assert result['response'] == 'The report is complete.'
    assert host.task_audit('default', 'initial')['outcome'] == 'ok'
    assert sum(m == 'chat.send' for m, _ in worker.calls) == 1
    assert not host._interactive_task_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize('snapshot,code', [
    (goal('paused', 2, lastVerdict='needs_input'), 'TASK_GOAL_PAUSED'),
    (goal('cleared', 2), 'TASK_GOAL_CANCELLED'),
    (goal('active', 2, goalId='replacement'), 'TASK_RESULT_UNKNOWN'),
    (None, 'TASK_RESULT_UNKNOWN'),
    (goal('done', 2), 'TASK_RESULT_UNKNOWN'),
    (goal('done', 2, lastRunId='missing'), 'TASK_RESULT_UNKNOWN'),
])
async def test_unfinished_or_unverified_goal_is_never_success(profile_roots, monkeypatch, snapshot, code):
    host = ProfileHost()
    monkeypatch.setattr(host, 'TASK_GOAL_POLL_SECONDS', .001, raising=False)
    worker = Worker(host, goal())
    task = worker.start()
    await asyncio.wait_for(worker.observed.wait(), 1)
    worker.goal = snapshot
    with pytest.raises(ProfileHostError) as error:
        await asyncio.wait_for(task, 1)
    assert error.value.code == code
    if code == 'TASK_GOAL_CANCELLED':
        assert error.value.terminal_state == 'aborted'
    assert not host._interactive_task_sessions
    assert not any(m in {'chat.abort', 'goal.stop', 'goal.resume'} for m, _ in worker.calls)


@pytest.mark.asyncio
async def test_old_done_goal_cannot_replace_a_new_instruction_response(profile_roots):
    host = ProfileHost()
    worker = Worker(host, goal('done', 8, lastRunId='older-result'))
    result = await asyncio.wait_for(worker.start(), 1)
    assert result['response'] == 'I have started working.'
    assert not any(m == 'chat.history' for m, _ in worker.calls)


@pytest.mark.asyncio
async def test_replayed_first_ack_is_not_a_completed_goal_result(profile_roots):
    host = ProfileHost()
    worker = Worker(host, goal('done', 8, lastRunId='later-result'))
    worker.binding = None  # A legacy receipt has no causal goal proof.
    worker.replayed = True
    with pytest.raises(ProfileHostError) as error:
        await asyncio.wait_for(worker.start(), 1)
    assert error.value.code == 'TASK_RESULT_UNKNOWN'
    assert sum(m == 'chat.send' for m, _ in worker.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_goal_is_not_overwritten_by_its_completed_first_turn(profile_roots):
    host = ProfileHost()
    current = goal()
    bindings, calls = [], []
    def persist(binding):
        bindings.append(binding)
        return binding
    async def rpc(profile, method, params, timeout):
        nonlocal current
        calls.append(method)
        if method == 'chat.command':
            return {'status': 'completed', 'runId': 'initial',
                    'goalBinding': {'version': 1, 'state': 'goal', 'goalId': 'goal-1', 'revision': 1}}
        if method == 'goal.get':
            return {'goal': current}
        if method == 'goal.stop':
            assert bindings == [{'goalId': 'goal-1', 'statusAtRequest': 'active'}]
            assert params['expectedGoalId'] == 'goal-1'
            assert params['expectedRevision'] == 1
            current = goal('cleared', 2)
            return {'goal': current}
        assert method == 'chat.inflight'
        return {'inflight': None, 'goal': current}
    host._target_rpc = rpc
    scope = dict(profile='default', task_id='task-1', run_id='initial',
                 expected_bot_id=profiles.ensure_profile_bot_id('default').bot_id)
    result = await host.stop_task(**scope, on_goal_binding=persist)
    assert result == {'status': 'stopped', 'workerStatus': 'aborted'}
    # A restart reads the same durable binding and does not send stop again.
    assert await host.stop_task(**scope, goal_binding=bindings[0]) == result
    assert calls.count('goal.stop') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', [goal(goalId='new-goal'), goal('done', 4), None])
async def test_cancel_cannot_adopt_a_different_generation_or_clear_done_goal(profile_roots, replacement):
    host = ProfileHost()
    calls = []
    async def rpc(profile, method, params, timeout):
        calls.append(method)
        if method == 'chat.command':
            return {'status': 'completed', 'runId': 'initial'}
        if method == 'goal.get':
            return {'goal': replacement}
        assert method == 'chat.inflight'
        return {'inflight': None, 'goal': replacement}
    host._target_rpc = rpc
    result = await host.stop_task(
        profile='default', task_id='task-1', run_id='initial',
        expected_bot_id=profiles.ensure_profile_bot_id('default').bot_id,
        goal_binding={'goalId': 'goal-1', 'statusAtRequest': 'active'},
    )
    assert result['status'] == ('stopped' if replacement and replacement['status'] == 'done' else 'status_unknown')
    assert 'goal.stop' not in calls
    assert 'chat.abort' not in calls


@pytest.mark.asyncio
async def test_steering_reaches_the_same_live_task_before_its_standing_goal_finishes(board, monkeypatch):
    store, orchestrator = board
    card = test_voice_board.dispatch(orchestrator, profile='default')
    host = ProfileHost()
    monkeypatch.setattr(host, 'TASK_GOAL_POLL_SECONDS', .001)
    current_goal = goal()
    initial_observed, second_accepted, second_can_run = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def terminal(run_id, text):
        await host._handle_profile_event('default', 'chat', {
            'sessionKey': card.session_key, 'runId': run_id, 'state': 'final',
            'message': {'content': text},
        })

    async def rpc(profile, method, params, timeout):
        nonlocal current_goal
        assert profile == 'default'
        assert params['sessionKey'] == card.session_key
        if method == 'runtime.voice.reserve':
            return {'sessionKey': params['sessionKey'], 'reserved': True}
        if method == 'goal.get':
            if calls:
                initial_observed.set()
            return {'goal': current_goal}
        if method == 'chat.send':
            calls.append(params)
            assert params['queueForNextTurn'] is True
            if len(calls) == 1:
                await terminal(params['idempotencyKey'], 'The research is continuing.')
            else:
                second_accepted.set()
            return {'runId': params['idempotencyKey'], 'status': 'accepted'}
        assert method == 'chat.command'
        binding = {'version': 1, 'state': 'goal', 'goalId': 'goal-1', 'revision': 1}
        if params['runId'] != 'steer-live':
            return {'runId': params['runId'], 'status': 'completed', 'goalBinding': binding}
        if not second_can_run.is_set():
            return {'runId': 'steer-live', 'status': 'accepted'}
        await terminal('steer-live', 'Completed with the updated figures.')
        current_goal = goal('done', 4, lastRunId='steer-live')
        return {'runId': 'steer-live', 'status': 'completed', 'goalBinding': binding}

    host._target_rpc = rpc

    async def spawn(prompt, **kwargs):
        result = await host.run_task(
            'default', task_id=card.id, prompt=prompt, idempotency_key=kwargs['command_id'], interactive=True,
            expected_bot_id=card.assignee_bot_id,
            on_started=lambda run: store.set_worker_run_id(card.id, kwargs['claim_token'], run),
            on_next_instruction=lambda completed: store.voice_commands.advance_boundary(
                card.id, kwargs['claim_token'], completed,
            ),
        )
        return result['response']

    orchestrator._spawn = spawn
    task = asyncio.create_task(orchestrator._execute(card.id))
    try:
        await asyncio.wait_for(initial_observed.wait(), 1)
        assert not task.done()
        store.voice_commands.enqueue(
            conversation_id=card.voice_conversation_id, card_id=card.id, command_id='steer-live',
            expected_revision=store.get_card(card.id).revision, text='Use the updated figures.',
        )
        await asyncio.wait_for(second_accepted.wait(), 1)
        assert store.get_card(card.id).status == 'in_progress'
        commands = store.voice_commands.list(card.voice_conversation_id, card.id)
        assert [c['status'] for c in commands] == ['completed', 'delivering']
        assert commands[1]['workerRunId'] is None
        second_can_run.set()
        assert (await asyncio.wait_for(task, 1))[0] == 'done'
        assert store.get_card(card.id).result == 'Completed with the updated figures.'
        assert store.get_card(card.id).attempt_count == 1
        assert len(calls) == 2
        assert calls[1]['message'] == 'Use the updated figures.'
        assert [c['status'] for c in store.voice_commands.list(card.voice_conversation_id, card.id)] == ['completed', 'completed']
    finally:
        second_can_run.set()
        await asyncio.wait_for(task, 1)
