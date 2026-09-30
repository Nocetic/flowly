"""Validate the actual relay's UTF-8 identity envelope across the language hop."""
import asyncio
import copy
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from flowly.live_voice.authority import VoiceAuthorityError
from flowly.live_voice.relay_transport import RelayBrowserVerifier
from tests.relay_voice_helpers import prepare_relay, relay_frame, relay_rpc


@pytest.fixture
def vector():
    return json.loads((Path(__file__).parent / 'fixtures' / 'voice-relay-authority.json').read_text())


def verifier(vector, **clock):
    return RelayBrowserVerifier(vector['handshake'], server_id='server-a', now=clock.pop('now', lambda: 1000), **clock)


def test_accepts_the_node_signed_body_without_reserializing_unicode_or_numbers(vector):
    message = verifier(vector).verify(vector['frame'])
    assert message['params'] == {'text': 'İstanbul 💬', 'number': 1}
    assert message.principal.uid == 'account-a'
    assert message.principal.session_id == 'browser-a'
    assert message.principal.conversation_id == 'desktop:voice-work:task'
    assert message.principal.expires_at == 1300
    assert 'authority' not in message
    assert 'account-a' not in json.dumps(message)
    assert vector['handshake']['key'] not in repr(verifier(vector))


@pytest.mark.parametrize('field,value', [('userId', 'account-b'), ('serverId', 'server-b'),
                                         ('sessionId', 'browser-b'), ('conversationId', 'other'),
                                         ('kind', 'connected'), ('sequence', 2), ('expiresAt', 1301), ('linkId', 'other')])
def test_every_identity_field_is_signed(vector, field, value):
    vector['frame']['authority'][field] = value
    with pytest.raises(VoiceAuthorityError):
        verifier(vector).verify(vector['frame'])


@pytest.mark.parametrize('mutation', ['body', 'mac', 'missing', 'key', 'server'])
def test_unverified_frames_cannot_establish_browser_identity(vector, mutation):
    expected = 'server-a'
    if mutation == 'body':
        vector['frame']['body'] = vector['frame']['body'].replace('health', 'chat.send')
    elif mutation == 'mac':
        vector['frame']['mac'] = 'b' * 64
    elif mutation == 'missing':
        vector['frame'].pop('authority')
    elif mutation == 'key':
        vector['handshake']['key'] = 'b' * 64
    else:
        expected = 'server-b'
    with pytest.raises(VoiceAuthorityError):
        RelayBrowserVerifier(vector['handshake'], server_id=expected, now=lambda: 1000).verify(vector['frame'])


def test_replay_and_clock_rollback_cannot_revive_an_old_identity(vector):
    wall, monotonic = [1000.0], [50.0]
    reader = verifier(vector, now=lambda: wall[0], monotonic=lambda: monotonic[0])
    reader.verify(vector['frame'])
    with pytest.raises(VoiceAuthorityError):
        reader.verify(vector['frame'])
    fresh = verifier(vector, now=lambda: wall[0], monotonic=lambda: monotonic[0])
    monotonic[0] += 301
    with pytest.raises(VoiceAuthorityError):
        fresh.verify(vector['frame'])


def test_bad_mac_with_a_higher_sequence_cannot_poison_the_next_valid_message(vector):
    reader = verifier(vector)
    fake = copy.deepcopy(vector['frame'])
    fake['authority']['sequence'] = 9000
    with pytest.raises(VoiceAuthorityError):
        reader.verify(fake)
    assert reader.verify(vector['frame'])['id'] == 'cross-language'


@pytest.fixture
def admission(monkeypatch):
    import flowly.profile as profiles
    from flowly.bus.queue import MessageBus
    from flowly.channels import feature_rpc
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig
    from flowly.live_voice.access import VoiceAccessVerifier
    from tests.test_chat_command_transport import Socket
    from tests.test_voice_access import HOST
    from tests.test_voice_access import fixture as certificate_fixture

    token, _, keys, _, _ = certificate_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    channel, socket = WebChannel(WebChannelConfig(enabled=True, server_id='server-a'), MessageBus()), Socket()
    prepare_relay(channel, socket)
    yield channel, socket, token
    channel.chat_commands.close()


