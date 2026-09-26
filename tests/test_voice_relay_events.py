"""Account event leases and replay retain original authority across reconnects."""
import asyncio
import json
import time

import pytest

from flowly.artifacts.store import ArtifactStore
from flowly.channels import feature_rpc
from flowly.gateway.server import GatewayServer
from flowly.live_voice.authority import RequestOwner, request_owner_scope
from flowly.live_voice.event_mirror import mirror_event
from flowly.live_voice.events import EventAccess, event_access_scope
from flowly.session.control_access import SessionControlScope
from flowly.session.manager import SessionManager
from tests.relay_voice_helpers import prepare_relay, relay_frame, relay_rpc
from tests.test_chat_command_transport import Socket
from tests.test_voice_relay_authority import admission as _admission_fixture

admission = _admission_fixture

A = RequestOwner('account-a')


async def bind(channel, socket, token, session='browser-a', uid='account-a'):
    start = len(socket.sent)
    await relay_rpc(channel, socket, {'id': 'bind', 'method': 'voice.events.bind', 'sessionId': session,
                                     'params': {'voiceAccess': token({'sub': uid})}}, uid=uid)
    reply = next(frame for frame in socket.sent[start:] if frame.get('id') == 'bind')
    assert reply['result']['bound'] is True


async def publish(channel, text='private', session=None, access=None):
    frame = {'type': 'event', 'event': 'goal.updated', 'data': {'text': text}}
    if session:
        frame['sessionId'] = session
    with event_access_scope(access or EventAccess(owner=A)):
        await channel._send_or_queue(json.dumps(frame))


def events(socket):
    return [frame for frame in socket.sent if frame['type'] == 'event']


@pytest.mark.asyncio
async def test_private_fanout_requires_each_browser_certificate_and_matching_relay_account(admission):
    channel, socket, token = admission
    await bind(channel, socket, token)
    await bind(channel, socket, token, 'browser-b', 'account-b')
    await relay_rpc(channel, socket, {'id': 'shared', 'method': 'health', 'sessionId': 'unbound-a', 'params': {}}, uid=A.uid)
    socket.sent.clear()
    await publish(channel)
    assert [frame['sessionId'] for frame in events(socket)] == ['browser-a']
    assert events(socket)[0]['voiceDelivery']['userId'] == A.uid


@pytest.mark.asyncio
async def test_clear_revokes_delivery_but_background_health_does_not(admission):
    channel, socket, token = admission
    await bind(channel, socket, token)
    await relay_rpc(channel, socket, {'id': 'health', 'method': 'health', 'sessionId': 'browser-a', 'params': {}}, uid=A.uid)
    await publish(channel, 'before clear')
    await relay_rpc(channel, socket, {'id': 'clear', 'method': 'voice.events.clear', 'sessionId': 'browser-a', 'params': {}}, uid=A.uid)
    await publish(channel, 'after clear')
    assert [frame['data']['text'] for frame in events(socket)] == ['before clear']
    assert len(channel._outbound_queue) == 1
    await bind(channel, socket, token)
    assert [frame['data']['text'] for frame in events(socket)] == ['before clear', 'after clear']
    assert channel._outbound_queue == []


@pytest.mark.asyncio
async def test_invalid_certificate_clears_an_existing_recipient(admission):
    channel, socket, token = admission
    await bind(channel, socket, token)
    await relay_rpc(channel, socket, {'id': 'invalid', 'method': 'health', 'sessionId': 'browser-a',
                                     'params': {'voiceAccess': 'bad'}}, uid=A.uid)
    await publish(channel)
    assert events(socket) == []
    assert socket.sent[-1]['error']['code'] == 'VOICE_AUTH_REQUIRED'


