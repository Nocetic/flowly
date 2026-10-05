from concurrent.futures import ThreadPoolExecutor

import pytest

from flowly.live_voice.access import VoicePrincipal
from flowly.live_voice.sessions import VoiceError, VoiceSessions
from flowly.session.manager import SessionManager


@pytest.fixture
def voice(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    return VoiceSessions(SessionManager(tmp_path / 'workspace'))


def open_call(voice, **changes):
    return voice.open({'conversationId': 'conversation-1', 'connectionId': 'connection-1', 'language': 'tr', **changes},
                      profile='creative', bot_id='agent-1')


def append(voice, **changes):
    return voice.append({'conversationId': 'conversation-1', 'connectionId': 'connection-1',
                         'messageId': 'message-1', 'ordinal': 1, 'role': 'user', 'text': 'Raporu hazırla.', **changes})


def account_voice(voice, uid):
    return voice.for_principal(VoicePrincipal(uid, 'host-1', 9999999999, 'credential-1'))


def test_resumed_speech_link_survives_history_reload_and_corrections(voice):
    open_call(voice)
    append(voice, role='assistant', text='Merhaba!')
    voice.end({'conversationId': 'conversation-1', 'connectionId': 'connection-1'})
    open_call(voice, connectionId='connection-2')
    linked = {'connectionId': 'connection-2', 'role': 'assistant', 'text': 'Nasılsın?',
              'continuesMessageId': 'connection-1:message-1'}
    append(voice, **linked)
    append(voice, **{**linked, 'text': 'Sen nasılsın?', 'revision': 2})
    restarted = VoiceSessions(SessionManager(voice.sessions.workspace))
    messages = restarted.history({'conversationId': 'conversation-1'})['messages']
    assert len(messages) == 2
    assert messages[1]['voice']['continuesMessageId'] == 'connection-1:message-1'
    assert messages[1]['content'] == 'Sen nasılsın?'
    with pytest.raises(VoiceError):
        append(voice, **{**linked, 'continuesMessageId': None, 'revision': 3})


@pytest.mark.parametrize('parent,role', [('missing:message', 'assistant'),
                                       ('connection-1:message-1', 'assistant'),
                                       ('connection-1:message-1', 'user')])
def test_invalid_resume_links_do_not_mutate_history(voice, parent, role):
    open_call(voice)
    append(voice, role='assistant')
    with pytest.raises(VoiceError):
        append(voice, messageId='other', ordinal=2, role=role, continuesMessageId=parent)
    assert len(voice.history({'conversationId': 'conversation-1'})['messages']) == 1


def test_diagnostic_binding_is_owned_immutable_and_survives_reconnect(voice):
    owner = account_voice(voice, 'account-a')
    run = '11111111-1111-4111-8111-111111111111'
    first = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    second = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    params = {'conversationId': 'conversation-1', 'connectionId': first}
    expected = {'runId': run, 'connectionId': first,
                'runRef': '120a0c73eef5846dc9691db137cc4043682d25b656d7274f9346f9041e0c61c1',
                'sessionRef': 'b8b944ecc0f08de10e2f89816ad2c4228225b358b6fb91618533dd050505e576'}
    open_call(owner, connectionId=first, voiceRunId=run)
    # Retrying open cannot relabel an existing connection's trace.
    open_call(owner, connectionId=first, voiceRunId=second)
    assert owner.diagnostic_identity(params) == expected
    owner.end(params)
    open_call(owner, connectionId=second, voiceRunId=run)
    assert owner.diagnostic_identity(params) == expected
    next_binding = owner.diagnostic_identity({**params, 'connectionId': second})
    assert next_binding['runId'] == run and next_binding['connectionId'] == second
    assert next_binding['runRef'] == expected['runRef']
    assert next_binding['sessionRef'] != expected['sessionRef']
    restarted = VoiceSessions(SessionManager(voice.sessions.workspace)).for_principal(
        VoicePrincipal('account-a', 'host-1', 9999999999, 'new-credential'))
    assert restarted.diagnostic_identity(params) == expected
    with pytest.raises(VoiceError):
        account_voice(voice, 'account-b').diagnostic_identity(params)
    assert owner.diagnostic_identity({**params, 'connectionId': run}) == {}


def test_legacy_and_invalid_diagnostics_do_not_change_open_acceptance(voice):
    opened = open_call(voice, voiceRunId='private arbitrary text')
    assert 'diagnosticRunId' not in opened['lastConnection']
    assert voice.diagnostic_identity({'conversationId': 'conversation-1', 'connectionId': 'connection-1'}) == {}


def test_account_owned_conversations_are_not_adopted_or_listed_by_other_accounts(voice):
    owner = account_voice(voice, 'account-a')
    other = account_voice(voice, 'account-b')
    opened = open_call(owner)
    append(owner)
    assert owner.sessions.read(opened['sessionKey']).metadata['voiceOwner'] == {'kind': 'account', 'uid': 'account-a'}
    assert len(owner.list({})['conversations']) == 1
    assert not other.list({})['conversations']
    assert not voice.list({})['conversations']
    for client in (other, voice):
        for operation in (lambda: client.get('conversation-1'), lambda: open_call(client),
                          lambda: client.history({'conversationId': 'conversation-1'}),
                          lambda: append(client), lambda: client.end({'conversationId': 'conversation-1', 'connectionId': 'connection-1'})):
            with pytest.raises(VoiceError) as error:
                operation()
            assert error.value.code == 'NOT_FOUND'
    assert len(owner.history({'conversationId': 'conversation-1'})['messages']) == 1
    assert owner.get('conversation-1')['lastConnection']['endedAt'] is None


def test_account_cannot_claim_a_legacy_host_conversation(voice):
    opened = open_call(voice)
    voice.sessions.mutate(opened['sessionKey'], lambda session: session.metadata.pop('voiceOwner'))
    owner = account_voice(voice, 'account-a')
    assert not owner.list({})['conversations']
    with pytest.raises(VoiceError, match='not found'):
        open_call(owner)
    assert voice.get('conversation-1')['conversationId'] == 'conversation-1'
    assert 'voiceOwner' not in voice.sessions.read(opened['sessionKey']).metadata


def test_account_views_filter_before_pagination_and_survive_new_manager(voice):
    a = account_voice(voice, 'account-a')
    b = account_voice(voice, 'account-b')
    for number, client in enumerate((a, b, a, b)):
        open_call(client, conversationId=f'conversation-{number}')
    restarted = VoiceSessions(SessionManager(voice.sessions.workspace)).for_principal(VoicePrincipal('account-a', 'host-1', 9999999999, 'fresh-credential'))
    first = restarted.list({'limit': 1})
    second = restarted.list({'limit': 1, 'offset': first['nextOffset']})
    assert second['nextOffset'] is None
    assert {first['conversations'][0]['conversationId'], second['conversations'][0]['conversationId']} == {'conversation-0', 'conversation-2'}


def test_corrupt_ownership_is_not_treated_as_a_host_conversation(voice):
    opened = open_call(voice)
    voice.sessions.mutate(opened['sessionKey'], lambda session: session.metadata.update(voiceOwner=None))
    assert not voice.list({})['conversations']
    with pytest.raises(VoiceError):
        voice.get('conversation-1')


def test_transcript_has_one_canonical_chat_and_no_agent_turn(voice):
    opened = open_call(voice)
    append(voice)
    append(voice, messageId='message-2', ordinal=2, role='assistant', text='Başlattım.')
    duplicate = append(voice)
    assert duplicate['replayed']
    history = voice.history({'conversationId': 'conversation-1'})
    assert [m['role'] for m in history['messages']] == ['user', 'assistant']
    assert history['conversation']['title'] == 'Raporu hazırla.'
    assert len(voice.sessions.get_full_messages(opened['sessionKey'])) == 2
    assert voice.sessions.list_sessions()[0]['kind'] == 'voice'
    assert all(m['kind'] == 'voice' and not m.get('tool_calls') for m in history['messages'])


def test_catalog_returns_voice_metadata_without_paths_or_other_chats(voice):
    open_call(voice)
    append(voice)
    ordinary = voice.sessions.get_or_create('desktop:regular')
    ordinary.add_message('user', 'Not a voice conversation')
    voice.sessions.save(ordinary)
    result = voice.list({'limit': 1})
    assert result['nextOffset'] is None
    assert len(result['conversations']) == 1
    row = result['conversations'][0]
    assert row['conversationId'] == 'conversation-1'
    assert row['profile'] == 'creative'
    assert row['botId'] == 'agent-1'
    assert row['title'] == 'Raporu hazırla.'
    assert 'path' not in row
    assert 'messages' not in row
    assert voice.list({'offset': 1})['conversations'] == []


def test_provider_corrections_and_late_repeats_do_not_restore_old_text(voice):
    opened = open_call(voice)
    append(voice, role='assistant', text='Creative')
    append(voice, role='assistant', text='Growth', revision=2, delivery='interrupted')
    append(voice, role='assistant', text='Creative')
    # The agent display archive contains an earlier version: canonical voice
    # history must project the correction rather than resurrect that version.
    rows = voice.sessions.get_full_messages(opened['sessionKey'])
    assert len(rows) == 1
    assert rows[0]['content'] == 'Growth'
    assert rows[0]['voice']['delivery'] == 'interrupted'
    with pytest.raises(VoiceError, match='Interrupted'):
        append(voice, role='assistant', text='Growth', revision=3, delivery='played')


def test_reverse_arrival_sorts_by_connection_and_provider_ordinal(voice):
    open_call(voice)
    append(voice, messageId='second', ordinal=2, role='assistant')
    append(voice, messageId='first', ordinal=1)
    open_call(voice, connectionId='connection-2')
    append(voice, connectionId='connection-2', ordinal=1, messageId='first')
    rows = voice.history({'conversationId': 'conversation-1'})['messages']
    assert [(m['voice']['generation'], m['voice']['ordinal']) for m in rows] == [(1, 1), (1, 2), (2, 1)]


@pytest.mark.parametrize('change', [{'text': 'Changed'}, {'role': 'assistant'}, {'ordinal': 2}])
def test_message_identity_conflict_keeps_original(voice, change):
    open_call(voice)
    append(voice)
    with pytest.raises(VoiceError):
        append(voice, **change)
    assert voice.history({'conversationId': 'conversation-1'})['messages'][0]['content'] == 'Raporu hazırla.'


def test_new_connection_invalidates_old_commands_without_losing_late_transcript(voice):
    open_call(voice)
    open_call(voice, connectionId='connection-2')
    with pytest.raises(VoiceError) as exc:
        voice.require_connection({'conversationId': 'conversation-1', 'connectionId': 'connection-1'})
    assert exc.value.code == 'STALE_CONNECTION'
    append(voice)
    voice.end({'conversationId': 'conversation-1', 'connectionId': 'connection-1'})
    assert voice.require_connection({'conversationId': 'conversation-1', 'connectionId': 'connection-2'})


def test_end_is_idempotent_and_reconnect_does_not_reopen_old_handle(voice):
    open_call(voice)
    params = {'conversationId': 'conversation-1', 'connectionId': 'connection-1'}
    assert voice.end(params) == voice.end(params)
    with pytest.raises(VoiceError):
        voice.require_connection(params)
    with pytest.raises(VoiceError):
        open_call(voice)
    resumed = open_call(voice, connectionId='connection-2')
    assert resumed['lastConnection']['generation'] == 2


def test_target_identity_is_pinned_for_resume(voice):
    open_call(voice)
    with pytest.raises(VoiceError) as exc:
        voice.open({'conversationId': 'conversation-1', 'connectionId': 'connection-2'}, profile='default', bot_id='agent-2')
    assert exc.value.code == 'TARGET_CONFLICT'
    assert voice.get('conversation-1')['profile'] == 'creative'


@pytest.mark.parametrize('bad', ['../escape', 'slash/value', 'other:session', 'underscore_collision', '', [], None])
def test_invalid_conversation_identity_does_not_create_files(voice, bad):
    with pytest.raises(VoiceError):
        open_call(voice, conversationId=bad)
    assert voice.sessions.list_sessions() == []


def test_concurrent_process_style_writers_preserve_both_transcripts(voice):
    open_call(voice)
    managers = [VoiceSessions(SessionManager(voice.sessions.workspace)) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda pair: append(pair[1], messageId=f'message-{pair[0]}', ordinal=pair[0]), enumerate(managers)))
    assert len(voice.history({'conversationId': 'conversation-1'})['messages']) == 4


