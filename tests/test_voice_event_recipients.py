"""Live delivery requires the event's original owner and a current recipient."""
import asyncio
import json
import time

import pytest

import flowly.profile as profiles
from flowly.artifacts.store import ArtifactStore
from flowly.channels import feature_rpc
from flowly.gateway.server import GatewayServer
from flowly.live_voice.access import VoiceAccessVerifier, VoicePrincipal
from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.live_voice.events import EventAccess, EventRecipients, event_access_scope
from flowly.session.control_access import SessionControlScope
from flowly.session.manager import Session, SessionManager
from tests.test_voice_access import HOST
from tests.test_voice_access import fixture as access_fixture

A, B = RequestOwner('event-account-a'), RequestOwner('event-account-b')
KEY = 'desktop:voice-work:event-task'
SHARED = 'desktop:event-shared'


class Socket:
    def __init__(self):
        self.closed = False
        self.sent = []
        self.before_send = None
        self.changed = asyncio.Event()

    async def send_json(self, value):
        if self.before_send is not None:
            await self.before_send()
        self.sent.append(json.loads(json.dumps(value)))
        self.changed.set()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    sessions = SessionManager(tmp_path / 'workspace')
    store = ArtifactStore(sessions.sessions_dir.parent / 'artifacts.sqlite')
    with request_owner_scope(A):
        sessions.reserve_voice_work(KEY)
        artifact = store.create('markdown', 'Private output', 'private artifact text', session_key=KEY)
    sessions.save(Session(key=SHARED))
    server = GatewayServer(sessions=sessions, artifact_store=store)
    monkeypatch.setattr(feature_rpc, '_artifact_store', lambda: store)
    monkeypatch.setattr(feature_rpc, '_artifact_change_callback', server._broadcast_artifact_event)
    token, _, keys, _, now = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    sockets = {owner: Socket() for owner in (A, B, HOST_OWNER)}
    server._ws_clients = {str(index): socket for index, socket in enumerate(sockets.values())}
    yield server, sessions, store, artifact, sockets, token, now
    server.chat_commands.close()
    store.close()


async def rpc(server, socket, token, owner, method='health', **params):
    access = {'voiceAccess': token({'sub': owner.uid})} if owner.uid else {}
    await server._handle_ws_rpc(socket, 'client', {'id': method, 'method': method, 'params': {**params, **access}})


async def bind_all(setup):
    server, _, _, _, sockets, token, _ = setup
    for owner, socket in sockets.items():
        await rpc(server, socket, token, owner)
        assert 'result' in socket.sent[-1]
        socket.sent.clear()
        socket.changed.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['tool', 'goal', 'artifact', 'plan', 'stream'])
async def test_private_events_only_reach_a_valid_account_recipient(setup, kind):
    await bind_all(setup)
    server, _, _, artifact, sockets, _, _ = setup
    if kind == 'tool':
        await server.broadcast_tool_event('tool.start', {'sessionKey': KEY, 'content': 'private tool'})
    elif kind == 'goal':
        await server.broadcast_goal_updated(KEY, {'goal': 'private goal'})
    elif kind == 'artifact':
        await server._broadcast_artifact_event('artifact.updated', artifact)
    elif kind == 'plan':
        await server.broadcast_event('plan.updated', {'sessionKey': KEY, 'plan': 'private plan'})
    else:
        server.bind_session_ws(KEY, sockets[B])
        await server._session_send(KEY, sockets[A], {'type': 'event', 'event': 'chat',
                                                     'data': {'sessionKey': KEY, 'content': 'private stream'}})
        assert sockets[B].sent == []
        return
    assert len(sockets[A].sent) == 1
    assert sockets[B].sent == []
    assert sockets[HOST_OWNER].sent == []
    assert A.uid not in json.dumps(sockets[A].sent)


