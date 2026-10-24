"""A voice call belongs to the chat it starts in (docs/chat-wire-protocol.md §7)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from flowly.agent.loop import AgentLoop, _turn_user_extras
from flowly.live_voice import chat_binding
from flowly.live_voice.access import VoicePrincipal
from flowly.live_voice.authority import RequestOwner, current_request_owner, request_owner_scope
from flowly.live_voice.chat_binding import (
    ABANDONED_SECONDS, CONTEXT_KIND, MAX_TURNS, VoiceChatBindings, call_record, chat_key, transcript, upsert_context,
)
from flowly.live_voice.events import current_event_access
from flowly.live_voice.memory_snapshot import validate_snapshot
from flowly.live_voice.recent_chat import bound_chat, latest_chat
from flowly.live_voice.service import LiveVoiceService
from flowly.live_voice.sessions import VoiceError, VoiceSessions
from flowly.session.history_rows import history_row
from flowly.session.manager import ConcurrentSessionWriteError, SessionManager

CHAT = 'web:chat-1'


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    return SessionManager(tmp_path / 'workspace')


@pytest.fixture
def voice(manager):
    return VoiceSessions(manager)


def open_call(voice, chat=CHAT, **changes):
    return voice.open({'conversationId': 'call-1', 'connectionId': 'connection-1', 'language': 'tr', **changes},
                      profile='default', bot_id='agent-1', chat=chat)


def say(voice, ordinal, role, text, **changes):
    return voice.append({'conversationId': 'call-1', 'connectionId': changes.pop('connectionId', 'connection-1'),
                         'messageId': f'm{ordinal}', 'ordinal': ordinal, 'role': role, 'text': text, **changes})


def end(voice, connection='connection-1'):
    return voice.end({'conversationId': 'call-1', 'connectionId': connection})


def ledger(manager):
    return manager.read('desktop:voice:call-1')


# ── binding ───────────────────────────────────────────────────────────────


def test_a_call_is_bound_once_to_its_chat_and_says_so(voice):
    assert open_call(voice)['chatSessionKey'] == CHAT
    # Reopening (a new connection) with the same chat, or by an older client without it, keeps it.
    assert open_call(voice, connectionId='connection-2')['chatSessionKey'] == CHAT
    assert open_call(voice, chat=None, connectionId='connection-3')['chatSessionKey'] == CHAT
    with pytest.raises(VoiceError) as other:
        open_call(voice, chat='web:chat-2', connectionId='connection-4')
    assert other.value.code == 'CONFLICT'


def test_a_call_of_its_own_cannot_join_a_chat_later(voice):
    open_call(voice, chat=None)
    with pytest.raises(VoiceError) as late:
        open_call(voice, connectionId='connection-2')
    assert late.value.code == 'CONFLICT'


@pytest.mark.parametrize('key, profile', [('desktop:voice:x', 'default'), ('web:a/b', 'default'), ('web:', 'default'),
                                          ('desktop:profile-home', 'default'), (42, 'default'), (CHAT, 'writer')])
def test_only_the_main_agents_web_chats_take_a_call(key, profile):
    with pytest.raises(VoiceError) as refused:
        chat_key(key, profile=profile)
    assert refused.value.code == 'INVALID_PARAMS'


def test_bound_calls_are_not_listed_as_voice_chats(voice):
    open_call(voice)
    voice.open({'conversationId': 'call-2', 'connectionId': 'c', 'language': 'en'}, profile='default', bot_id='agent-1')
    assert [row['conversationId'] for row in voice.list({})['conversations']] == ['call-2']


# ── the record ────────────────────────────────────────────────────────────


def test_live_record_has_no_turns_and_ended_record_has_the_final_words_in_order(voice, manager):
    open_call(voice)
    say(voice, 1, 'user', 'Merhaba')
    say(voice, 2, 'assistant', 'Selam, nasıl yardımcı olabilirim?')
    say(voice, 2, 'assistant', 'Selam! Nasıl yardımcı olabilirim?', revision=2)  # a correction
    live = call_record(ledger(manager))
    assert live['state'] == 'voice_call' and live['phase'] == 'live' and live['sessionKey'] == CHAT
    assert live['callId'] == 'call-1' and 'turns' not in live and live['endedAt'] is None
    assert live['startedAt'].endswith('Z') and len(live['startedAt']) == len('2026-10-24T14:02:11.000Z')
    assert 'runId' not in live
    say(voice, 3, 'assistant', '', delivery='interrupted')
    end(voice)
    ended = call_record(ledger(manager))
    assert ended['phase'] == 'ended' and ended['endedAt'].endswith('Z') and ended['durationMs'] >= 0
    assert [(t['role'], t['text']) for t in ended['turns']] == [
        ('user', 'Merhaba'), ('assistant', 'Selam! Nasıl yardımcı olabilirim?'), ('assistant', '')]
    assert ended['turns'][2]['interrupted'] is True
    assert ended['truncated'] is False


def test_a_long_call_keeps_its_newest_words(voice, manager):
    open_call(voice)
    for ordinal in range(1, MAX_TURNS + 21):
        say(voice, ordinal, 'user' if ordinal % 2 else 'assistant', f'söz {ordinal}')
    turns, truncated = transcript(ledger(manager))
    assert len(turns) == MAX_TURNS and truncated and turns[-1]['text'] == f'söz {MAX_TURNS + 20}'


def test_an_unbound_call_has_no_record(voice, manager):
    voice.open({'conversationId': 'call-1', 'connectionId': 'c', 'language': 'en'}, profile='default', bot_id='agent-1')
    assert call_record(ledger(manager)) is None


# ── the chat: never a write beside a running turn ─────────────────────────


def test_a_write_beside_a_running_turn_would_lose_the_turn(manager):
    """Why the context never goes in with a plain mutate: the turn's own save fails."""
    turn = manager.get_or_create(CHAT)
    turn.add_message('user', 'yazılı soru')
    manager.save(turn)
    turn = manager.get_or_create(CHAT)
    turn.add_message('assistant', 'cevap')
    manager.mutate(CHAT, lambda other: upsert_context(other, 'call-1', 'konuşma'))
    with pytest.raises(ConcurrentSessionWriteError):
        manager.save(turn)


