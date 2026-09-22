"""IDs alone cannot read, resolve or cancel another account's work."""
import asyncio
import time
from unittest.mock import Mock

import pytest

import flowly.profile as profiles
from flowly.agent import inflight
from flowly.agent.run_abort import CURRENT_RUN_ID, RunAbortController
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.web import WebChannel
from flowly.clarify.manager import ClarifyManager
from flowly.clarify.types import ClarifyRequest
from flowly.config.schema import WebChannelConfig
from flowly.exec.approval_manager import ApprovalManager
from flowly.exec.types import ExecRequest, PendingApproval
from flowly.gateway.server import GatewayServer
from flowly.live_voice.access import VoiceAccessVerifier
from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.session.commands import ChatCommandStore
from flowly.session.control_access import run_control_guard
from flowly.session.manager import Session, SessionManager
from flowly.session.ownership import SessionAccessError
from tests.test_chat_command_transport import Socket
from tests.test_voice_access import HOST
from tests.test_voice_access import fixture as access_fixture

A, B = RequestOwner('account-a'), RequestOwner('account-b')
KEY = 'desktop:voice-work:private-task'
SHARED = 'desktop:shared'


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    manager = SessionManager(tmp_path / 'workspace')
    with request_owner_scope(A):
        manager.reserve_voice_work(KEY)
    manager.save(Session(key=SHARED))
    return manager


def pending(kind, key=KEY, request_id='request-1'):
    args = dict(id=request_id, session_key=key, created_at=time.time(), expires_at=time.time() + 30)
    return (PendingApproval(request=ExecRequest(command='private action', session_key=key), **args)
            if kind == 'approval' else ClarifyRequest(question='private question', **args))


async def begin(manager, item, owner=A):
    with request_owner_scope(owner):
        token = CURRENT_RUN_ID.set('run-private')
        try:
            task = asyncio.create_task(manager.request_and_wait(item))
        finally:
            CURRENT_RUN_ID.reset(token)
    await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['approval', 'clarify'])
