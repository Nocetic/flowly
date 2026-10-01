from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from loguru import logger

import flowly.profile as profiles
import flowly.profile_host as profile_host_module
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer
from flowly.live_voice.access import VoiceAccessVerifier
from flowly.live_voice.authority import (
    HOST_OWNER,
    RequestOwner,
    current_request_owner,
    request_owner_scope,
    sign_profile_hop,
)
from flowly.profile_host import ProfileHost, _Runtime
from flowly.profile_host_contract import ProfileHostError
from flowly.session.manager import SessionManager
from tests.relay_voice_helpers import relay_rpc
from tests.test_chat_command_transport import Socket
from tests.test_voice_access import HOST
from tests.test_voice_access import fixture as access_fixture


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    server = GatewayServer(host='127.0.0.1', auth_token='g' * 48, require_loopback_auth=True,
                           sessions=SessionManager(tmp_path / 'workspace'), on_chat_message=AsyncMock())
    server.set_managed_runtime_control('runtime-1', lambda: None)
    return server


@pytest.mark.asyncio
async def test_public_gateway_cannot_inherit_ambient_or_client_id_authority(server, monkeypatch):
    seen = []
    monkeypatch.setattr(server, '_dispatch_ws_rpc', AsyncMock(side_effect=lambda *_: seen.append(current_request_owner())))
    server._profile_host_socket = object()
    with request_owner_scope(RequestOwner('ambient-account')):
        await server._handle_ws_rpc(Socket(), server._profile_host_client_id, {'id': 'rpc-1', 'method': 'health', 'params': {}})
    assert seen == [HOST_OWNER]
    assert current_request_owner() is None


@pytest.mark.asyncio
async def test_private_in_process_socket_preserves_the_callers_authority(server, monkeypatch):
    seen = []
    monkeypatch.setattr(server, '_dispatch_ws_rpc', AsyncMock(side_effect=lambda *_: seen.append(current_request_owner())))
    internal = server._profile_host_socket = Socket()
    for owner in (None, HOST_OWNER, RequestOwner('account-a')):
        with request_owner_scope(owner):
            await server._handle_ws_rpc(internal, 'private', {'id': 'rpc-1', 'method': 'health', 'params': {}})
    assert seen == [None, HOST_OWNER, RequestOwner('account-a')]


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
async def test_login_certificate_is_removed_before_dispatch_and_scopes_normal_rpcs(server, monkeypatch, transport):
    token, _, keys, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    with request_owner_scope(RequestOwner('account-1')):
        server.sessions.reserve_voice_work('desktop:voice-work:task-1')
    seen = []
    if transport == 'gateway':
        async def dispatch(_ws, _client, data):
            seen.append((current_request_owner(), data['params']))
        monkeypatch.setattr(server, '_dispatch_ws_rpc', dispatch)
        async def call(frame):
            await server._handle_ws_rpc(Socket(), 'client', frame)
    else:
        channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
        async def dispatch(_ws, data):
            seen.append((current_request_owner(), data['params']))
        monkeypatch.setattr(channel, '_dispatch_rpc', dispatch)
        async def call(frame):
            await relay_rpc(channel, Socket(), frame, uid='account-1')
    await call({'id': 'rpc-1', 'method': 'chat.history', 'params': {'voiceAccess': token(), 'sessionKey': 'desktop:voice-work:task-1'}})
    assert seen == [(RequestOwner('account-1'), {'sessionKey': 'desktop:voice-work:task-1'})]
    assert current_request_owner() is None