@pytest.mark.asyncio
async def test_certificate_for_another_account_is_rejected_before_dispatch(admission, monkeypatch):
    channel, socket, token = admission
    dispatch = AsyncMock()
    monkeypatch.setattr(channel, '_dispatch_rpc', dispatch)
    await relay_rpc(channel, socket, {'id': 'wrong-account', 'method': 'health',
                                     'params': {'voiceAccess': token({'sub': 'account-a'})}}, uid='account-b')
    dispatch.assert_not_awaited()
    assert socket.sent[-1]['error']['code'] == 'VOICE_AUTH_REQUIRED'


@pytest.mark.asyncio
async def test_unattested_account_calls_cannot_use_legacy_transport_fallback(admission, monkeypatch):
    channel, socket, token = admission
    dispatch = AsyncMock()
    monkeypatch.setattr(channel, '_dispatch_rpc', dispatch)
    await channel._handle_rpc(socket, {'id': 'legacy', 'method': 'health', 'params': {'voiceAccess': token()}})
    dispatch.assert_not_awaited()
    assert socket.sent[-1]['error']['code'] == 'VOICE_AUTH_UNAVAILABLE'


@pytest.mark.asyncio
async def test_forged_browser_handshake_and_unwrapped_lifecycle_do_not_change_identity(admission):
    channel, socket, _ = admission
    original = channel._relay_authority
    await channel._handle_relay_message(socket, {'type': 'ready', 'sessionId': 'browser-a',
                                                'capabilities': ['voice-relay-authority-v1'],
                                                'relayAuthority': {'key': 'b' * 64}})
    await relay_rpc(channel, socket, {'id': 'shared', 'method': 'health', 'sessionId': 'browser-a', 'params': {}}, uid='account-a')
    await channel._handle_relay_message(socket, {'type': 'browser-disconnected', 'sessionId': 'browser-a'})
    assert channel._relay_authority is original
    assert channel._relay_principals['browser-a'].uid == 'account-a'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['account', 'expiry', 'disconnect', 'link'])
async def test_delayed_private_reply_is_withheld_after_connection_authority_changes(admission, monkeypatch, change):
    channel, socket, token = admission
    entered, release = asyncio.Event(), asyncio.Event()

    async def dispatch(reply, request):
        entered.set()
        await release.wait()
        await reply.send(json.dumps({'type': 'rpc', 'id': request['id'], 'sessionId': 'browser-a',
                                     'result': {'content': 'private account-a reply'}}))

    monkeypatch.setattr(channel, '_dispatch_rpc', dispatch)
    task = asyncio.create_task(relay_rpc(channel, socket, {'id': 'slow', 'method': 'health', 'sessionId': 'browser-a',
                                                          'params': {'voiceAccess': token({'sub': 'account-a'})}}, uid='account-a'))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        if change == 'account':
            channel._decode_relay_message(socket, relay_frame(channel, {'type': 'browser-connected', 'sessionId': 'browser-a'},
                                                              uid='account-b', kind='connected'))
        elif change == 'expiry':
            monkeypatch.setattr(channel._relay_authority, '_now', lambda: time.time() + 600)
        elif change == 'disconnect':
            await channel._handle_relay_message(socket, relay_frame(channel, {'type': 'browser-disconnected', 'sessionId': 'browser-a'},
                                                                     uid='account-a', kind='disconnected'))
        else:
            channel._ws = object()
    finally:
        release.set()
        await task
    assert 'private account-a reply' not in repr(socket.sent)
    if change != 'link':
        assert socket.sent[-1]['error']['code'] == 'VOICE_AUTH_REQUIRED'


@pytest.mark.asyncio
async def test_corrupt_or_replayed_envelopes_cannot_dispatch_twice(admission, monkeypatch):
    channel, socket, _ = admission
    dispatch = AsyncMock()
    monkeypatch.setattr(channel, '_dispatch_rpc', dispatch)
    frame = relay_frame(channel, {'id': 'once', 'method': 'health', 'params': {}}, uid='account-a')
    forged = copy.deepcopy(frame)
    forged['body'] = forged['body'].replace('health', 'sessions.delete')
    await channel._handle_relay_message(socket, forged)
    dispatch.assert_not_awaited()
    await channel._handle_relay_message(socket, frame)
    await channel._handle_relay_message(socket, frame)
    assert dispatch.await_count == 1