@pytest.mark.asyncio
async def test_the_end_of_a_call_waits_for_the_running_turn_and_both_are_kept(manager, voice):
    loop_like = SimpleNamespace(sessions=manager, _session_turn_locks={})
    between = lambda key, op: AgentLoop.mutate_between_turns(loop_like, key, op)  # noqa: E731
    bindings = VoiceChatBindings(manager, between_turns=between)
    bindings.bind('call-1', CHAT)
    open_call(voice)
    say(voice, 1, 'user', 'Bunu hatırla')
    end(voice)
    lock = loop_like._session_turn_locks.setdefault(CHAT, asyncio.Lock())
    await lock.acquire()  # a turn is running in the chat
    turn = manager.get_or_create(CHAT)
    turn.add_message('user', 'yazılı soru')
    finishing = asyncio.create_task(bindings.finish('call-1'))
    await asyncio.sleep(0.05)
    assert not finishing.done()  # it waits for the turn
    turn.add_message('assistant', 'cevap')
    manager.save(turn)  # the turn's own save succeeds
    lock.release()
    assert await finishing is True
    chat = manager.read(CHAT)
    assert [m['content'] for m in chat.messages if m.get('kind') != CONTEXT_KIND] == ['yazılı soru', 'cevap']
    context = [m for m in chat.messages if m.get('kind') == CONTEXT_KIND]
    assert len(context) == 1 and context[0]['_display_hidden'] is True and context[0]['role'] == 'user'
    assert 'User: Bunu hatırla' in context[0]['content'] and context[0]['voiceCallId'] == 'call-1'
    assert ledger(manager).metadata['voice']['chatMirror']['revision'] == ledger(manager).metadata['voice']['revision']
    # The next turn reads it from its cached object: mutate refreshed the cache.
    assert any(m.get('kind') == CONTEXT_KIND for m in manager.get_or_create(CHAT).messages)
    # Writing it again changes nothing (one row per call).
    assert await bindings.finish('call-1') is True
    assert len([m for m in manager.read(CHAT).messages if m.get('kind') == CONTEXT_KIND]) == 1