@pytest.mark.asyncio
async def test_shared_health_rpc_does_not_erase_account_subscription_but_clear_does(setup):
    await bind_all(setup)
    server, _, _, _, sockets, token, _ = setup
    await rpc(server, sockets[A], token, HOST_OWNER)
    sockets[A].sent.clear()
    await server.broadcast_goal_updated(KEY, {'goal': 'still connected'})
    assert len(sockets[A].sent) == 1
    await rpc(server, sockets[A], token, HOST_OWNER, 'voice.events.clear')
    assert sockets[A].sent[-1]['result'] == {'cleared': True}
    sockets[A].sent.clear()
    await server.broadcast_goal_updated(KEY, {'goal': 'after logout'})
    assert sockets[A].sent == []


@pytest.mark.asyncio
async def test_expired_lease_and_backward_wall_clock_never_extend_private_delivery(setup):
    server, _, _, _, sockets, _, now = setup
    wall, monotonic = [float(now)], [100.0]
    recipients = EventRecipients(now=lambda: wall[0], monotonic=lambda: monotonic[0])
    server._event_recipients = recipients
    principal = VoicePrincipal(A.uid, HOST, now + 10, 'credential')
    assert await recipients.bind(sockets[A], recipients.begin(sockets[A]), principal)
    await server.broadcast_goal_updated(KEY, {'goal': 'valid'})
    assert len(sockets[A].sent) == 1
    sockets[A].sent.clear()
    wall[0] -= 100
    monotonic[0] += 11
    await server.broadcast_goal_updated(KEY, {'goal': 'expired'})
    assert sockets[A].sent == []
    await server.broadcast_goal_updated(SHARED, {'goal': 'shared'})
    assert len(sockets[A].sent) == 1


@pytest.mark.asyncio
async def test_event_binding_does_not_change_owner_between_fanout_recipients(setup):
    await bind_all(setup)
    server, sessions, _, _, sockets, _, _ = setup

    async def replace():
        sockets[A].before_send = None
        sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))

    sockets[A].before_send = replace
    await server.broadcast_event('plan.updated', {'sessionKey': KEY, 'plan': 'original account plan'})
    assert len(sockets[A].sent) == 1
    assert sockets[B].sent == []


@pytest.mark.asyncio
async def test_delayed_account_reply_is_replaced_after_socket_switches_accounts(setup):
    await bind_all(setup)
    server, _, _, _, sockets, token, _ = setup
    await rpc(server, sockets[A], token, B)
    sockets[A].sent.clear()
    with request_owner_scope(A):
        await server._ws_rpc_reply(sockets[A], 'old-request', {'secret': 'old account result'})
    assert sockets[A].sent == [{'type': 'rpc', 'id': 'old-request', 'error': {
        'code': 'VOICE_AUTH_REQUIRED', 'message': 'The account connection changed or expired.'}}]


@pytest.mark.asyncio
async def test_invalid_certificate_revokes_previous_private_delivery(setup):
    await bind_all(setup)
    server, _, _, _, sockets, _, _ = setup
    await server._handle_ws_rpc(sockets[A], 'client', {'id': 'bad', 'method': 'health', 'params': {'voiceAccess': 'invalid'}})
    sockets[A].sent.clear()
    await server.broadcast_goal_updated(KEY, {'goal': 'no longer subscribed'})
    assert sockets[A].sent == []


@pytest.mark.asyncio
async def test_older_verification_cannot_overwrite_newer_account_binding(setup, monkeypatch):
    server, _, _, _, sockets, _, now = setup
    entered, finish = asyncio.Event(), asyncio.Event()

    async def verify(params, **_):
        if params['voiceAccess'] == 'slow-a':
            entered.set()
            await finish.wait()
            owner = A
        else:
            owner = B
        return owner, {}, VoicePrincipal(owner.uid, HOST, now + 300, 'credential')

    monkeypatch.setattr(feature_rpc, 'resolve_voice_access', verify)
    old = asyncio.create_task(server._handle_ws_rpc(sockets[A], 'client', {
        'id': 'old', 'method': 'health', 'params': {'voiceAccess': 'slow-a'}}))
    await asyncio.wait_for(entered.wait(), 1)
    await server._handle_ws_rpc(sockets[A], 'client', {'id': 'new', 'method': 'health', 'params': {'voiceAccess': 'fast-b'}})
    finish.set()
    await old
    state = server.event_recipients.get(sockets[A])
    assert server.event_recipients.owner(state) == B
    assert next(row for row in sockets[A].sent if row['id'] == 'old')['error']['code'] == 'VOICE_AUTH_REQUIRED'


