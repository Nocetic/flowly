"""Standing goals keep their original authority across reads and restarts."""
import json
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.channels import feature_rpc
from flowly.goals.models import GoalState
from flowly.goals.store import GoalStore
from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.session.manager import Session, SessionManager
from flowly.session.ownership import SessionAccessError

A, B = RequestOwner('goal-account-a'), RequestOwner('goal-account-b')
KEY = 'desktop:voice-work:owned-goal'
SHARED = 'desktop:shared-goal'


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    sessions = SessionManager(tmp_path / 'workspace')
    store = GoalStore(sessions.sessions_dir.parent)
    with request_owner_scope(A):
        sessions.reserve_voice_work(KEY)
        first = store.save(GoalState(session_key=KEY, goal='private objective'))
        current = store.save(GoalState(session_key=KEY, goal='replacement objective'))
    sessions.save(Session(key=SHARED))
    shared = store.save(GoalState(session_key=SHARED, goal='shared objective'))
    return sessions, store, first, current, shared


@pytest.mark.parametrize('owner', [B, HOST_OWNER])
def test_goal_store_denies_reads_and_writes_before_mutation(data, owner):
    _, store, first, current, _ = data
    mutation = Mock(return_value=current)
    with request_owner_scope(owner):
        for operation in (
            lambda: store.get(KEY),
            lambda: store.get_generation(KEY, first.goal_id),
            lambda: store.save(current),
            lambda: store.update(KEY, mutation),
            lambda: store.compare_and_update(current, mutation),
        ):
            with pytest.raises(SessionAccessError):
                operation()
        assert [item.session_key for item in store.iter_states()] == [SHARED]
    mutation.assert_not_called()
    assert store.get(KEY).revision == current.revision


@pytest.mark.parametrize('change', ['owner', 'delete', 'corrupt'])
def test_original_owner_survives_restart_and_canonical_replacement(data, change):
    sessions, store, first, current, _ = data
    if change == 'owner':
        sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    elif change == 'delete':
        sessions.delete(KEY)
    else:
        sessions._get_session_path(KEY).write_text('{broken')
    reopened = GoalStore(store.root.parent)
    for owner in (A, B, HOST_OWNER):
        with request_owner_scope(owner):
            for operation in (
                lambda: reopened.get(KEY),
                lambda: reopened.get_generation(KEY, first.goal_id),
                lambda: reopened.save(current),
                lambda: reopened.save(GoalState(session_key=KEY, goal='adopted')),
            ):
                with pytest.raises(SessionAccessError):
                    operation()
            assert [row.session_key for row in reopened.iter_states()] == [SHARED]


def test_history_retains_original_owner_even_after_current_record_is_removed(data):
    sessions, store, first, _, _ = data
    store._paths(KEY)[1].unlink()
    sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    with request_owner_scope(B), pytest.raises(SessionAccessError):
        GoalStore(store.root.parent).get_generation(KEY, first.goal_id)


def test_saved_snapshot_cannot_be_reassigned_or_resurrected_for_another_owner(data):
    sessions, store, _, current, _ = data
    store._paths(KEY)[1].unlink()
    sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    with request_owner_scope(B), pytest.raises(SessionAccessError):
        store.save(current)
    current.session_key = SHARED
    with request_owner_scope(A), pytest.raises(SessionAccessError):
        store.save(current)
    mutation = Mock(return_value=current)
    with request_owner_scope(A), pytest.raises(SessionAccessError):
        store.compare_and_update(current, mutation)
    mutation.assert_not_called()


def test_allowed_goal_reads_preserve_proof_and_do_not_publish_account_ids(data):
    _, store, first, current, _ = data
    with request_owner_scope(A):
        reopened = GoalStore(store.root.parent)
        assert reopened.get(KEY).goal_id == current.goal_id
        assert reopened.get_generation(KEY, first.goal_id)['goalId'] == first.goal_id
        updated = reopened.update(KEY, lambda state: state)
        assert updated.revision == current.revision + 1
        assert {row.session_key for row in reopened.iter_states()} == {KEY, SHARED}
    assert A.uid not in json.dumps(updated.to_public_dict())
    assert A.uid not in json.dumps(updated.to_dict())
    assert A.uid not in repr(updated)


