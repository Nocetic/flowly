"""Only the task dispatcher may establish the first voice-work owner."""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import pytest

import flowly.profile as profiles
from flowly.channels import feature_rpc
from flowly.gateway.server import GatewayServer
from flowly.live_voice.access import VoiceAccessVerifier
from flowly.live_voice.authority import (
    HOST_OWNER,
    RequestOwner,
    request_owner_scope,
    sign_profile_hop,
)
from flowly.profile_host import ProfileHost, _Runtime
from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc
from flowly.session.manager import Session, SessionManager
from flowly.session.ownership import SessionAccessError
from tests.test_chat_command_transport import Socket
from tests.test_voice_access import HOST
from tests.test_voice_access import fixture as access_fixture

A = RequestOwner('account-a')
B = RequestOwner('account-b')
KEY = 'desktop:voice-work:task-1'
RESERVE = 'runtime.voice.reserve'


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    return SessionManager(tmp_path / 'workspace')


@pytest.mark.parametrize('owner', [A, B, HOST_OWNER])
@pytest.mark.parametrize('operation', ['get', 'save', 'mutate', 'archive'])
def test_external_request_cannot_claim_an_uncreated_worker_key(sessions, owner, operation):
    callback = Mock()
    actions = {
        'get': lambda: sessions.get_or_create(KEY),
        'save': lambda: sessions.save(Session(key=KEY)),
        'mutate': lambda: sessions.mutate(KEY, callback),
        'archive': lambda: sessions.flush_full(Session(key=KEY), required=True),
    }
    with request_owner_scope(owner), pytest.raises(SessionAccessError):
        actions[operation]()
    callback.assert_not_called()
    assert not list(sessions.sessions_dir.glob('*.jsonl'))


def test_reservation_is_durable_idempotent_and_never_adopts_another_owner(sessions):
    with request_owner_scope(A):
        sessions.reserve_voice_work(KEY)
        session = sessions.get_or_create(KEY)
        assert session.metadata['voiceOwner'] == {'kind': 'account', 'uid': A.uid}
        session.add_message('assistant', 'Saved result')
        sessions.save(session)
        before = sessions._get_session_path(KEY).read_bytes()
        SessionManager(sessions.workspace).reserve_voice_work(KEY)
        assert sessions._get_session_path(KEY).read_bytes() == before
    for owner in (B, HOST_OWNER):
        with request_owner_scope(owner), pytest.raises(SessionAccessError):
            sessions.reserve_voice_work(KEY)
        assert sessions._get_session_path(KEY).read_bytes() == before


def test_missing_alias_and_orphan_archive_cannot_be_claimed(sessions):
    with request_owner_scope(A):
        with pytest.raises(SessionAccessError):
            sessions.get_or_create(KEY.replace(':', '_'))
        sessions.reserve_voice_work(KEY)
        session = sessions.get_or_create(KEY)
        session.add_message('assistant', 'Private result')
        sessions.save(session)
        sessions._get_session_path(KEY).unlink()
        with pytest.raises(SessionAccessError):
            sessions.reserve_voice_work(KEY)


def test_competing_reservations_cannot_replace_the_first_owner(sessions):
    barrier = threading.Barrier(2)

    def reserve(owner):
        manager = SessionManager(sessions.workspace)
        barrier.wait(timeout=3)
        with request_owner_scope(owner):
            try:
                manager.reserve_voice_work(KEY)
            except SessionAccessError:
                return None
            return owner

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (A, B)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert sessions.read(KEY).metadata['voiceOwner'] == {'kind': 'account', 'uid': winners[0].uid}


@pytest.mark.parametrize('key', ['desktop:ordinary', KEY + '/file', KEY + '\\file',
                               'desktop:voice-work:', KEY + '\x00', KEY.replace(':', '_'), None])
def test_reservation_rejects_noncanonical_or_unrelated_keys(sessions, key):
    with request_owner_scope(A), pytest.raises(SessionAccessError):
        sessions.reserve_voice_work(key)
    assert not list(sessions.sessions_dir.glob('*.jsonl'))


@pytest.mark.asyncio
async def test_public_gateway_cannot_reserve_even_with_a_valid_account_certificate(sessions, monkeypatch):
    token, _, keys, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    server = GatewayServer(sessions=sessions, on_chat_message=AsyncMock(),
                           host='127.0.0.1', auth_token='g' * 48, require_loopback_auth=True)
    server.set_managed_runtime_control('runtime-1', lambda: None)
    socket = Socket()
    for owner in (A, B, HOST_OWNER):
        access = {'voiceAccess': token({'sub': owner.uid})} if owner.uid else {}
        await server._handle_ws_rpc(socket, server._profile_host_client_id, {
            'id': 'reserve', 'method': RESERVE, 'params': {'sessionKey': KEY, **access},
        })
        assert socket.sent[-1]['error']['code'] == 'VOICE_AUTH_REQUIRED'
        await server._handle_ws_rpc(socket, 'client', {
            'id': 'send', 'method': 'chat.send',
            'params': {'sessionKey': KEY, 'message': 'Claim first', 'idempotencyKey': 'bad-run', **access},
        })
        assert socket.sent[-1]['error']['code'] == 'NOT_FOUND'
    server.on_chat_message.assert_not_awaited()
    assert server.chat_commands.lookup(KEY, 'bad-run') is None
    assert not list(sessions.sessions_dir.glob('*.jsonl'))


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['private', 'signed'])
async def test_only_verified_parent_can_reserve_the_persisted_owner(sessions, transport):
    server = GatewayServer(sessions=sessions, host='127.0.0.1',
                           auth_token='g' * 48, require_loopback_auth=True)
    server.set_managed_runtime_control('runtime-1', lambda: None)
    socket = Socket()
    if transport == 'private':
        server._profile_host_socket = socket
    for i, owner in enumerate((A, A, B, HOST_OWNER)):
        frame = {'id': f'reserve-{i}', 'method': RESERVE, 'params': {'sessionKey': KEY}}
        if transport == 'signed':
            frame['voiceAuthority'] = sign_profile_hop(server.voice_parent_key, 'runtime-1', frame['id'],
                                                       RESERVE, frame['params'], owner)
        with request_owner_scope(owner):
            await server._handle_ws_rpc(socket, 'parent', frame)
        if owner == A:
            assert socket.sent[-1]['result'] == {'sessionKey': KEY, 'reserved': True}
        else:
            assert socket.sent[-1]['error']['code'] == 'NOT_FOUND'
    assert sessions.read(KEY).metadata['voiceOwner'] == {'kind': 'account', 'uid': A.uid}


