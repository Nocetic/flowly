"""Opening a queued task inspects readiness without starting its worker."""
from dataclasses import replace

import pytest

import flowly.profile as profiles
from flowly.channels import feature_rpc
from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.session.manager import SessionManager
from tests import test_voice_feature_rpc

runtime = test_voice_feature_rpc.runtime
request = test_voice_feature_rpc.request


async def accepted(runtime):
    _, profile, _, _ = runtime
    await feature_rpc.dispatch('voice.open', request(profile))
    result, _ = await feature_rpc.dispatch('voice.tasks.dispatch', request(
        profile, commandId='task-1', title='Prepare the report', body='Include the latest figures',
    ))
    return result['card']


async def read_task(runtime, card):
    _, profile, _, _ = runtime
    result, _ = await feature_rpc.dispatch('voice.tasks.get', request(profile, taskId=card['id']))
    return result


@pytest.mark.asyncio
async def test_queued_task_reports_no_session_without_creating_files_or_a_run(runtime):
    _, profile, store, _ = runtime
    card = await accepted(runtime)
    before = list(profile.path.rglob('*'))
    result = await read_task(runtime, card)
    assert result['workSessionState'] == 'not_created'
    assert result['card']['body'] == 'Include the latest figures'
    assert list(profile.path.rglob('*')) == before
    assert store.get_runs(card['id']) == []


@pytest.mark.asyncio
async def test_durable_reservation_opens_the_chat_before_the_first_agent_response(runtime, monkeypatch):
    _, profile, store, _ = runtime
    card = await accepted(runtime)
    with monkeypatch.context() as env:
        env.setenv('FLOWLY_HOME', str(profile.path))
        sessions = SessionManager(profile.path / 'workspace')
        with request_owner_scope(HOST_OWNER):
            sessions.reserve_voice_work(card['sessionKey'])
    result = await read_task(runtime, card)
    assert result['workSessionState'] == 'ready'
    assert store.get_runs(card['id']) == []
    assert sessions.read(card['sessionKey']).messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['owner', 'corrupt', 'orphan', 'identity', 'identity_during_read'])
async def test_unverifiable_work_session_is_never_reported_as_queued_or_ready(runtime, monkeypatch, failure):
    _, profile, _, _ = runtime
    card = await accepted(runtime)
    with monkeypatch.context() as env:
        env.setenv('FLOWLY_HOME', str(profile.path))
        sessions = SessionManager(profile.path / 'workspace')
        with request_owner_scope(RequestOwner('wrong-account') if failure == 'owner' else HOST_OWNER):
            sessions.reserve_voice_work(card['sessionKey'])
    path = sessions._get_session_path(card['sessionKey'])
    if failure == 'corrupt':
        path.write_text('corrupt metadata\n')
    elif failure == 'orphan':
        path.with_name(path.stem + '.full.jsonl').write_text('private history')
        path.unlink()
    elif failure.startswith('identity'):
        original = profiles.describe_profile
        calls = 0

        def describe(name):
            nonlocal calls
            result = original(name)
            if name == profile.name:
                calls += 1
                if failure == 'identity' or calls > 1:
                    return replace(result, bot_id='replacement-agent')
            return result

        monkeypatch.setattr(profiles, 'describe_profile', describe)
    result = await read_task(runtime, card)
    assert result['workSessionState'] == 'unavailable'
    assert 'wrong-account' not in repr(result)


@pytest.mark.asyncio
async def test_other_conversation_cannot_probe_the_worker_state(runtime):
    _, profile, _, _ = runtime
    card = await accepted(runtime)
    await feature_rpc.dispatch('voice.open', request(profile, conversationId='other'))
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch('voice.tasks.get', request(profile, conversationId='other', taskId=card['id']))