def test_shared_goal_is_visible_but_cannot_be_adopted_by_retagging_session(data):
    sessions, store, _, _, shared = data
    for owner in (A, B, HOST_OWNER):
        with request_owner_scope(owner):
            assert store.get(SHARED).goal_id == shared.goal_id
    sessions.mutate(SHARED, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': A.uid}))
    with request_owner_scope(A), pytest.raises(SessionAccessError):
        store.get(SHARED)


@pytest.mark.parametrize('raw', ['missing', None, {}, {'kind': 'internal'}, {'kind': 'account', 'uid': None}])
def test_unverifiable_persisted_binding_does_not_adopt_current_account(data, raw):
    _, store, _, _, _ = data
    path = store._paths(KEY)[1]
    record = json.loads(path.read_text())
    if raw == 'missing':
        record.pop('sessionOwner', None)
    else:
        record['sessionOwner'] = raw
    path.write_text(json.dumps(record))
    with request_owner_scope(A), pytest.raises(SessionAccessError):
        GoalStore(store.root.parent).get(KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['goal.get', 'goal.pause', 'goal.resume', 'goal.stop'])
async def test_goal_rpc_denies_before_custom_provider_or_control_callback(data, monkeypatch, method):
    _, _, _, current, _ = data
    provider = Mock(return_value=current)
    control = AsyncMock(return_value={'goal': current.to_public_dict()})
    monkeypatch.setattr(feature_rpc, '_goal_state_provider', provider)
    monkeypatch.setattr(feature_rpc, '_goal_control_cb', control)
    with request_owner_scope(B), pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch(method, {'sessionKey': KEY})
    assert error.value.code == 'NOT_FOUND'
    provider.assert_not_called()
    control.assert_not_awaited()


@pytest.mark.asyncio
async def test_rpc_rechecks_persisted_owner_before_callback_after_reassignment(data, monkeypatch):
    sessions, _, _, current, _ = data
    sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    control = AsyncMock(return_value={'goal': current.to_public_dict()})
    monkeypatch.setattr(feature_rpc, '_goal_control_cb', control)
    with request_owner_scope(B), pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch('goal.resume', {'sessionKey': KEY})
    assert error.value.code == 'NOT_FOUND'
    control.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_rpc_checks_original_proof_owner_before_custom_provider(data, monkeypatch):
    sessions, store, first, _, _ = data
    store._paths(KEY)[1].unlink()
    sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    with request_owner_scope(B):
        store.save(GoalState(session_key=KEY, goal='account B objective'))
    provider = Mock(return_value={'goalId': first.goal_id, 'status': 'done'})
    monkeypatch.setattr(feature_rpc, '_goal_generation_provider', provider)
    with request_owner_scope(B), pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch('goal.get', {'sessionKey': KEY, 'goalId': first.goal_id})
    assert error.value.code == 'NOT_FOUND'
    provider.assert_not_called()


@pytest.fixture
def agent(data):
    from flowly.agent.loop import AgentLoop
    from flowly.goals.manager import GoalManager

    loop = object.__new__(AgentLoop)
    loop.goal_manager = GoalManager(data[1], Mock())
    loop.goal_runtime = Mock()
    loop.abort_autonomous_run = Mock()
    loop.context_epoch = lambda _: 0
    loop.publish_goal_snapshot = AsyncMock()
    loop._gateway_server = None
    return loop


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['pause', 'resume', 'stop'])
async def test_agent_controls_check_owner_before_runtime_effects(data, agent, action):
    with request_owner_scope(B), pytest.raises(SessionAccessError):
        await agent.goal_control(KEY, action)
    agent.abort_autonomous_run.assert_not_called()
    agent.goal_runtime.cancel_session.assert_not_called()
    agent.goal_runtime.wake.assert_not_called()
    agent.publish_goal_snapshot.assert_not_awaited()