async def test_pending_manager_filters_and_resolves_only_the_original_owner(sessions, kind):
    manager = ApprovalManager() if kind == 'approval' else ClarifyManager()
    item = pending(kind)
    task = await begin(manager, item)
    try:
        for owner in (B, HOST_OWNER):
            with request_owner_scope(owner):
                assert manager.list_pending() == []
                assert manager.get_pending(item.id) is None
                assert manager.resolve(item.id, 'deny' if kind == 'approval' else 'wrong answer') is False
            assert not task.done()
        with request_owner_scope(A):
            assert manager.get_pending(item.id) is item
            assert manager.list_pending() == [item]
            assert manager.resolve(item.id, 'allow-once' if kind == 'approval' else 'right answer') is True
            assert manager.resolve(item.id, 'deny' if kind == 'approval' else 'second answer') is False
        assert await task == ('allow-once' if kind == 'approval' else 'right answer')
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['approval', 'clarify'])
@pytest.mark.parametrize('change', ['canonical_owner', 'pending_target', 'deleted'])
async def test_stale_pending_id_cannot_adopt_a_replacement_owner_or_target(sessions, kind, change):
    manager = ApprovalManager() if kind == 'approval' else ClarifyManager()
    item = pending(kind)
    task = await begin(manager, item)
    try:
        if change == 'canonical_owner':
            sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
        elif change == 'pending_target':
            item.session_key = SHARED
        else:
            sessions.delete(KEY)
        for owner in (A, B, HOST_OWNER):
            with request_owner_scope(owner):
                assert manager.get_pending(item.id) is None
                assert manager.resolve(item.id, 'deny' if kind == 'approval' else 'unwanted') is False
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['approval', 'clarify'])
async def test_ordinary_shared_session_requests_preserve_existing_visibility(sessions, kind):
    manager = ApprovalManager() if kind == 'approval' else ClarifyManager()
    item = pending(kind, key=SHARED)
    task = await begin(manager, item, HOST_OWNER)
    try:
        for owner in (A, B, HOST_OWNER):
            with request_owner_scope(owner):
                assert manager.get_pending(item.id) is item
        with request_owner_scope(B):
            assert manager.resolve(item.id, 'deny' if kind == 'approval' else 'shared answer')
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
@pytest.mark.parametrize('source', ['accepted', 'inflight', 'unknown', 'conflicting', 'duplicate_live', 'unbound_live'])
async def test_abort_derives_the_actual_session_before_callback(sessions, monkeypatch, transport, source):
    token, _, keys, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    server = GatewayServer(sessions=sessions)
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    surface = server if transport == 'gateway' else channel
    controller = RunAbortController()
    callback = Mock(side_effect=controller.request)
    server.on_chat_abort = callback
    channel._abort_callback = callback
    if source in {'accepted', 'conflicting', 'duplicate_live', 'unbound_live'}:
        with request_owner_scope(A):
            surface.chat_commands.accept(KEY, 'run-private', {'message': 'private task'})
    if source in {'inflight', 'conflicting'}:
        with request_owner_scope(A):
            inflight.begin(SHARED if source == 'conflicting' else KEY, 'run-private')
    if source == 'duplicate_live':
        with request_owner_scope(A):
            inflight.begin(KEY, 'run-private')
            inflight.begin(SHARED, 'run-private')
    elif source == 'unbound_live':
        inflight._runs[SHARED] = {'runId': 'run-private'}
    socket = Socket()

    async def call(owner, **extra):
        access = {'voiceAccess': token({'sub': owner.uid})} if owner.uid else {}
        frame = {'id': 'abort', 'method': 'chat.abort', 'sessionId': 'relay-1',
                 'params': {'runId': 'run-private', **access, **extra}}
        if transport == 'gateway':
            await server._handle_ws_rpc(socket, 'client', frame)
        else:
            from tests.relay_voice_helpers import relay_rpc

            await relay_rpc(channel, socket, frame, uid=owner.uid)

    try:
        for owner in (B, HOST_OWNER):
            for supplied in ({}, {'sessionKey': SHARED}):
                await call(owner, **supplied)
                assert socket.sent[-1]['error']['code'] == 'NOT_FOUND'
        callback.assert_not_called()
        assert not controller.is_requested('run-private')
        await call(A)
        if source in {'unknown', 'conflicting', 'duplicate_live', 'unbound_live'}:
            assert socket.sent[-1]['error']['code'] == 'NOT_FOUND'
            callback.assert_not_called()
        else:
            assert socket.sent[-1]['result']['ok'] is True
            callback.assert_called_once_with('run-private')
    finally:
        inflight.finish(KEY, 'run-private')
        inflight.finish(SHARED, 'run-private')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['approval', 'clarify'])
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
async def test_real_rpc_request_ids_use_the_managers_original_owner(sessions, monkeypatch, kind, transport):
    import flowly.clarify.manager as clarifies
    import flowly.exec.approval_manager as approvals

    token, _, keys, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    manager = ApprovalManager() if kind == 'approval' else ClarifyManager()
    monkeypatch.setattr(approvals if kind == 'approval' else clarifies, '_manager', manager)
    item = pending(kind)
    task = await begin(manager, item)
    socket = Socket()
    server = GatewayServer(sessions=sessions)
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    prefix = 'exec.approval' if kind == 'approval' else 'agent.clarify'

    async def call(owner, method, extra):
        access = {'voiceAccess': token({'sub': owner.uid})} if owner.uid else {}
        frame = {'id': 'control', 'method': method, 'sessionId': 'relay-1', 'params': {**access, **extra}}
        if transport == 'gateway':
            await server._handle_ws_rpc(socket, 'client', frame)
        else:
            from tests.relay_voice_helpers import relay_rpc

            await relay_rpc(channel, socket, frame, uid=owner.uid)

    try:
        for owner in (B, HOST_OWNER):
            if transport == 'gateway':
                await call(owner, prefix + '.list', {})
                assert socket.sent[-1]['result'] == ({'approvals': []} if kind == 'approval' else {'clarifies': []})
            await call(owner, prefix + '.resolve', {'id': item.id, 'sessionKey': SHARED,
                       **({'decision': 'allow-once'} if kind == 'approval' else {'answer': 'unwanted'})})
            assert socket.sent[-1].get('result', {}).get('ok') is not True
        assert not task.done()
        assert 'private action' not in repr(socket.sent) and 'private question' not in repr(socket.sent)
        await call(A, prefix + '.resolve', {'id': item.id,
                   **({'decision': 'allow-once'} if kind == 'approval' else {'answer': 'right answer'})})
        assert socket.sent[-1]['result']['ok'] is True
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_persisted_command_owner_survives_restart_and_cannot_adopt_recreated_session(sessions):
    path = sessions.sessions_dir.parent / 'commands.sqlite3'
    commands = ChatCommandStore(path)
    with request_owner_scope(A):
        commands.accept(KEY, 'run-private', {'message': 'private task'})
    commands.close()
    restarted = ChatCommandStore(path, owner_id='new-process')
    try:
        with request_owner_scope(A), run_control_guard(restarted, {'runId': 'run-private'}):
            pass
        sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
        for owner in (A, B, HOST_OWNER):
            with request_owner_scope(owner), pytest.raises(SessionAccessError):
                with run_control_guard(restarted, {'runId': 'run-private'}):
                    pytest.fail('A stale run must not adopt the replacement owner')
    finally:
        restarted.close()