@pytest.mark.asyncio
async def test_relay_rejects_even_a_valid_local_parent_proof(server, monkeypatch):
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    dispatch = AsyncMock()
    monkeypatch.setattr(channel, '_dispatch_rpc', dispatch)
    proof = sign_profile_hop(server.voice_parent_key, 'runtime-1', 'rpc-1', 'chat.history', {}, None)
    socket = Socket()
    await channel._handle_rpc(socket, {'id': 'rpc-1', 'method': 'chat.history', 'params': {}, 'voiceAuthority': proof})
    dispatch.assert_not_called()
    assert socket.sent[-1]['error']['code'] == 'VOICE_AUTH_REQUIRED'


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', [None, HOST_OWNER, RequestOwner('account-a')])
async def test_profile_rpc_preserves_authority_end_to_end_without_a_login_bearer(server, monkeypatch, owner):
    host = ProfileHost.__new__(ProfileHost)
    frames = []

    class Reply:
        closed = False

        async def send_json(self, response):
            host._resolve_rpc(runtime, response)

    class Link:
        closed = False

        async def send_json(self, frame):
            frames.append(frame)
            await server._handle_ws_rpc(Reply(), 'profile-parent', frame)

    runtime = _Runtime('writer', None, SimpleNamespace(), Link(), 'runtime-1',
                       capabilities=frozenset({'voice-owner-hop-v1', 'voice-owner-events-v1'}), voice_parent_key=server.voice_parent_key)

    async def dispatch(ws, _client, data):
        assert current_request_owner() == owner
        assert 'voiceAuthority' not in data
        assert 'voiceAccess' not in data['params']
        await ws.send_json({'type': 'rpc', 'id': data['id'], 'result': {'ok': True}})

    monkeypatch.setattr(server, '_dispatch_ws_rpc', dispatch)
    with request_owner_scope(owner):
        assert await host._rpc(runtime, 'chat.inflight', {'sessionKey': 'desktop:voice-work:task-1'}, 1) == {'ok': True}
    assert frames[0]['voiceAuthority']['instanceId'] == 'runtime-1'
    assert server.voice_parent_key not in repr(frames) + repr(runtime)
    assert current_request_owner() is None


@pytest.mark.asyncio
async def test_account_call_cannot_downgrade_to_an_old_profile_runtime():
    host = ProfileHost.__new__(ProfileHost)
    ws = SimpleNamespace(closed=False, send_json=AsyncMock())
    runtime = _Runtime('writer', None, SimpleNamespace(), ws, 'runtime-old')
    with request_owner_scope(RequestOwner('account-a')):
        with pytest.raises(ProfileHostError) as error:
            await host._rpc(runtime, 'chat.history', {'sessionKey': 'desktop:voice-work:task-1'}, 1)
    assert error.value.code == 'VOICE_AUTH_UNAVAILABLE'
    ws.send_json.assert_not_called()
    assert not runtime.pending


@pytest.mark.asyncio
async def test_real_loopback_transport_preserves_scopes_and_rejects_rebound_or_replayed_hops(server, monkeypatch):
    server.port = 0
    seen = []

    def read_owner(params):
        owner = current_request_owner()
        seen.append(owner)
        return {'kind': 'internal' if owner is None else 'account' if owner.uid else 'host',
                'uid': owner.uid if owner else None, 'params': params}

    monkeypatch.setitem(feature_rpc._DISPATCH, 'test.owner', (read_owner, True, False))
    monkeypatch.setattr(feature_rpc, 'FEATURE_METHODS', feature_rpc.FEATURE_METHODS | {'test.owner'})
    host = ProfileHost.__new__(ProfileHost)
    await server.start()
    session = None
    try:
        session, ws = await host._open_runtime_transport(
            port=server.port, token='g' * 48, error_code='TEST_AUTH', error_message='Connection failed.')

        async def request(frame):
            await ws.send_json({'type': 'rpc', **frame})
            while True:
                reply = await ws.receive_json(timeout=3)
                if reply.get('type') == 'rpc' and reply.get('id') == frame['id']:
                    return reply

        # Possession of the gateway token grants host access, never an account
        # identity or the parent's private in-process authority.
        plain = await request({'id': 'plain', 'method': 'test.owner', 'params': {'uid': 'account-a'}})
        assert plain['result']['kind'] == 'host'
        for index, owner in enumerate((None, HOST_OWNER, RequestOwner('account-a'))):
            frame = {'id': f'signed-{index}', 'method': 'test.owner', 'params': {'message': 'İşi sürdür'}}
            frame['voiceAuthority'] = sign_profile_hop(server.voice_parent_key, 'runtime-1', frame['id'],
                                                       frame['method'], frame['params'], owner)
            result = await request(frame)
            assert result['result']['kind'] == ('internal' if owner is None else 'account' if owner.uid else 'host')
            assert result['result']['uid'] == (owner.uid if owner else None)
            assert result['result']['params'] == frame['params']
            replay = await request(frame)
            assert replay['error']['code'] == 'VOICE_AUTH_REQUIRED'
        changed = {'id': 'changed', 'method': 'test.owner', 'params': {'message': 'Original'}}
        changed['voiceAuthority'] = sign_profile_hop(server.voice_parent_key, 'runtime-1', changed['id'],
                                                     changed['method'], changed['params'], RequestOwner('account-a'))
        changed['params'] = {'message': 'Replacement'}
        assert (await request(changed))['error']['code'] == 'VOICE_AUTH_REQUIRED'
        assert seen == [HOST_OWNER, None, HOST_OWNER, RequestOwner('account-a')]
        assert current_request_owner() is None
    finally:
        if session is not None:
            await session.close()
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['chat.clear', 'chat.retry', 'chat.undo'])
async def test_account_chat_callback_failures_do_not_expose_exception_content(server, monkeypatch, method):
    token, _, keys, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    with request_owner_scope(RequestOwner('account-1')):
        server.sessions.reserve_voice_work('desktop:voice-work:task-1')
    setattr(server, f'on_{method.split(".")[1]}', AsyncMock(side_effect=RuntimeError('private-account-content')))
    error = Mock()
    monkeypatch.setattr(logger, 'error', error)
    socket = Socket()
    await server._handle_ws_rpc(socket, 'client', {'id': 'rpc-1', 'method': method,
                                                'params': {'sessionKey': 'desktop:voice-work:task-1', 'voiceAccess': token()}})
    assert socket.sent[-1]['error']['code'] == 'INTERNAL'
    assert 'private-account-content' not in repr(socket.sent) + repr(error.call_args_list)
    assert error.call_count == 1
    assert current_request_owner() is None