def test_failed_save_does_not_pollute_cached_transcript(voice, monkeypatch):
    open_call(voice)
    original = voice.sessions._save_unlocked

    def fail(*_args, **_kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(voice.sessions, '_save_unlocked', fail)
    with pytest.raises(OSError):
        append(voice)
    monkeypatch.setattr(voice.sessions, '_save_unlocked', original)
    assert voice.history({'conversationId': 'conversation-1'})['messages'] == []
    assert not append(voice)['replayed']


def test_bounded_snapshot_pagination_has_no_phantom_sessions(voice):
    open_call(voice)
    append(voice)
    append(voice, messageId='second', ordinal=2)
    first = voice.history({'conversationId': 'conversation-1', 'limit': 1})
    second = voice.history({'conversationId': 'conversation-1', 'offset': first['nextOffset'], 'limit': 1})
    assert len(first['messages']) == len(second['messages']) == 1
    assert first['conversation']['revision'] == second['conversation']['revision']
    assert second['nextOffset'] is None
    assert len(voice.sessions.list_sessions()) == 1


def test_provider_event_ordinals_are_not_confused_with_message_count_limits(voice):
    open_call(voice)
    append(voice, ordinal=1_000_001)
    assert voice.history({'conversationId': 'conversation-1'})['messages'][0]['voice']['ordinal'] == 1_000_001


def test_correction_before_any_audible_words_keeps_an_interrupted_message(voice):
    open_call(voice)
    append(voice, role='assistant', text='', delivery='interrupted', revision=2)
    row = voice.history({'conversationId': 'conversation-1'})['messages'][0]
    assert row['content'] == ''
    assert row['voice']['delivery'] == 'interrupted'
    with pytest.raises(VoiceError):
        append(voice, messageId='user-empty', ordinal=2, text='')


def test_transcript_rows_keep_their_first_heard_time_in_utc(voice):
    from datetime import datetime
    open_call(voice)
    first = append(voice)['message']['voice']['createdAt']
    assert datetime.fromisoformat(first).utcoffset().total_seconds() == 0
    corrected = append(voice, text='Raporu hemen hazırla.', revision=2)['message']['voice']
    assert corrected['createdAt'] == first and corrected['revision'] == 2
    assert voice.history({'conversationId': 'conversation-1'})['messages'][0]['voice']['createdAt'] == first


def test_a_connection_records_the_app_holding_the_call(voice):
    opened = open_call(voice, client='ios')
    assert opened['lastConnection']['client'] == 'ios' and opened['lastConnection']['endedAt'] is None
    # Another device reading the conversation sees where the call runs.
    assert voice.get('conversation-1')['lastConnection']['client'] == 'ios'
    # Replaying the same open is idempotent; the same connection cannot change app.
    assert open_call(voice, client='ios')['lastConnection']['client'] == 'ios'
    with pytest.raises(VoiceError):
        open_call(voice, client='desktop')
    voice.end({'conversationId': 'conversation-1', 'connectionId': 'connection-1'})
    assert open_call(voice, connectionId='connection-2', client='desktop')['lastConnection']['client'] == 'desktop'


def test_older_clients_send_no_app_and_unknown_apps_are_refused(voice):
    assert 'client' not in open_call(voice)['lastConnection']
    with pytest.raises(VoiceError):
        open_call(voice, connectionId='connection-2', client='watch')
