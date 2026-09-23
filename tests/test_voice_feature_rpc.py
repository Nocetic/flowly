from unittest.mock import AsyncMock, Mock

import pytest
from loguru import logger

import flowly.profile as profiles
from flowly.board.orchestrator import BoardOrchestrator
from flowly.board.store import BoardStore
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer
from flowly.live_voice.access import VoiceAccessVerifier
from flowly.live_voice.service import LiveVoiceService
from flowly.live_voice.sessions import VoiceSessions
from flowly.session.manager import SessionManager
from tests.relay_voice_helpers import relay_rpc
from tests.test_chat_command_transport import Socket
from tests.test_voice_access import HOST
from tests.test_voice_access import fixture as access_fixture


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.delenv('FLOWLY_HOME', raising=False)
    monkeypatch.setattr(profiles, '_DEFAULT_HOME', home)
    monkeypatch.setattr(profiles, '_PROFILES_ROOT', home / 'profiles')
    profiles.create_profile('creative', local_runtime=True)
    profile = profiles.ensure_profile_bot_id('creative')
    sessions = SessionManager(tmp_path / 'workspace')
    store = BoardStore(tmp_path / 'board.db')
    orchestrator = BoardOrchestrator(store, AsyncMock(return_value='Ready'))
    service = LiveVoiceService(VoiceSessions(sessions), lambda: (store, orchestrator))
    monkeypatch.setattr(feature_rpc, '_voice_provider', lambda: service)
    yield service, profile, store, sessions
    store.close()


def request(profile, **kwargs):
    return {'conversationId': 'voice-1', 'connectionId': 'connection-1',
            'profile': 'creative', 'botId': profile.bot_id, **kwargs}


@pytest.mark.asyncio
async def test_voice_diagnostics_use_owned_binding_and_strip_operation_metadata(runtime, monkeypatch):
    service, profile, _, _ = runtime
    run = '11111111-1111-4111-8111-111111111111'
    connection = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    operation = 'b' * 64
    rows = []
    sink = logger.add(lambda message: rows.append(str(message)))
    try:
        await feature_rpc.voice_call('voice.open', request(profile, connectionId=connection, voiceRunId=run))
        # The service receives business arguments only, while finally logs the
        # stored run even when the caller supplies a different run identity.
        original_call = LiveVoiceService.call
        received = []
        def observe(self, method, params):
            received.append(dict(params))
            return original_call(self, method, params)
        monkeypatch.setattr(LiveVoiceService, 'call', observe)
        await feature_rpc.voice_call('voice.end', request(profile, connectionId=connection,
            voiceRunId=connection, _voiceDiagnostic={'operationId': operation, 'text': 'private speech'}))
        assert '_voiceDiagnostic' not in received[-1]
        end_log = next(row for row in rows if '"method":"voice.end"' in row)
        assert f'"runId":"{run}"' in end_log
        assert f'"operationId":"{operation}"' in end_log
        assert 'private speech' not in end_log
    finally:
        logger.remove(sink)


@pytest.mark.asyncio
async def test_verified_account_owns_voice_transcript_over_both_transports(runtime, monkeypatch):
    _, profile, _, sessions = runtime
    token, _, fetch, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=fetch))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    server = GatewayServer(sessions=sessions, on_chat_message=AsyncMock())
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    socket = Socket()
    access = token()
    await server._handle_feature_rpc(socket, 'open', 'voice.open', request(profile, voiceAccess=access, ownerUid='forged'))
    assert 'error' not in socket.sent[-1]
    await relay_rpc(channel, socket, {'id': 'append', 'method': 'voice.append', 'sessionId': 'relay-1',
                                      'params': {**request(profile, voiceAccess=access), 'messageId': 'speech-1', 'ordinal': 1,
                                                 'role': 'user', 'text': 'Private account speech'}}, uid='account-1')
    assert 'error' not in socket.sent[-1]
    stored = sessions.read('desktop:voice:voice-1')
    assert stored.metadata['voiceOwner'] == {'kind': 'account', 'uid': 'account-1'}
    assert access not in repr(stored.metadata) + repr(stored.messages)
    for credential in ({}, {'voiceAccess': token({'sub': 'account-2'})}):
        listing, _ = await feature_rpc.dispatch('voice.list', credential)
        assert listing['conversations'] == []
        for method in ('voice.history', 'voice.tasks.events', 'voice.end', 'voice.open', 'voice.focus',
                       'voice.notice', 'voice.tasks.dispatch', 'voice.tasks.get', 'voice.tasks.steer',
                       'voice.tasks.cancel', 'voice.tasks.requests', 'voice.tasks.respond'):
            with pytest.raises(feature_rpc.FeatureRpcError) as error:
                await feature_rpc.dispatch(method, request(profile, **credential))
            assert error.value.code == 'NOT_FOUND'
    own, _ = await feature_rpc.dispatch('voice.history', request(profile, voiceAccess=access))
    assert own['messages'][0]['content'] == 'Private account speech'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [None, '', 'invalid', 'expired'])