def test_a_turn_during_a_live_call_gets_the_words_so_far_in_its_own_session(manager, voice):
    bindings = VoiceChatBindings(manager)
    bindings.bind('call-1', CHAT)
    open_call(voice)
    say(voice, 1, 'user', 'Yarın toplantı var')
    turn = manager.get_or_create(CHAT)
    bindings.on_turn_start(turn)
    rows = [m for m in turn.messages if m.get('kind') == CONTEXT_KIND]
    assert len(rows) == 1 and 'still going' in rows[0]['content'] and 'User: Yarın toplantı var' in rows[0]['content']
    say(voice, 2, 'assistant', 'Not aldım')
    bindings.on_turn_start(turn)
    rows = [m for m in turn.messages if m.get('kind') == CONTEXT_KIND]
    assert len(rows) == 1 and 'Agent: Not aldım' in rows[0]['content']
    manager.save(turn)  # saved with the turn, no conflict
    end(voice)
    other = manager.get_or_create('web:chat-2')
    bindings.on_turn_start(other)
    assert not other.messages


def test_the_context_row_reaches_the_model_but_never_the_transcript(manager):
    chat = manager.get_or_create(CHAT)
    chat.add_message('user', 'merhaba')
    upsert_context(chat, 'call-1', '[Voice call] User: x')
    manager.save(chat)
    assert any('[Voice call]' in str(m.get('content')) for m in chat.get_history())
    assert all('[Voice call]' not in str(m.get('content')) for m in manager.get_full_messages(CHAT))


# ── the record is the chat's, not the request's ───────────────────────────


@pytest.mark.asyncio
async def test_the_record_goes_out_shared_even_from_an_account_request(manager, voice):
    seen = []

    async def emit(record):
        seen.append((record['phase'], current_request_owner(), current_event_access()))

    bindings = VoiceChatBindings(manager, emit=emit)
    bindings.bind('call-1', CHAT)
    account = voice.for_principal(VoicePrincipal('account-1', 'host-1', 9999999999, 'cred'))
    with request_owner_scope(RequestOwner('account-1')):
        open_call(account)
        bindings.changed('call-1')
    await bindings.wait()
    assert seen == [('live', None, None)]


@pytest.mark.asyncio
async def test_the_service_binds_checks_the_chat_and_sends_live_then_ended(manager, voice, monkeypatch):
    import flowly.live_voice.service as service_module

    monkeypatch.setattr(service_module, 'target', lambda params: ('default', 'agent-1'))
    records = []

    async def emit(record):
        records.append(record['phase'])

    bindings = VoiceChatBindings(manager, emit=emit)
    service = LiveVoiceService(voice, lambda: (None, None), chats=bindings)
    params = {'conversationId': 'call-1', 'connectionId': 'connection-1', 'language': 'tr', 'profile': 'default',
              'botId': 'agent-1', 'chatSessionKey': CHAT}
    assert service.call('voice.open', params)['chatSessionKey'] == CHAT
    assert bindings.chat_of('call-1') == CHAT
    service.call('voice.end', {'conversationId': 'call-1', 'connectionId': 'connection-1'})
    await bindings.wait()
    assert records == ['live', 'ended']
    # Another account's chat is not this account's to join.
    manager.mutate('web:private', lambda s: s.metadata.update({'voiceOwner': {'kind': 'account', 'uid': 'someone'}}))
    owned = service.for_owner(RequestOwner('account-1'))
    with request_owner_scope(RequestOwner('account-1')), pytest.raises(VoiceError) as hidden:
        owned.call('voice.open', {**params, 'conversationId': 'call-9', 'chatSessionKey': 'web:private'})
    assert hidden.value.code == 'NOT_FOUND'
    # Without bindings (an older host), a chat key is refused, not ignored.
    with pytest.raises(VoiceError) as unsupported:
        LiveVoiceService(voice, lambda: (None, None)).call('voice.open', {**params, 'conversationId': 'call-8'})
    assert unsupported.value.code == 'UNAVAILABLE'