@pytest.mark.asyncio
async def test_private_reply_stamps_the_verified_origin_and_shortest_expiry(admission, monkeypatch):
    channel, socket, token = admission

    async def dispatch(reply, request):
        await reply.send(json.dumps({'type': 'rpc', 'id': request['id'], 'sessionId': 'wrong-browser',
                                     'result': {'content': 'private'}, 'voiceDelivery': {'userId': 'forged'}}))

    monkeypatch.setattr(channel, '_dispatch_rpc', dispatch)
    expires = int(time.time()) + 30
    await relay_rpc(channel, socket, {'id': 'private', 'method': 'health', 'sessionId': 'browser-a',
                                     'params': {'voiceAccess': token({'sub': 'account-a', 'exp': expires})}}, uid='account-a')
    assert socket.sent[-1]['sessionId'] == 'browser-a'
    assert socket.sent[-1]['voiceDelivery'] == {'version': 1, 'linkId': 'test-link', 'userId': 'account-a',
                                              'sessionId': 'browser-a', 'expiresAt': expires}


@pytest.mark.asyncio
async def test_shared_rpc_does_not_acquire_private_delivery_authority(admission, monkeypatch):
    channel, socket, _ = admission

    async def dispatch(reply, request):
        await reply.send(json.dumps({'type': 'rpc', 'id': request['id'], 'result': {'ok': True}}))

    monkeypatch.setattr(channel, '_dispatch_rpc', dispatch)
    await relay_rpc(channel, socket, {'id': 'shared', 'method': 'health', 'params': {}}, uid='account-a')
    assert 'voiceDelivery' not in socket.sent[-1]


@pytest.mark.asyncio
async def test_real_core_relay_connection_negotiates_before_account_rpc_and_keeps_receiving(admission, monkeypatch):
    import websockets

    from flowly.live_voice.authority import RequestOwner, current_request_owner
    from flowly.live_voice.events import current_event_access
    from tests.relay_voice_helpers import TEST_RELAY_KEY

    channel, _, token = admission
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def dispatch(reply, request):
        observed.append((current_request_owner(), current_event_access()))
        entered.set()
        await release.wait()
        await reply.send(json.dumps({'type': 'rpc', 'id': request['id'], 'sessionId': 'browser-a', 'result': {'ok': True}}))

    monkeypatch.setattr(channel, '_dispatch_rpc', dispatch)
    conversation = asyncio.get_running_loop().create_future()

    async def relay(socket):
        try:
            await socket.send(json.dumps({'type': 'ready', 'capabilities': ['voice-relay-authority-v1'],
                                          'relayAuthority': {'version': 1, 'key': TEST_RELAY_KEY,
                                                             'serverId': 'server-a', 'linkId': 'test-link'}}))
            assert json.loads(await socket.recv()) == {'type': 'relay.authority.enable', 'version': 1, 'linkId': 'test-link'}
            await socket.send(json.dumps({'type': 'relay.authority.enabled', 'version': 1, 'linkId': 'test-link'}))
            await socket.send(json.dumps(relay_frame(channel, {'id': 'slow', 'method': 'profiles.rpc', 'sessionId': 'browser-a',
                                                                'params': {'voiceAccess': token({'sub': 'account-a'})}}, uid='account-a')))
            await asyncio.wait_for(entered.wait(), 1)
            await socket.send(json.dumps(relay_frame(channel, {'type': 'ping', 'timestamp': 7, 'sessionId': 'browser-a'}, uid='account-a')))
            assert json.loads(await asyncio.wait_for(socket.recv(), 1)) == {'type': 'pong', 'timestamp': 7, 'sessionId': 'browser-a'}
            release.set()
            assert json.loads(await asyncio.wait_for(socket.recv(), 1))['result'] == {'ok': True}
            conversation.set_result(True)
        except BaseException as error:
            release.set()
            if not conversation.done():
                conversation.set_exception(error)

    async with websockets.serve(relay, '127.0.0.1', 0) as server:
        channel.config.relay_url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/relay'
        channel.config.auth_token = 'local-relay-test-public-secret-at-least-32-bytes'
        channel._running = True
        await asyncio.wait_for(channel._connect_and_run(), 4)
        await conversation
    assert observed == [(RequestOwner('account-a'), None)]
    assert channel._ws is None
    assert channel._relay_authority is None
    assert channel._relay_principals == {}