async def test_invalid_voice_access_never_falls_back_to_host_ownership(runtime, monkeypatch, invalid):
    _, profile, _, sessions = runtime
    token, _, fetch, _, now = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=fetch))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    if invalid == 'expired':
        invalid = token({'exp': now - 1})
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch('voice.open', request(profile, voiceAccess=invalid))
    assert error.value.code == 'VOICE_AUTH_REQUIRED'
    assert not sessions.list_sessions()


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
async def test_unexpected_authenticated_voice_failure_does_not_log_or_return_credentials(runtime, monkeypatch, transport):
    _, profile, _, sessions = runtime
    monkeypatch.setattr(feature_rpc, 'dispatch', AsyncMock(side_effect=RuntimeError('private-credential-and-account-data')))
    exception = Mock()
    error = Mock()
    monkeypatch.setattr(logger, 'exception', exception)
    monkeypatch.setattr(logger, 'error', error)
    socket = Socket()
    params = request(profile, voiceAccess='private-credential-and-account-data')
    if transport == 'gateway':
        await GatewayServer(sessions=sessions, on_chat_message=AsyncMock())._handle_feature_rpc(socket, 'open', 'voice.open', params)
    else:
        await WebChannel(WebChannelConfig(enabled=True), MessageBus())._handle_feature_rpc(socket, 'open', 'relay-1', 'voice.open', params)
    assert socket.sent[-1]['error']['code'] == 'INTERNAL'
    assert 'private-credential-and-account-data' not in repr(socket.sent)
    exception.assert_not_called()
    assert error.call_count == 1
    assert 'private-credential-and-account-data' not in repr(error.call_args)


@pytest.mark.asyncio
async def test_direct_and_relay_share_transcript_without_starting_an_agent(runtime):
    _, profile, _, sessions = runtime
    server = GatewayServer(sessions=sessions, on_chat_message=AsyncMock())
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    channel.bus.publish_inbound = AsyncMock()
    socket = Socket()
    await server._handle_feature_rpc(socket, 'rpc-1', 'voice.open', request(profile))
    await channel._handle_rpc(socket, {'id': 'rpc-2', 'method': 'voice.append', 'sessionId': 'relay-1',
                                      'params': {**request(profile), 'messageId': 'speech-1', 'ordinal': 1,
                                                 'role': 'user', 'text': 'Raporu hazırla.'}})
    assert socket.sent[-1]['result']['message']['kind'] == 'voice'
    result, _ = await feature_rpc.dispatch('voice.history', {'conversationId': 'voice-1'})
    assert len(result['messages']) == 1
    assert channel.bus.publish_inbound.await_count == 0
    assert not server._active_tasks
    await server._ws_rpc_chat_history(socket, 'history-1', {'sessionKey': 'desktop:voice:voice-1'})
    assert socket.sent[-1]['result']['messages'][0]['voice']['providerMessageId'] == 'speech-1'