def test_original_command_receipt_cannot_be_adopted_after_owner_replacement(sessions):
    commands = ChatCommandStore(':memory:')
    try:
        with request_owner_scope(A):
            commands.accept(KEY, 'run-private', {'message': 'private task'})
        sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
        with request_owner_scope(B):
            assert commands.lookup(KEY, 'run-private') is None
            with pytest.raises(SessionAccessError):
                commands.accept(KEY, 'run-private', {'message': 'private task'})
        assert commands.control_scope('run-private').owner == {'kind': 'account', 'uid': A.uid}
    finally:
        commands.close()


@pytest.mark.parametrize('raw', [None, 'null', '{}', '{"kind":"internal"}', '{"kind":"account","uid":null}'])
def test_corrupt_or_legacy_command_authority_cannot_adopt_an_account_session(sessions, raw):
    commands = ChatCommandStore(':memory:')
    with request_owner_scope(A):
        commands.accept(KEY, 'run-private', {'message': 'private task'})
    commands._db.execute('UPDATE chat_commands SET voice_owner_json = ? WHERE run_id = ?', (raw, 'run-private'))
    try:
        with request_owner_scope(A), pytest.raises(SessionAccessError):
            with run_control_guard(commands, {'runId': 'run-private'}):
                pytest.fail('The original owner could not be verified')
    finally:
        commands.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['approval', 'clarify'])
async def test_sessionless_account_request_is_not_shared(sessions, kind):
    manager = ApprovalManager() if kind == 'approval' else ClarifyManager()
    item = pending(kind, key=None)
    task = await begin(manager, item)
    try:
        for owner in (B, HOST_OWNER):
            with request_owner_scope(owner):
                assert manager.list_pending() == []
        with request_owner_scope(A):
            assert manager.get_pending(item.id) is item
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['approval', 'clarify'])
async def test_cancel_during_notification_cleans_the_pending_control(sessions, kind):
    manager = ApprovalManager() if kind == 'approval' else ClarifyManager()
    entered = asyncio.Event()
    closed = []

    async def notify(_):
        entered.set()
        await asyncio.Future()

    async def close(*args):
        closed.append(args)

    manager.add_notify_callback(notify)
    manager.add_close_callback(close)
    item = pending(kind)
    task = await begin(manager, item)
    await asyncio.wait_for(entered.wait(), 1)
    future = manager._futures[item.id]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert manager.list_pending() == []
    assert manager._futures == {}
    assert manager._control_scopes == {}
    assert future.cancelled()
    assert closed == [(item.id, 'cancelled', KEY)]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['approval', 'clarify'])
async def test_duplicate_pending_id_cannot_replace_the_original_waiter(sessions, kind):
    manager = ApprovalManager() if kind == 'approval' else ClarifyManager()
    item = pending(kind)
    task = await begin(manager, item)
    future = manager._futures[item.id]
    try:
        with request_owner_scope(HOST_OWNER), pytest.raises(ValueError, match='already pending'):
            await manager.request_and_wait(pending(kind, SHARED))
        assert manager._futures[item.id] is future
        with request_owner_scope(A):
            assert manager.resolve(item.id, 'deny' if kind == 'approval' else 'original answer')
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
@pytest.mark.parametrize('failure', ['rejected', 'raised', 'missing'])
async def test_failed_abort_never_claims_cancellation_or_emits_a_terminal(sessions, monkeypatch, transport, failure):
    token, _, keys, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    server = GatewayServer(sessions=sessions)
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    surface = server if transport == 'gateway' else channel
    with request_owner_scope(A):
        surface.chat_commands.accept(KEY, 'run-private', {'message': 'private task'})
    callback = None if failure == 'missing' else Mock(return_value=False, side_effect=(
        RuntimeError('private callback content') if failure == 'raised' else None))
    server.on_chat_abort = callback
    channel._abort_callback = callback
    socket = Socket()
    frame = {'id': 'abort', 'method': 'chat.abort', 'sessionId': 'unrelated-relay-session',
             'params': {'runId': 'run-private', 'voiceAccess': token({'sub': A.uid})}}
    if transport == 'gateway':
        await server._handle_ws_rpc(socket, 'client', frame)
    else:
        from tests.relay_voice_helpers import relay_rpc

        await relay_rpc(channel, socket, frame, uid=A.uid)
    assert len(socket.sent) == 1
    assert socket.sent[-1]['result'] == {'ok': True, 'cancelled': False}
    assert surface.chat_commands.lookup(KEY, 'run-private')['status'] == 'accepted'
