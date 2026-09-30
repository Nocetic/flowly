"""Conversation preparation is independent of the bounded worker queue.

Regression tests for maintainer execution; no provider or model calls.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from flowly.channels import feature_rpc
from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError
from flowly.session.manager import SessionManager
from tests.test_voice_board import board, dispatch
from tests.test_voice_work_readiness import runtime, accepted, read_task
from tests.test_voice_feature_rpc import request


@pytest.mark.asyncio
async def test_prepare_many_chats_without_claiming_or_sending(runtime, monkeypatch):
    service, profile, store, _ = runtime
    cards = []
    await feature_rpc.dispatch('voice.open', request(profile))
    for i in range(12):
        result, _ = await feature_rpc.dispatch('voice.tasks.dispatch', request(
            profile, commandId=f'chat-{i}', title=f'Chat {i}', body=f'Instruction {i}',
        ))
        cards.append(result['card'])
    # Only the simulated child runtime uses the profile home. Public dispatch
    # below still runs on the primary host, as it does in production.
    with monkeypatch.context() as child:
        child.setenv('FLOWLY_HOME', str(profile.path))
        sessions = SessionManager(profile.path / 'workspace')

    async def reserve(name, method, params, timeout):
        assert name == profile.name and method == 'runtime.voice.reserve'
        assert params['expectedBotId'] == profile.bot_id
        with monkeypatch.context() as child:
            child.setenv('FLOWLY_HOME', str(profile.path))
            sessions.reserve_voice_work(params['sessionKey'])
        return {'reserved': True, 'sessionKey': params['sessionKey']}

    host = type('Host', (), {})()
    host._target_rpc = AsyncMock(side_effect=reserve)
    service.worker = lambda: host
    # Opening old task chats must not depend on a still-live voice connection.
    await feature_rpc.dispatch('voice.end', request(profile))
    for card in cards:
        params = {'conversationId': 'voice-1', 'taskId': card['id'], 'profile': 'forged', 'botId': 'forged'}
        before = store.voice_commands.list('voice-1', card['id'])
        for _ in range(2):
            result, _ = await feature_rpc.dispatch('voice.tasks.prepare', params)
            assert result['workSessionState'] == 'ready'
            assert result['card']['id'] == card['id']
        assert store.get_runs(card['id']) == []
        assert store.voice_commands.list('voice-1', card['id']) == before
        with request_owner_scope(HOST_OWNER):
            assert sessions.read(card['sessionKey']).messages == []
    assert host._target_rpc.await_count == 12  # Repeat opens are pure reads.


@pytest.mark.asyncio
async def test_prepare_checks_membership_before_touching_runtime(runtime):
    service, profile, _, _ = runtime
    card = await accepted(runtime)
    host = type('Host', (), {'_target_rpc': AsyncMock()})()
    service.worker = lambda: host
    await feature_rpc.dispatch('voice.open', request(profile, conversationId='other'))
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch('voice.tasks.prepare', {'conversationId': 'other', 'taskId': card['id']})
    with request_owner_scope(RequestOwner('another-account')):
        with pytest.raises(feature_rpc.FeatureRpcError):
            await feature_rpc.dispatch('voice.tasks.prepare', {'conversationId': 'voice-1', 'taskId': card['id']})
    host._target_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_preparation_never_claims_work(runtime):
    service, _, store, _ = runtime
    card = await accepted(runtime)
    host = type('Host', (), {'_target_rpc': AsyncMock(return_value={'reserved': True, 'sessionKey': 'wrong'})})()
    service.worker = lambda: host
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch('voice.tasks.prepare', {'conversationId': 'voice-1', 'taskId': card['id']})
    assert store.get_runs(card['id']) == []
    assert (await read_task(runtime, card))['workSessionState'] == 'not_created'


def test_scheduler_selects_independent_voice_chats_even_on_busy_profile(board):
    store, orchestrator = board
    cards = [dispatch(orchestrator, command_id=f'chat-{i}') for i in range(12)]
    picked = store.list_dispatchable(limit=20, exclude_profiles=('creative',))
    assert {card.id for card in picked} == {card.id for card in cards}


@pytest.mark.asyncio
async def test_same_agent_voice_work_uses_global_capacity_and_releases_slots(board):
    store, orchestrator = board
    started = asyncio.Queue()
    finish = asyncio.Event()

    async def worker(prompt, **kwargs):
        await started.put(kwargs['task_id'])
        await finish.wait()
        return 'Done'

    orchestrator._spawn = worker
    cards = [dispatch(orchestrator, command_id=f'chat-{i}') for i in range(7)]
    try:
        assert await orchestrator.dispatch_once() == orchestrator.MAX_PARALLEL
        first = {await asyncio.wait_for(started.get(), 1) for _ in range(orchestrator.MAX_PARALLEL)}
        assert len(first) == orchestrator.MAX_PARALLEL
        assert await orchestrator.dispatch_once() == 0
        assert len([c for c in cards if store.get_card(c.id).claim_token]) == orchestrator.MAX_PARALLEL
        finish.set()
        await asyncio.gather(*tuple(orchestrator._dispatch_tasks.values()))
        assert await orchestrator.dispatch_once() == 2
        await asyncio.gather(*tuple(orchestrator._dispatch_tasks.values()))
        assert all(store.get_card(c.id).status == 'done' for c in cards)
    finally:
        finish.set()
        await orchestrator.stop_dispatcher()


@pytest.mark.asyncio
async def test_profile_host_locks_voice_work_by_chat_not_agent(board):
    _, _ = board  # Initializes the real named profile fixture.
    host = ProfileHost()
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def reserve(name, method, params, timeout):
        assert method == 'runtime.voice.reserve'
        await entered.put(params['sessionKey'])
        await release.wait()
        # Stop before execution: this regression exercises real lock admission.
        raise ProfileHostError('UNAVAILABLE', 'Controlled end')

    host._target_rpc = AsyncMock(side_effect=reserve)
    tasks = [asyncio.create_task(host.run_task('creative', task_id=key, prompt='Hello',
        idempotency_key=f'command-{i}', interactive=True)) for i, key in enumerate(['one', 'two', 'one'])]
    try:
        assert {await asyncio.wait_for(entered.get(), 1) for _ in range(2)} == {
            'desktop:voice-work:one', 'desktop:voice-work:two',
        }
        assert entered.empty()  # The duplicate session remains serialized.
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