@pytest.mark.asyncio
async def test_explicit_original_close_scope_retires_card_without_adopting_new_owner(setup):
    await bind_all(setup)
    server, sessions, _, _, sockets, _, _ = setup
    with request_owner_scope(A):
        original = SessionControlScope.capture(KEY)
    sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    with event_access_scope(EventAccess(scopes=(original,), canonical=False)):
        await server.broadcast_approval_closed('pending-a', 'cancelled', KEY)
    assert len(sockets[A].sent) == 1
    assert sockets[B].sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['approval', 'clarify'])
@pytest.mark.parametrize('sessionless', [False, True])
async def test_real_pending_manager_retains_scope_through_close_after_registry_removal(setup, kind, sessionless):
    from flowly.clarify.manager import ClarifyManager
    from flowly.clarify.types import ClarifyRequest
    from flowly.exec.approval_manager import ApprovalManager
    from flowly.exec.types import ExecRequest, PendingApproval

    await bind_all(setup)
    server, sessions, _, _, sockets, _, _ = setup
    key = None if sessionless else KEY
    args = dict(id='private-request', session_key=key, created_at=time.time(), expires_at=time.time() + 30)
    if kind == 'approval':
        manager = ApprovalManager()
        pending = PendingApproval(request=ExecRequest(command='private operation', session_key=key), **args)
        manager.add_notify_callback(server.broadcast_approval_request)
        manager.add_close_callback(server.broadcast_approval_closed)
    else:
        manager = ClarifyManager()
        pending = ClarifyRequest(question='private question', **args)
        manager.add_notify_callback(server.broadcast_clarify_request)
        manager.add_close_callback(server.broadcast_clarify_closed)
    with request_owner_scope(A):
        task = asyncio.create_task(manager.request_and_wait(pending))
    await asyncio.wait_for(sockets[A].changed.wait(), 1)
    try:
        assert len(sockets[A].sent) == 1
        assert sockets[B].sent == []
        assert sockets[HOST_OWNER].sent == []
        if key:
            sessions.mutate(key, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(sockets[A].sent) == 2
    assert sockets[A].sent[-1]['event'].endswith('.closed')
    assert sockets[B].sent == []
    assert manager._control_scopes == {}


@pytest.mark.asyncio
async def test_artifact_delete_keeps_original_event_owner_after_row_is_gone(setup):
    await bind_all(setup)
    server, _, store, artifact, sockets, token, _ = setup
    await rpc(server, sockets[A], token, A, 'artifacts.delete', id=artifact['id'])
    events = [row for row in sockets[A].sent if row.get('event') == 'artifact.deleted']
    assert len(events) == 1
    assert store.get(artifact['id']) is None
    assert sockets[B].sent == []
    assert sockets[HOST_OWNER].sent == []


@pytest.mark.asyncio
async def test_delayed_run_event_uses_persisted_original_owner_and_rejects_redirected_target(setup):
    await bind_all(setup)
    server, sessions, _, _, sockets, _, _ = setup
    with request_owner_scope(A):
        server.chat_commands.accept(KEY, 'private-run', {'message': 'private work'})
    await server.broadcast_tool_event('tool.complete', {'sessionKey': SHARED, 'runId': 'private-run', 'result': 'private'})
    assert all(not socket.sent for socket in sockets.values())
    sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    await server.broadcast_tool_event('tool.complete', {'sessionKey': KEY, 'runId': 'private-run', 'result': 'private'})
    assert all(not socket.sent for socket in sockets.values())


@pytest.mark.asyncio
async def test_slow_recipient_has_a_bounded_send_and_loses_its_lease(setup):
    await bind_all(setup)
    server, _, _, _, sockets, _, _ = setup
    server._event_send_timeout = 0.01

    async def stalled():
        await asyncio.Future()

    sockets[A].before_send = stalled
    await asyncio.wait_for(server.broadcast_goal_updated(KEY, {'goal': 'private'}), 1)
    assert server.event_recipients.get(sockets[A]).retired
    assert sockets[A].sent == []


@pytest.mark.asyncio
async def test_private_reverse_request_does_not_follow_a_changed_recipient(setup):
    await bind_all(setup)
    server, _, _, _, sockets, token, _ = setup
    await rpc(server, sockets[A], token, B)
    sockets[A].sent.clear()
    with request_owner_scope(A):
        await server._ws_send(sockets[A], {'type': 'profile.request', 'id': 'broker', 'prompt': 'private prompt'})
    assert sockets[A].sent == []


@pytest.mark.asyncio
async def test_profile_events_resolve_the_named_profiles_session_root(setup, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from flowly.gateway.server import _ProfileClientSubscription

    await bind_all(setup)
    server, primary, _, _, sockets, _, _ = setup
    primary.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    profile_home = tmp_path / 'named-profile'
    with monkeypatch.context() as context:
        context.setenv('FLOWLY_HOME', str(profile_home))
        named = SessionManager(tmp_path / 'named-workspace')
        with request_owner_scope(A):
            named.reserve_voice_work(KEY)
    monkeypatch.setattr(profiles, 'describe_profile', lambda _: SimpleNamespace(path=profile_home))
    server._profile_client_subscriptions = {
        client: _ProfileClientSubscription(conversations={('worker', KEY)}) for client in server._ws_clients}
    await server._broadcast_profile_host_event({'profile': 'worker', 'type': 'chat',
                                                'data': {'sessionKey': KEY, 'content': 'private profile reply'}})
    assert len(sockets[A].sent) == 1
    assert sockets[B].sent == []


@pytest.mark.asyncio
async def test_real_socket_replacement_retires_old_connection_without_inheriting_its_account(setup):
    import aiohttp

    _, sessions, store, _, _, token, _ = setup

    async def on_chat(*_args, **_kwargs):
        return 'unused'

    server = GatewayServer(host='127.0.0.1', port=0, sessions=sessions, artifact_store=store,
                           auth_token='g' * 48, require_loopback_auth=True, advertise_control=False,
                           on_chat_message=on_chat)
    await server.start()
    try:
        async with aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + 'g' * 48}) as client:
            url = f'http://127.0.0.1:{server.port}/ws?clientId=stable&token=' + 'g' * 48
            async with client.ws_connect(url) as first:
                await first.send_json({'type': 'rpc', 'id': 'a', 'method': 'voice.events.bind',
                                       'params': {'voiceAccess': token({'sub': A.uid})}})
                assert (await first.receive_json(timeout=1))['result']['bound']
                old = server._ws_clients['stable']
                server.bind_session_ws(KEY, old)
                async with client.ws_connect(url) as second:
                    await second.send_json({'type': 'rpc', 'id': 'b', 'method': 'health', 'params': {}})
                    assert (await second.receive_json(timeout=1))['result']['ok']
                    assert server.event_recipients.owner(server.event_recipients.get(server._ws_clients['stable'])) == HOST_OWNER
                    assert server.event_recipients.get(old).retired
                    await server.broadcast_goal_updated(KEY, {'goal': 'private'})
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(first.receive(), 0.03)
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(second.receive(), 0.03)
    finally:
        await server.stop()
        server.chat_commands.close()