@pytest.mark.asyncio
async def test_dispatch_returns_durable_acceptance_and_conflicts_do_not_create_work(runtime):
    service, profile, store, _ = runtime
    service.call('voice.open', request(profile))
    command = request(profile, commandId='dispatch-1', title='Create report', body='Include sources.')
    result, _ = await feature_rpc.dispatch('voice.tasks.dispatch', command)
    assert result['status'] == 'accepted'
    assert result['card']['status'] == 'ready'
    assert result['card']['sessionKey'].startswith('desktop:voice-work:')
    repeated, _ = await feature_rpc.dispatch('voice.tasks.dispatch', command)
    assert repeated['card']['id'] == result['card']['id']
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch('voice.tasks.dispatch', {**command, 'body': 'Different instructions'})
    assert len(store.list_cards()) == 1
    events, _ = await feature_rpc.dispatch('voice.tasks.events', {'conversationId': 'voice-1'})
    assert events['cards'][0]['id'] == result['card']['id']


@pytest.mark.asyncio
async def test_wrong_agent_identity_and_unknown_conversation_are_rejected(runtime):
    _, profile, store, sessions = runtime
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch('voice.open', request(profile, botId='different'))
    assert sessions.list_sessions() == []
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch('voice.tasks.dispatch', request(profile, commandId='dispatch-1', title='Work'))
    assert store.list_cards() == []


@pytest.mark.asyncio
async def test_expired_connection_cannot_dispatch_but_can_read_durable_work(runtime):
    service, profile, store, _ = runtime
    service.call('voice.open', request(profile))
    service.call('voice.end', request(profile))
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch('voice.tasks.dispatch', request(profile, commandId='dispatch-1', title='Work'))
    assert error.value.code == 'STALE_CONNECTION'
    assert store.list_cards() == []
    assert service.call('voice.tasks.events', {'conversationId': 'voice-1'})['cards'] == []


@pytest.mark.asyncio
async def test_chat_send_cannot_execute_a_transcript_as_an_agent_turn(runtime):
    _, _, _, sessions = runtime
    server = GatewayServer(sessions=sessions, on_chat_message=AsyncMock())
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    channel.bus.publish_inbound = AsyncMock()
    params = {'sessionKey': 'desktop:voice:voice-1', 'message': 'Recorded speech'}
    socket = Socket()
    await server._ws_rpc_chat_send(socket, 'client-1', 'rpc-1', params)
    assert socket.sent[-1]['error']['code'] == 'VOICE_TRANSCRIPT_ONLY'
    await channel._handle_rpc(socket, {'method': 'chat.send', 'id': 'rpc-2', 'sessionId': 'relay-1', 'params': params})
    assert socket.sent[-1]['error']['code'] == 'VOICE_TRANSCRIPT_ONLY'
    assert channel.bus.publish_inbound.await_count == 0
    assert not server._active_tasks
    assert sessions.list_sessions() == []


@pytest.mark.asyncio
async def test_named_runtime_does_not_advertise_or_accept_a_second_voice_task_owner(runtime, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(runtime[1].path))
    from flowly.runtime_capabilities import resolve_runtime_capabilities
    # Use the same runtime resolver inputs as production child processes.
    capabilities = resolve_runtime_capabilities()
    if capabilities.owns_shared_board:
        pytest.fail('Named runtime fixture did not establish runtime isolation')
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch('voice.get', {'conversationId': 'voice-1'})
    assert not any(m.startswith('voice.') and m != 'voice.context' for m in feature_rpc.system_capabilities()['featureMethods'])


def completion_notice(runtime):
    service, profile, store, _ = runtime
    service.call('voice.open', request(profile))
    card = service.call('voice.tasks.dispatch', request(profile, commandId='dispatch-1', title='Rapor'))['card']
    claim = store.claim_card(card['id'], worker='creative')
    store.finish_claim(card['id'], claim.claim_token, outcome='done', result='Rapor hazır.')
    event = next(e for e in store.get_events(card['id']) if e['kind'] == 'run_finished')
    return service, store, card['id'], {**request(profile), 'eventId': event['id']}