def test_store_authority_uses_its_own_profile_root(data, monkeypatch, tmp_path):
    _, store, _, current, _ = data
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'another-profile'))
    other = SessionManager(tmp_path / 'other-workspace')
    with request_owner_scope(B):
        other.reserve_voice_work(KEY)
    with request_owner_scope(A):
        assert store.get(KEY).goal_id == current.goal_id
        saved = store.save(GoalState(session_key=KEY, goal='same profile'))
        assert saved.revision == current.revision + 1


def test_legacy_shared_and_host_goals_preserve_access_without_account_adoption(data):
    sessions, store, _, _, _ = data
    host_key = 'desktop:voice-work:host-goal'
    with request_owner_scope(HOST_OWNER):
        sessions.reserve_voice_work(host_key)
        host_goal = store.save(GoalState(session_key=host_key, goal='host objective'))
    for key in (host_key, SHARED):
        path = store._paths(key)[1]
        record = json.loads(path.read_text())
        record.pop('sessionOwner')
        path.write_text(json.dumps(record))
    with request_owner_scope(HOST_OWNER):
        assert store.get(host_key).goal_id == host_goal.goal_id
    for owner in (A, B, HOST_OWNER):
        with request_owner_scope(owner):
            assert store.get(SHARED).goal == 'shared objective'
    with request_owner_scope(A), pytest.raises(SessionAccessError):
        store.get(host_key)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
@pytest.mark.parametrize('method', ['goal.get', 'goal.pause', 'goal.resume', 'goal.stop'])
async def test_public_rpc_requires_certificate_owner_before_goal_callback(data, monkeypatch, transport, method):
    import flowly.profile as profiles
    from flowly.bus.queue import MessageBus
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig
    from flowly.gateway.server import GatewayServer
    from flowly.live_voice.access import VoiceAccessVerifier
    from tests.test_chat_command_transport import Socket
    from tests.test_voice_access import HOST
    from tests.test_voice_access import fixture as access_fixture

    sessions, _, _, current, _ = data
    token, _, keys, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    provider = Mock(return_value=current)
    control = AsyncMock(return_value={'goal': current.to_public_dict()})
    monkeypatch.setattr(feature_rpc, '_goal_state_provider', provider)
    monkeypatch.setattr(feature_rpc, '_goal_control_cb', control)
    server = GatewayServer(sessions=sessions)
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    socket = Socket()

    async def call(owner):
        access = {'voiceAccess': token({'sub': owner.uid})} if owner.uid else {}
        frame = {'id': 'goal', 'method': method, 'sessionId': 'relay',
                 'params': {'sessionKey': KEY, **access}}
        if transport == 'gateway':
            await server._handle_ws_rpc(socket, 'client', frame)
        else:
            from tests.relay_voice_helpers import relay_rpc

            await relay_rpc(channel, socket, frame, uid=owner.uid)

    try:
        for owner in (B, HOST_OWNER):
            await call(owner)
            assert socket.sent[-1]['error']['code'] == 'NOT_FOUND'
            assert 'private objective' not in json.dumps(socket.sent[-1])
        provider.assert_not_called()
        control.assert_not_awaited()
        await call(A)
        assert socket.sent[-1]['result']['goal']['goalId'] == current.goal_id
    finally:
        server.chat_commands.close()
        channel.chat_commands.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['owner', 'pause', 'replace', 'delete'])
async def test_resume_rechecks_owner_and_generation_after_awaited_publish(data, agent, change):
    from flowly.goals.store import GoalStoreConflictError

    sessions, store, _, _, _ = data
    with request_owner_scope(A):
        agent.goal_manager.pause(KEY)

    async def publish(*_):
        with request_owner_scope(None):
            if change == 'owner':
                sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
            elif change == 'delete':
                sessions.delete(KEY)
            elif change == 'pause':
                agent.goal_manager.pause(KEY)
            else:
                store.save(GoalState(session_key=KEY, goal='new objective'))

    agent.publish_goal_snapshot.side_effect = publish
    error = SessionAccessError if change in {'owner', 'delete'} else GoalStoreConflictError
    with request_owner_scope(A), pytest.raises(error):
        await agent.goal_control(KEY, 'resume')
    agent.goal_runtime.wake.assert_not_called()