@pytest.mark.asyncio
async def test_slow_certificate_cannot_rebind_after_later_clear(admission, monkeypatch):
    channel, socket, token = admission
    original = feature_rpc.resolve_voice_access
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(params):
        if params.get('voiceAccess'):
            entered.set()
            await release.wait()
        return await original(params)

    monkeypatch.setattr(feature_rpc, 'resolve_voice_access', slow)
    pending = asyncio.create_task(relay_rpc(channel, socket, {
        'id': 'slow-bind', 'method': 'voice.events.bind', 'sessionId': 'browser-a',
        'params': {'voiceAccess': token({'sub': A.uid})}}, uid=A.uid))
    await asyncio.wait_for(entered.wait(), 1)
    await relay_rpc(channel, socket, {'id': 'clear', 'method': 'voice.events.clear', 'sessionId': 'browser-a', 'params': {}}, uid=A.uid)
    release.set()
    await pending
    assert socket.sent[-1]['id'] == 'slow-bind'
    assert socket.sent[-1]['error']['code'] == 'VOICE_AUTH_REQUIRED'
    await publish(channel)
    assert events(socket) == []


@pytest.mark.asyncio
async def test_reconnect_never_replays_private_content_before_fresh_account_lease(admission):
    channel, socket, token = admission
    await bind(channel, socket, token)
    channel._ws = None
    await publish(channel)
    assert len(channel._outbound_queue) == 1
    replacement = Socket()
    prepare_relay(channel, replacement)
    await channel._flush_outbound_queue()
    assert events(replacement) == []
    await bind(channel, replacement, token, 'browser-b', 'account-b')
    assert events(replacement) == []
    await bind(channel, replacement, token, 'new-a')
    assert [frame['sessionId'] for frame in events(replacement)] == ['new-a']
    assert events(replacement)[0]['voiceDelivery']['userId'] == A.uid


@pytest.mark.asyncio
async def test_original_session_owner_is_checked_again_at_replay(admission, tmp_path, monkeypatch):
    channel, socket, token = admission
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    sessions = SessionManager(tmp_path / 'workspace')
    key = 'desktop:voice-work:relay-owned'
    with request_owner_scope(A):
        sessions.reserve_voice_work(key)
        scope = SessionControlScope.capture(key, sessions_dir=sessions.sessions_dir)
    channel._ws = None
    await publish(channel, access=EventAccess(scopes=(scope,)))
    path = scope.path
    # Model an externally recreated canonical file, bypassing normal API
    # ownership checks specifically to test the delivery-time guard.
    path.write_text(path.read_text().replace('account-a', 'account-b'))
    prepare_relay(channel, socket)
    await bind(channel, socket, token, 'browser-b', 'account-b')
    await bind(channel, socket, token)
    assert events(socket) == []
    assert channel._outbound_queue == []


@pytest.mark.asyncio
async def test_expiry_and_monotonic_clock_floor_revoke_private_delivery(admission, monkeypatch):
    channel, socket, token = admission
    await bind(channel, socket, token)
    leases = channel._relay_recipients.leases
    monkeypatch.setattr(leases, 'monotonic', lambda: time.monotonic() + 600)
    await publish(channel)
    assert events(socket) == []


@pytest.mark.asyncio
async def test_disconnect_retires_lease_even_if_same_browser_identifier_returns(admission):
    channel, socket, token = admission
    await bind(channel, socket, token)
    await channel._handle_relay_message(socket, relay_frame(channel, {'type': 'browser-disconnected', 'sessionId': 'browser-a'},
                                                             uid=A.uid, kind='disconnected'))
    await relay_rpc(channel, socket, {'id': 'back', 'method': 'health', 'sessionId': 'browser-a', 'params': {}}, uid=A.uid)
    await publish(channel)
    assert events(socket) == []


@pytest.mark.asyncio
async def test_partial_fanout_failure_replays_only_the_unsent_recipient(admission, monkeypatch):
    channel, socket, token = admission
    await bind(channel, socket, token, 'a-first')
    await bind(channel, socket, token, 'a-second')
    original = socket.send
    fail = [True]

    async def send(payload):
        frame = json.loads(payload)
        if frame.get('type') == 'event' and frame['sessionId'] == 'a-second' and fail[0]:
            raise ConnectionError('disconnected')
        await original(payload)

    monkeypatch.setattr(socket, 'send', send)
    await publish(channel)
    assert [frame['sessionId'] for frame in events(socket)] == ['a-first']
    fail[0] = False
    await channel._flush_outbound_queue()
    assert [frame['sessionId'] for frame in events(socket)] == ['a-first', 'a-second']