def test_notice_delivery_is_claimed_before_speech_and_survives_reconnect(runtime):
    service, _, _, params = completion_notice(runtime)
    assert service.call('voice.notice', {**params, 'delivery': 'started'})['accepted']
    assert not service.call('voice.notice', {**params, 'delivery': 'started'})['accepted']
    assert service.call('voice.notice', {**params, 'delivery': 'played'})['accepted']
    service.call('voice.end', params)
    profile = runtime[1]
    service.call('voice.open', request(profile, connectionId='connection-2'))
    assert not service.call('voice.notice', {**params, 'connectionId': 'connection-2', 'delivery': 'started'})['accepted']
    history = service.call('voice.history', {'conversationId': 'voice-1'})
    assert history['notices'][str(params['eventId'])]['delivery'] == 'played'
    assert history['messages'] == []  # An announcement is never a synthetic user message.


def test_crash_during_notice_does_not_allow_automatic_replay(runtime):
    service, _, _, params = completion_notice(runtime)
    service.call('voice.notice', {**params, 'delivery': 'started'})
    service.call('voice.open', request(runtime[1], connectionId='connection-2'))
    retry = service.call('voice.notice', {**params, 'connectionId': 'connection-2', 'delivery': 'started'})
    assert not retry['accepted']
    assert retry['notice']['delivery'] == 'started'


def test_notice_completion_can_be_recorded_after_voice_ends(runtime):
    service, _, _, params = completion_notice(runtime)
    service.call('voice.notice', {**params, 'delivery': 'started'})
    service.call('voice.end', params)
    assert service.call('voice.notice', {**params, 'delivery': 'interrupted'})['accepted']
    assert not service.call('voice.notice', {**params, 'delivery': 'played'})['accepted']


def test_notice_cannot_reference_another_conversations_event(runtime):
    service, _, _, params = completion_notice(runtime)
    service.call('voice.open', request(runtime[1], conversationId='voice-2'))
    from flowly.live_voice.sessions import VoiceError
    with pytest.raises(VoiceError) as error:
        service.call('voice.notice', {**params, 'conversationId': 'voice-2', 'delivery': 'started'})
    assert error.value.code == 'NOT_FOUND'


def test_old_completion_is_not_announced_after_task_has_resumed(runtime):
    service, store, card_id, params = completion_notice(runtime)
    store.set_status(card_id, 'ready')
    result = service.call('voice.notice', {**params, 'delivery': 'started'})
    assert result == {'accepted': False, 'reason': 'superseded'}


def test_notice_rechecks_the_displayed_result_revision_before_claiming(runtime):
    service, store, card_id, params = completion_notice(runtime)
    revision = store.get_card(card_id).revision
    store.update_card(card_id, title='Updated report')
    result = service.call('voice.notice', {**params, 'expectedRevision': revision, 'delivery': 'started'})
    assert result == {'accepted': False, 'reason': 'superseded'}


@pytest.mark.asyncio
async def test_scoped_steer_and_cancel_return_receipts_without_another_card(runtime):
    service, profile, store, _ = runtime
    service.call('voice.open', request(profile))
    card = service.call('voice.tasks.dispatch', request(profile, commandId='dispatch-1', title='Report'))['card']
    params = request(profile, taskId=card['id'], commandId='steer-1', expectedRevision=card['revision'], text='Add charts')
    receipt, _ = await feature_rpc.dispatch('voice.tasks.steer', params)
    assert receipt['command']['status'] == 'queued_for_next_turn'
    replay, _ = await feature_rpc.dispatch('voice.tasks.steer', params)
    assert replay['command'] == receipt['command']
    fetched, _ = await feature_rpc.dispatch('voice.tasks.get', request(profile, taskId=card['id']))
    assert len(fetched['commands']) == 2
    stopped, _ = await feature_rpc.dispatch('voice.tasks.cancel', request(
        profile, taskId=card['id'], commandId='cancel-1', expectedRevision=fetched['card']['revision'],
    ))
    assert stopped['command']['status'] == 'stopped'
    assert stopped['card']['status'] == 'cancelled'
    assert len(store.list_cards()) == 1
    service.call('voice.open', request(profile, conversationId='other'))
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch('voice.tasks.get', request(profile, conversationId='other', taskId=card['id']))