@pytest.mark.asyncio
async def test_the_sweep_ends_an_abandoned_call_and_writes_its_chat(manager, voice):
    clock = {'now': datetime.now(timezone.utc)}
    records = []

    async def emit(record):
        records.append(record['phase'])

    loop_like = SimpleNamespace(sessions=manager, _session_turn_locks={})
    bindings = VoiceChatBindings(manager, emit=emit, now=lambda: clock['now'],
                                 between_turns=lambda key, op: AgentLoop.mutate_between_turns(loop_like, key, op))
    open_call(voice)
    say(voice, 1, 'user', 'Alo')
    bindings.rebuild()
    assert bindings.chat_of('call-1') == CHAT
    await bindings.sweep()
    assert records == []  # still live
    clock['now'] += timedelta(seconds=ABANDONED_SECONDS + 5)
    await bindings.sweep()
    assert records == ['ended']
    assert ledger(manager).metadata['voice']['connections'][-1]['endedAt']
    assert any(m.get('kind') == CONTEXT_KIND for m in manager.read(CHAT).messages)
    await bindings.sweep()
    assert records == ['ended']  # done; nothing to send again


# ── a voice request in the chat ───────────────────────────────────────────


def test_a_voice_request_is_marked_only_for_a_call_of_this_chat(manager):
    bindings = VoiceChatBindings(manager)
    bindings.bind('call-1', CHAT)
    assert bindings.chat_send_origin({}, CHAT) == {}
    assert bindings.chat_send_origin({'origin': 'voice', 'voiceCallId': 'call-1'}, CHAT) == {
        'origin': 'voice', 'voiceCallId': 'call-1'}
    for params, key in [({'origin': 'voice', 'voiceCallId': 'call-1'}, 'web:other'),
                        ({'origin': 'email', 'voiceCallId': 'call-1'}, CHAT),
                        ({'origin': 'voice', 'voiceCallId': 'a/b'}, CHAT),
                        ({'origin': 'voice'}, CHAT)]:
        with pytest.raises(ValueError):
            bindings.chat_send_origin(params, key)


def test_the_user_row_keeps_the_mark_and_history_returns_it(manager):
    assert _turn_user_extras({'_user_extras': {'origin': 'voice', 'voiceCallId': 'call-1'}}) == {
        'origin': 'voice', 'voiceCallId': 'call-1'}
    assert _turn_user_extras({'_user_extras': {'origin': 'other', 'voiceCallId': 'call-1'}}) == {}
    assert _turn_user_extras({}) == {}
    chat = manager.get_or_create(CHAT)
    chat.extend_with_turn_messages(user_content='araştır', new_messages=[], final_content='tamam',
                                   user_extras={'origin': 'voice', 'voiceCallId': 'call-1'})
    manager.save(chat)
    user = next(m for m in manager.get_full_messages(CHAT) if m['role'] == 'user')
    row = history_row(user)
    assert row['origin'] == 'voice' and row['voiceCallId'] == 'call-1'
    assert all('origin' not in m for m in chat.get_history())  # never sent to the model


# ── the call's startup context ────────────────────────────────────────────