@pytest.mark.asyncio
async def test_queue_records_keep_authority_out_of_wire_json(admission):
    channel, socket, _ = admission
    channel._ws = None
    await publish(channel)
    pending = channel._outbound_queue[0]
    assert pending.access.owner == A
    assert A.uid not in pending.payload
    assert 'voiceDelivery' not in pending.payload


@pytest.mark.asyncio
async def test_replay_follows_rebound_canonical_conversation_only_with_fresh_lease(admission):
    channel, socket, token = admission
    await bind(channel, socket, token, 'old-browser')
    channel._ws = None
    key = 'desktop:voice-work:task'
    with event_access_scope(EventAccess(owner=A)):
        await channel._send_or_queue(json.dumps({'type': 'event', 'event': 'goal.updated', 'sessionId': 'old-browser',
                                                'data': {'sessionKey': key, 'text': 'private'}}))
    replacement = Socket()
    prepare_relay(channel, replacement)
    channel._session_key_to_relay_id[key] = 'new-browser'
    await bind(channel, replacement, token, 'unrelated-a')
    assert events(replacement) == []
    await bind(channel, replacement, token, 'new-browser')
    assert [frame['sessionId'] for frame in events(replacement)] == ['new-browser']


@pytest.mark.asyncio
async def test_shared_replay_keeps_legacy_wire_contract(admission):
    channel, socket, _ = admission
    channel._ws = None
    frame = {'type': 'event', 'event': 'status', 'data': {'state': 'ready'}}
    await channel._send_or_queue(json.dumps(frame))
    channel._ws = socket
    channel._relay_authority_enabled = False
    await channel._flush_outbound_queue()
    assert socket.sent == [frame]


@pytest.fixture
def mirror_setup(admission, tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    sessions = SessionManager(tmp_path / 'workspace')
    store = ArtifactStore(sessions.sessions_dir.parent / 'artifacts.sqlite')
    key = 'desktop:voice-work:mirrored-artifact'
    with request_owner_scope(A):
        sessions.reserve_voice_work(key)
        artifact = store.create('markdown', 'Private report', 'private artifact', session_key=key)
    gateway = GatewayServer(sessions=sessions, artifact_store=store)
    yield admission, gateway, artifact, sessions, key
    gateway.chat_commands.close()
    store.close()


@pytest.mark.asyncio
async def test_real_artifact_mirror_queues_original_authority_without_raw_relay_write(mirror_setup):
    (channel, socket, token), gateway, artifact, _, _ = mirror_setup
    channel._ws = None
    await mirror_event(gateway, channel, 'artifact.updated', artifact)
    assert len(channel._outbound_queue) == 1
    assert channel._outbound_queue[0].access.producer() == A
    assert 'voiceDelivery' not in channel._outbound_queue[0].payload
    prepare_relay(channel, socket)
    await bind(channel, socket, token, 'other-account', 'account-b')
    assert events(socket) == []
    await bind(channel, socket, token)
    assert events(socket)[0]['voiceDelivery']['userId'] == A.uid
    assert events(socket)[0]['data']['content'] == 'private artifact'


@pytest.mark.asyncio
async def test_mirror_cannot_recapture_a_different_owner_between_direct_and_relay(mirror_setup, monkeypatch):
    (channel, socket, token), gateway, artifact, sessions, key = mirror_setup
    await bind(channel, socket, token)
    await bind(channel, socket, token, 'other-account', 'account-b')
    path = sessions._get_session_path(key)

    async def direct(event):
        assert event.access.producer() == A
        path.write_text(path.read_text().replace(A.uid, 'account-b'))

    monkeypatch.setattr(gateway, '_broadcast_clients', direct)
    await mirror_event(gateway, channel, 'artifact.updated', artifact)
    assert events(socket) == []
    assert channel._outbound_queue == []