@pytest.mark.asyncio
async def test_relay_account_dispatch_failure_is_settled_before_scope_unwinds(monkeypatch):
    token, _, keys, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    monkeypatch.setattr(channel, '_dispatch_rpc', AsyncMock(side_effect=LookupError('private-account-content')))
    error, exception = Mock(), Mock()
    monkeypatch.setattr(logger, 'error', error)
    monkeypatch.setattr(logger, 'exception', exception)
    socket = Socket()
    await relay_rpc(channel, socket, {'id': 'rpc-1', 'sessionId': 'relay-1', 'method': 'chat.inflight',
                                      'params': {'voiceAccess': token()}}, uid='account-1')
    assert socket.sent[-1]['error']['code'] == 'INTERNAL'
    assert socket.sent[-1]['sessionId'] == 'relay-1'
    assert 'private-account-content' not in repr(socket.sent) + repr(error.call_args_list)
    assert error.call_count == 1
    exception.assert_not_called()
    assert current_request_owner() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['missing', 'invalid', 'replaced'])
async def test_attach_cannot_use_a_missing_invalid_or_replaced_parent_key(tmp_path, monkeypatch, change):
    host = ProfileHost.__new__(ProfileHost)
    host._emit = AsyncMock()
    session, ws = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())
    host._open_runtime_transport = AsyncMock(return_value=(session, ws))
    lease = {'instanceId': 'runtime-1', 'authToken': 'g' * 48, 'port': 12345,
             'capabilities': ['voice-owner-hop-v1'], 'voiceParentKey': 'a' * 64}
    monkeypatch.setattr(profile_host_module, 'describe_profile', lambda _name: SimpleNamespace(path=tmp_path))
    monkeypatch.setattr(profile_host_module, 'reconcile_runtime_lease', lambda *_args, **_kw: {**lease, 'voiceParentKey': 'b' * 64})
    if change == 'missing':
        lease.pop('voiceParentKey')
    elif change == 'invalid':
        lease['voiceParentKey'] = 'invalid-key'
    with pytest.raises(ProfileHostError) as error:
        await host._attach_runtime('writer', lease)
    if change == 'replaced':
        assert error.value.code == 'PROFILE_RUNTIME_CHANGED'
        session.close.assert_awaited_once()
        ws.close.assert_awaited_once()
    else:
        assert error.value.code == 'VOICE_AUTH_UNAVAILABLE'
        host._open_runtime_transport.assert_not_awaited()
    assert 'a' * 64 not in str(error.value)
    assert 'b' * 64 not in str(error.value)