def test_a_bound_calls_snapshot_takes_its_chat_as_the_latest_conversation(manager, voice, tmp_path):
    chat = manager.get_or_create(CHAT)
    chat.add_message('user', 'bağlı sohbet')
    chat.add_message('assistant', 'evet')
    manager.save(chat)
    newer = manager.get_or_create('web:newer')
    newer.add_message('user', 'daha yeni sohbet')
    newer.add_message('assistant', 'tamam')
    manager.save(newer)
    open_call(voice)
    directory = manager.sessions_dir
    assert bound_chat(directory, 'call-1') == CHAT
    assert bound_chat(directory, 'nope') is None
    assert latest_chat(directory, prefer=CHAT).key == CHAT
    assert validate_snapshot({'conversationId': 'call-1'}) == {'knownRevision': None, 'conversationId': 'call-1'}
    with pytest.raises(VoiceError):
        validate_snapshot({'conversationId': 'a/b'})
    with pytest.raises(VoiceError):
        validate_snapshot({'other': 1})


def test_capabilities_say_a_call_can_join_a_chat_only_with_bindings(manager, monkeypatch):
    from flowly.channels import feature_rpc

    monkeypatch.setattr(chat_binding, '_active', None)
    assert feature_rpc._chat_binding_version() == 0
    monkeypatch.setattr(chat_binding, '_active', VoiceChatBindings(manager))
    assert feature_rpc._chat_binding_version() == 1


@pytest.mark.asyncio
async def test_records_go_out_in_the_order_the_call_changed(manager, voice):
    """The server keeps the last record: a slow live must not land after the ended."""
    sent = []

    async def emit(record):
        if record['phase'] == 'live':
            await asyncio.sleep(0.05)  # the first send is slow
        sent.append(record['phase'])

    bindings = VoiceChatBindings(manager, emit=emit)
    bindings.bind('call-1', CHAT)
    open_call(voice)
    bindings.changed('call-1')
    end(voice)
    bindings.changed('call-1')
    await bindings.wait()
    assert sent == ['live', 'ended']


@pytest.mark.asyncio
async def test_over_the_relay_channel_a_voice_request_reaches_the_turn_marked_and_a_wrong_one_is_refused(manager, monkeypatch):
    from unittest.mock import AsyncMock

    from flowly.bus.queue import MessageBus
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig
    from tests.test_chat_command_transport import Socket

    bindings = VoiceChatBindings(manager)
    bindings.bind('call-1', CHAT)
    monkeypatch.setattr(chat_binding, '_active', bindings)
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    channel = WebChannel(WebChannelConfig(enabled=True), bus)
    socket = Socket()
    ask = {'sessionKey': CHAT, 'idempotencyKey': 'voice-1', 'message': 'Bunu araştır',
           'origin': 'voice', 'voiceCallId': 'call-1'}
    await channel._handle_rpc(socket, {'type': 'rpc', 'method': 'chat.send', 'id': 'r1', 'sessionId': 'relay-1', 'params': ask})
    await asyncio.gather(*list(channel._active_tasks.values()))
    assert socket.sent[-1]['result']['runId'] == 'voice-1'
    inbound = bus.publish_inbound.await_args.args[0]
    assert inbound.metadata['_user_extras'] == {'origin': 'voice', 'voiceCallId': 'call-1'}
    # A call of another chat, or an origin Core does not know: refused before it is accepted.
    for wrong in ({**ask, 'idempotencyKey': 'voice-2', 'sessionKey': 'web:other'},
                  {**ask, 'idempotencyKey': 'voice-3', 'origin': 'email'}):
        await channel._handle_rpc(socket, {'type': 'rpc', 'method': 'chat.send', 'id': 'r2', 'sessionId': 'relay-1', 'params': wrong})
        assert socket.sent[-1]['error']['code'] == 'INVALID_REQUEST'
        assert channel.chat_commands.lookup(wrong['sessionKey'], wrong['idempotencyKey']) is None
    # An ordinary message is untouched.
    await channel._handle_rpc(socket, {'type': 'rpc', 'method': 'chat.send', 'id': 'r3', 'sessionId': 'relay-1',
                                       'params': {'sessionKey': CHAT, 'idempotencyKey': 'm-1', 'message': 'yazılı'}})
    await asyncio.gather(*list(channel._active_tasks.values()))
    assert '_user_extras' not in bus.publish_inbound.await_args.args[0].metadata