def test_private_reservation_is_not_a_public_profile_rpc():
    with pytest.raises(ProfileHostError):
        validate_profile_rpc(RESERVE, {'sessionKey': KEY})
    assert RESERVE not in feature_rpc.FEATURE_METHODS


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [None, {}, {'reserved': True, 'sessionKey': 'desktop:voice-work:other'}])
async def test_task_never_sends_after_an_unverified_reservation(sessions, result):
    host = ProfileHost()
    host._target_rpc = AsyncMock(return_value=result)
    with request_owner_scope(A), pytest.raises(ProfileHostError) as error:
        await host.run_task('default', task_id='task-1', prompt='Run the task',
                            idempotency_key='run-1', interactive=True)
    assert error.value.code == 'VOICE_AUTH_UNAVAILABLE'
    assert [call.args[1] for call in host._target_rpc.await_args_list] == [RESERVE]
    assert not host._interactive_task_sessions


@pytest.mark.asyncio
async def test_host_reservation_cannot_downgrade_to_an_unsigned_runtime(sessions):
    from types import SimpleNamespace

    host = ProfileHost()
    socket = SimpleNamespace(closed=False, send_json=AsyncMock())
    runtime = _Runtime('writer', None, SimpleNamespace(), socket, 'runtime-old')
    with request_owner_scope(HOST_OWNER), pytest.raises(ProfileHostError) as error:
        await host._rpc(runtime, RESERVE, {'sessionKey': KEY}, 0.01)
    assert error.value.code == 'VOICE_AUTH_UNAVAILABLE'
    socket.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_task_dispatcher_reserves_before_the_first_worker_turn(sessions):
    from flowly.board.orchestrator import BoardOrchestrator
    from flowly.board.store import BoardStore
    from flowly.live_voice.authority import current_request_owner

    store = BoardStore(sessions.sessions_dir.parent / 'board.db')
    host = ProfileHost()
    server = GatewayServer(sessions=sessions)
    socket = server._profile_host_socket = Socket()
    calls = []

    async def rpc(profile, method, params, timeout):
        calls.append(method)
        assert profile == 'default'
        assert current_request_owner() == A
        if method == RESERVE:
            await server._handle_ws_rpc(socket, 'private', {'id': 'reserve', 'method': method, 'params': params})
            assert 'error' not in socket.sent[-1]
            return socket.sent[-1]['result']
        if method == 'chat.send':
            persisted = SessionManager(sessions.workspace).read(params['sessionKey'])
            assert persisted.metadata['voiceOwner'] == {'kind': 'account', 'uid': A.uid}
            persisted.add_message('assistant', 'Verified result', run_id=params['idempotencyKey'])
            sessions.save(persisted)
            await host.handle_primary_frame(server._scoped_event({'type': 'event', 'event': 'chat', 'data': {
                'sessionKey': params['sessionKey'], 'runId': params['idempotencyKey'],
                'state': 'final', 'message': {'content': 'Verified result'},
            }}))
            return {'runId': params['idempotencyKey'], 'status': 'completed'}
        if method == 'chat.command':
            return {'runId': params['runId'], 'status': 'completed', 'goalBinding': {'version': 1, 'state': 'none'}}
        if method == 'goal.get':
            return {'goal': None}
        raise AssertionError(method)

    host._target_rpc = rpc

    async def spawn(prompt, **kwargs):
        result = await host.run_task('default', task_id=kwargs['task_id'], prompt=prompt,
                                     idempotency_key=kwargs['command_id'], interactive=True,
                                     expected_bot_id=kwargs['expected_bot_id'])
        return result['response']

    orchestrator = BoardOrchestrator(store, spawn)
    try:
        with request_owner_scope(A):
            card = orchestrator.dispatch_voice(conversation_id='voice-1', command_id='command-1',
                                               profile='default', title='Run the task')
        assert not sessions._get_session_path(card.session_key).exists()
        assert current_request_owner() is None
        await orchestrator.run_card(card.id, deliver=False)
        assert store.get_card(card.id).status == 'done'
        assert store.get_card(card.id).result == 'Verified result'
        assert calls[:2] == [RESERVE, 'chat.send']
        with request_owner_scope(B), pytest.raises(SessionAccessError):
            sessions.read(card.session_key)
    finally:
        store.close()
