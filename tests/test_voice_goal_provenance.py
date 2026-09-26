"""Durable causal evidence connects a work command to its goal generation."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from flowly.goals.manager import GoalManager
from flowly.goals.models import GoalState, GoalStatus
from flowly.goals.store import GoalStore, GoalStoreError
from flowly.session.commands import ChatCommandStore
from tests import test_profile_host

profile_roots = test_profile_host.profile_roots


def execution_loop(tmp_path):
    from flowly.agent.loop import AgentLoop

    loop = object.__new__(AgentLoop)
    loop._goal_user_epochs = {}
    loop._session_turn_locks = {}
    loop.goal_runtime = object()
    loop.goal_manager = GoalManager(GoalStore(tmp_path), AsyncMock())
    commands = ChatCommandStore(tmp_path / 'commands.sqlite3')
    loop._gateway_server = SimpleNamespace(chat_commands=commands)
    return loop, commands


@pytest.mark.asyncio
async def test_agent_records_goal_evidence_before_unlocking_the_turn(tmp_path):
    from flowly.bus.events import InboundMessage, OutboundMessage

    loop, commands = execution_loop(tmp_path)
    commands.accept('web:chat', 'initial', {'message': 'Build report'})

    async def execute(message):
        receipt = commands.lookup(message.session_key, 'initial')
        assert receipt['status'] == 'running'
        assert receipt['goalBinding']['state'] == 'pending'
        state = loop.goal_manager.set(message.session_key, 'Finish report')
        assert state.created_by_run_id == 'initial'
        return OutboundMessage(channel='web', chat_id='chat', content='Working')

    loop._process_message_unlocked = execute
    message = InboundMessage(channel='web', chat_id='chat', sender_id='user', content='Build report', metadata={'run_id': 'initial'})
    try:
        await loop._process_message(message)
        receipt = commands.lookup(message.session_key, 'initial')
        assert receipt['status'] == 'completed'
        assert receipt['goalBinding']['goalId'] == loop.goal_manager.get(message.session_key).goal_id
        assert not loop._session_turn_locks[message.session_key].locked()
    finally:
        commands.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['error', 'aborted', 'provider_error', 'skipped'])
async def test_agent_lifecycle_persists_unsuccessful_execution(tmp_path, outcome):
    from flowly.bus.events import InboundMessage, OutboundMessage

    loop, commands = execution_loop(tmp_path)
    commands.accept('web:chat', 'initial', {'message': 'Build report'})

    async def execute(message):
        loop.goal_manager.set(message.session_key, 'Finish report')
        if outcome == 'error':
            raise RuntimeError('test worker failure')
        if outcome == 'aborted':
            raise asyncio.CancelledError()
        if outcome == 'skipped':
            return None
        return OutboundMessage(channel='web', chat_id='chat', content='Provider unavailable', metadata={'error': {'code': 'unavailable'}})

    loop._process_message_unlocked = execute
    message = InboundMessage(channel='web', chat_id='chat', sender_id='user', content='Build report', metadata={'run_id': 'initial'})
    try:
        if outcome in {'error', 'aborted'}:
            with pytest.raises(RuntimeError if outcome == 'error' else asyncio.CancelledError):
                await loop._process_message(message)
        else:
            await loop._process_message(message)
        receipt = commands.lookup(message.session_key, 'initial')
        assert receipt['status'] == ('aborted' if outcome in {'aborted', 'skipped'} else 'error')
        assert receipt['goalBinding']['goalId'] == loop.goal_manager.get(message.session_key).goal_id
    finally:
        commands.close()


def test_replacing_a_goal_retains_minimal_verified_generation_proof(tmp_path):
    store = GoalStore(tmp_path)
    initial = store.save(GoalState(session_key='work', goal='Private report requirements', created_by_run_id='initial'))
    initial.status = GoalStatus.DONE
    initial.last_run_id = 'final-run'
    done = store.save(initial)
    replacement = store.save(GoalState(session_key='work', goal='Next project'))
    reopened = GoalStore(tmp_path)
    proof = reopened.get_generation('work', done.goal_id)
    assert proof == {'goalId': done.goal_id, 'revision': done.revision, 'status': 'done',
                     'lastRunId': 'final-run', 'createdByRunId': 'initial'}
    assert reopened.get_generation('work', replacement.goal_id)['status'] == 'active'
    assert reopened.get_generation('another-work', done.goal_id) is None
    assert [state.goal_id for state in reopened.iter_states()] == [replacement.goal_id]
    archive = next((store.root / 'history').glob('*.json'))
    assert 'Private report requirements' not in archive.read_text()
    assert json.loads(archive.read_text())['sessionKey'] == 'work'


def test_failed_generation_archive_keeps_the_current_goal(tmp_path, monkeypatch):
    store = GoalStore(tmp_path)
    original = store.save(GoalState(session_key='work', goal='Report'))
    real_write = store._write_json

    def fail_archive(path, value):
        if path.parent.name == 'history':
            raise GoalStoreError('test disk failure')
        real_write(path, value)

    monkeypatch.setattr(store, '_write_json', fail_archive)
    with pytest.raises(GoalStoreError):
        store.save(GoalState(session_key='work', goal='Replacement'))
    assert store.get('work').goal_id == original.goal_id


def test_turn_origin_is_scoped_and_restored_even_after_failure(tmp_path):
    from flowly.goals.provenance import goal_turn_scope

    manager = GoalManager(GoalStore(tmp_path), AsyncMock())
    with pytest.raises(RuntimeError), goal_turn_scope('work', 'run-1'):
        owned = manager.set('work', 'Own goal')
        foreign = manager.set('different-work', 'Another goal')
        assert owned.created_by_run_id == 'run-1'
        assert foreign.created_by_run_id is None
        raise RuntimeError('test exit')
    unscoped = manager.set('work', 'Outside the turn')
    assert unscoped.created_by_run_id is None
    assert manager.store.get_generation('work', owned.goal_id)['createdByRunId'] == 'run-1'


@pytest.mark.asyncio
async def test_background_task_cannot_attribute_a_later_goal_to_a_finished_turn(tmp_path):
    from flowly.goals.provenance import creating_run_id, goal_turn_scope

    manager = GoalManager(GoalStore(tmp_path), AsyncMock())
    active, release = asyncio.Event(), asyncio.Event()

    async def background():
        assert creating_run_id('work') == 'run-1'
        active.set()
        await release.wait()
        return manager.set('work', 'Later background work')

    with goal_turn_scope('work', 'run-1'):
        task = asyncio.create_task(background())
        await active.wait()
    release.set()
    result = await task
    assert result.created_by_run_id is None


@pytest.mark.asyncio
async def test_host_recovers_the_completed_execution_chain_after_restart_and_goal_replacement(tmp_path, profile_roots):
    import flowly.profile as profiles
    from flowly.bus.events import InboundMessage, OutboundMessage
    from flowly.profile_host import ProfileHost

    loop, commands = execution_loop(tmp_path)
    session_key, chat_id = 'desktop:voice-work:task-1', 'voice-work:task-1'
    agent_id = profiles.ensure_profile_bot_id('default').bot_id
    request = {'sessionKey': session_key, 'message': 'Complete the report', 'thinking': False,
               'queueForNextTurn': True, 'idempotencyKey': 'initial', 'profileDirectory': [],
               'profileMentions': [], 'disabledTools': [], 'turnOrigin': 'user', 'expectedBotId': agent_id}
    commands.accept(session_key, 'initial', request)
    messages = []

    async def execute(message):
        if message.metadata['run_id'] == 'initial':
            loop.goal_manager.set(session_key, 'Complete the report')
            response = 'I have started working.'
        else:
            state = loop.goal_manager.get(session_key)
            state.status, state.last_run_id = GoalStatus.DONE, 'verified-final'
            loop.goal_manager.store.save(state)
            response = 'The report is complete and verified.'
        messages.append({'role': 'assistant', 'runId': message.metadata['run_id'], 'content': response})
        return OutboundMessage(channel='desktop', chat_id=chat_id, content=response)

    loop._process_message_unlocked = execute
    await loop._process_message(InboundMessage(channel='desktop', chat_id=chat_id, sender_id='user', content=request['message'], metadata={'run_id': 'initial'}))
    original = loop.goal_manager.get(session_key)
    await loop._process_message(InboundMessage(channel='desktop', chat_id=chat_id, sender_id='goal', content='', metadata={
        'run_id': 'verified-final', 'goal_run': True, '_goal_continuation_goal_id': original.goal_id,
        '_goal_base_user_epoch': loop.goal_user_epoch(session_key),
    }))
    replacement = loop.goal_manager.set(session_key, 'An unrelated later goal')
    commands.close()
    restarted = ChatCommandStore(tmp_path / 'commands.sqlite3', owner_id='restarted-process')
    historical = GoalStore(tmp_path)
    calls = []

    async def rpc(profile, method, params, timeout):
        assert profile == 'default' and params['sessionKey'] == session_key
        assert params['expectedBotId'] == agent_id
        calls.append(method)
        if method == 'runtime.voice.reserve':
            return {'sessionKey': session_key, 'reserved': True}
        if method == 'chat.send':
            created, receipt = restarted.accept(session_key, params['idempotencyKey'], params)
            assert not created
            return receipt
        if method == 'chat.command':
            return restarted.lookup(session_key, params['runId'])
        if method == 'chat.history':
            return {'messages': messages}
        if method == 'goal.get':
            return {'goal': historical.get_generation(session_key, params['goalId']) if 'goalId' in params
                    else historical.get(session_key).to_public_dict()}
        raise AssertionError(method)

    host = ProfileHost()
    host._target_rpc = rpc
    try:
        result = await host.run_task('default', task_id='task-1', prompt=request['message'], idempotency_key='initial',
                                     interactive=True, expected_bot_id=agent_id)
        assert result == {'runId': 'initial', 'completedRunId': 'verified-final', 'response': 'The report is complete and verified.'}
        assert calls.count('chat.send') == 1
        assert len(messages) == 2  # Reading a replay never executes again.
        assert historical.get(session_key).goal_id == replacement.goal_id
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_generation_rpc_uses_the_requested_goal_and_rejects_invalid_identity(tmp_path, monkeypatch):
    from flowly.channels import feature_rpc

    store = GoalStore(tmp_path)
    previous = store.save(GoalState(session_key='work', goal='Original', status=GoalStatus.DONE, last_run_id='final'))
    store.save(GoalState(session_key='work', goal='Replacement'))
    monkeypatch.setattr(feature_rpc, '_goal_generation_provider', store.get_generation)
    result, restart = await feature_rpc.dispatch('goal.get', {'sessionKey': 'work', 'goalId': previous.goal_id})
    assert result['goal']['goalId'] == previous.goal_id
    assert result['goal']['status'] == 'done'
    assert 'goal' not in result['goal']
    assert not restart
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch('goal.get', {'sessionKey': 'work', 'goalId': '\ninvalid'})
    assert error.value.code == 'INVALID_PARAMS'


@pytest.mark.asyncio
@pytest.mark.parametrize('goal_id', [None, True, '', 'x' * 513, 'bad\nidentity', 'bad\x7fidentity'])
async def test_invalid_goal_generation_is_rejected_before_profile_start(profile_roots, goal_id):
    import flowly.profile as profiles
    from flowly.profile_host import ProfileHost, ProfileHostError

    profiles.create_profile('writer', local_runtime=True)
    host = ProfileHost()
    host._ensure_runtime = AsyncMock(side_effect=AssertionError('Must not start a profile'))
    with pytest.raises(ProfileHostError) as error:
        await host.rpc('writer', 'goal.get', {'sessionKey': 'desktop:voice-work:task', 'goalId': goal_id})
    assert error.value.code == 'INVALID_PARAMS'
    host._ensure_runtime.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['accepted', 'error', 'aborted'])
async def test_offline_goal_delivery_records_acceptance_before_publishing(tmp_path, outcome):
    from flowly.agent.loop import _AgentGoalDelivery

    loop, commands = execution_loop(tmp_path)
    run_ids = []

    async def publish(message):
        run_ids.append(message.metadata['run_id'])
        assert commands.lookup(message.session_key, run_ids[-1])['status'] == 'accepted'
        if outcome == 'error':
            raise RuntimeError('test publish failure')
        if outcome == 'aborted':
            raise asyncio.CancelledError()

    loop.bus = SimpleNamespace(publish_inbound=publish)
    delivery = _AgentGoalDelivery(loop, session_key='web:chat', channel='web', chat_id='chat', direct=False)
    try:
        pending = delivery.run_continuation(session_key='web:chat', goal_id='goal-1', user_epoch=0, kickoff=False)
        if outcome == 'accepted':
            await pending
        else:
            with pytest.raises(RuntimeError if outcome == 'error' else asyncio.CancelledError):
                await pending
        assert commands.lookup('web:chat', run_ids[0])['status'] == outcome
    finally:
        commands.close()