@pytest.mark.asyncio
async def test_old_connection_cannot_steer_or_cancel_but_can_read_task(runtime):
    service, profile, _, _ = runtime
    service.call('voice.open', request(profile))
    card = service.call('voice.tasks.dispatch', request(profile, commandId='dispatch-1', title='Report'))['card']
    service.call('voice.open', request(profile, connectionId='connection-2'))
    for method in ('voice.tasks.steer', 'voice.tasks.cancel'):
        with pytest.raises(feature_rpc.FeatureRpcError) as error:
            await feature_rpc.dispatch(method, request(profile, taskId=card['id'], commandId='old-command',
                                                       expectedRevision=card['revision'], text='More'))
        assert error.value.code == 'STALE_CONNECTION'
    fetched, _ = await feature_rpc.dispatch('voice.tasks.get', request(profile, taskId=card['id']))
    assert fetched['card']['id'] == card['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,expected', [('applied', 'applied'), ('stale', 'rejected'), ('lost_ack', 'status_unknown')])
async def test_answer_is_recorded_once_and_never_retried_as_a_new_turn(runtime, outcome, expected):
    from types import SimpleNamespace

    from flowly.profile_host_contract import ProfileHostError

    service, profile, store, _ = runtime
    service.call('voice.open', request(profile))
    card = service.call('voice.tasks.dispatch', request(profile, commandId='dispatch-1', title='Report'))['card']

    async def rpc(target, method, params, timeout):
        assert target == 'creative'
        assert method == 'chat.respond'
        assert params['sessionKey'] == card['sessionKey']
        assert params['expectedBotId'] == profile.bot_id
        assert store.voice_commands.list('voice-1', card['id'])[-1]['status'] == 'delivering'
        if outcome == 'stale':
            raise ProfileHostError('STALE_REQUEST', 'Already answered')
        if outcome == 'lost_ack':
            raise ConnectionError('Lost acknowledgement')
        return {'status': 'applied'}

    host = SimpleNamespace(_target_rpc=AsyncMock(side_effect=rpc))
    service.worker = lambda: host
    params = request(profile, taskId=card['id'], commandId='response-1', expectedRevision=card['revision'],
                     requestId='question-1', requestRevision='a' * 64, requestType='clarify',
                     runId='worker-1', answer='Use sales figures')
    result, _ = await feature_rpc.dispatch('voice.tasks.respond', params)
    assert result['command']['status'] == expected
    replay, _ = await feature_rpc.dispatch('voice.tasks.respond', params)
    assert replay['command'] == result['command']
    assert host._target_rpc.await_count == 1
    assert len(store.list_cards()) == 1


@pytest.mark.asyncio
async def test_voice_focus_is_scoped_revisioned_and_cannot_reactivate_an_ended_connection(runtime):
    service, profile, _, _ = runtime
    conversation = service.call('voice.open', request(profile))
    card = service.call('voice.tasks.dispatch', request(profile, commandId='dispatch-1', title='Report'))['card']
    focused, _ = await feature_rpc.dispatch('voice.focus', request(
        profile, taskId=card['id'], expectedRevision=conversation['revision'],
    ))
    assert focused['focusTaskId'] == card['id']
    assert focused['voiceProtocolVersion'] == 1
    with pytest.raises(feature_rpc.FeatureRpcError) as stale:
        await feature_rpc.dispatch('voice.focus', request(profile, taskId=None, expectedRevision=conversation['revision']))
    assert stale.value.code == 'CONFLICT'
    other = service.call('voice.open', request(profile, conversationId='other'))
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch('voice.focus', request(profile, conversationId='other', taskId=card['id'], expectedRevision=other['revision']))
    service.call('voice.end', request(profile))
    with pytest.raises(feature_rpc.FeatureRpcError) as ended:
        await feature_rpc.dispatch('voice.focus', request(profile, taskId=card['id'], expectedRevision=focused['revision']))
    assert ended.value.code == 'STALE_CONNECTION'
