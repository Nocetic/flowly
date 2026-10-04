"""The language the owner speaks to an agent: learned by the host from the
owner's own speech and the model's reports, used for new conversations."""
import json

import pytest

from flowly.live_voice.access import VoicePrincipal
from flowly.live_voice.language import MAX_OWNERS, VoiceLanguagePreferences, detect_language
from flowly.live_voice.sessions import VoiceSessions
from flowly.session.manager import SessionManager


@pytest.mark.parametrize('text, expected', [
    ('Benim hakkımda ne biliyorsun, bana bir şey söyle', 'tr'),
    ('Bugün planım ne, bir bakar mısın lütfen', 'tr'),
    ('What do you know about me and my work', 'en'),
    ("Can you tell me what I'm doing today", 'en'),
    ('Hola, qué tengo que hacer hoy por la tarde', 'es'),
    ('¿Puedes ver mi calendario para mañana?', 'es'),
])
def test_clear_speech_says_its_language(text, expected):
    assert detect_language(text) == expected


@pytest.mark.parametrize('text', ['', 'Okay', 'Flowly Desktop', 'Americano', 'Hmm evet', '12 34 56'])
def test_short_or_ambiguous_speech_says_nothing(text):
    assert detect_language(text) is None


OWNER = {'kind': 'account', 'uid': 'owner-1'}


def test_heard_speech_moves_the_preference_only_on_a_second_hearing(tmp_path):
    store = VoiceLanguagePreferences(tmp_path / 'voice_language.json')
    store.heard(OWNER, 'bot-1', 'tr')
    assert store.get(OWNER, 'bot-1') == 'tr'  # nothing known yet: the first hearing sets it
    store.heard(OWNER, 'bot-1', 'en')
    assert store.get(OWNER, 'bot-1') == 'tr'  # one stray English sentence
    store.heard(OWNER, 'bot-1', 'tr')
    store.heard(OWNER, 'bot-1', 'en')
    assert store.get(OWNER, 'bot-1') == 'tr'  # the run was broken
    store.heard(OWNER, 'bot-1', 'en')
    assert store.get(OWNER, 'bot-1') == 'en'
    # The model's own report applies at once.
    store.reported(OWNER, 'bot-1', 'es')
    assert store.get(OWNER, 'bot-1') == 'es'


def test_preferences_are_per_owner_and_agent_and_survive_restarts(tmp_path):
    path = tmp_path / 'voice_language.json'
    store = VoiceLanguagePreferences(path)
    store.reported(OWNER, 'bot-1', 'tr')
    store.reported({'kind': 'account', 'uid': 'owner-2'}, 'bot-1', 'en')
    store.reported(OWNER, 'bot-2', 'es')
    restarted = VoiceLanguagePreferences(path)
    assert restarted.get(OWNER, 'bot-1') == 'tr'
    assert restarted.get({'kind': 'account', 'uid': 'owner-2'}, 'bot-1') == 'en'
    assert restarted.get(OWNER, 'bot-2') == 'es'
    assert restarted.get({'kind': 'host'}, 'bot-1') is None


def test_an_unreadable_store_is_empty_and_the_file_stays_bounded(tmp_path):
    path = tmp_path / 'voice_language.json'
    path.write_text('not json')
    store = VoiceLanguagePreferences(path)
    assert store.get(OWNER, 'bot-1') is None
    store.reported(OWNER, 'bot-1', 'tr')
    assert store.get(OWNER, 'bot-1') == 'tr'
    data = {f'owner-{index}:bot': {'language': 'en', 'updatedAt': f'2026-01-01T00:00:{index % 60:02d}'}
            for index in range(MAX_OWNERS + 50)}
    path.write_text(json.dumps(data))
    store.reported(OWNER, 'bot-1', 'tr')
    assert len(json.loads(path.read_text())) == MAX_OWNERS
    assert store.get(OWNER, 'bot-1') == 'tr'


@pytest.fixture
def voice(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    base = VoiceSessions(SessionManager(tmp_path / 'workspace'),
                         languages=VoiceLanguagePreferences(tmp_path / 'voice_language.json'))
    return base.for_principal(VoicePrincipal('owner-1', 'host-1', 9999999999, 'credential-1'))


def open_call(voice, conversation, language='en', connection='connection-1'):
    return voice.open({'conversationId': conversation, 'connectionId': connection, 'language': language},
                      profile='default', bot_id='agent-1')


def say(voice, conversation, text, ordinal):
    voice.append({'conversationId': conversation, 'connectionId': 'connection-1', 'messageId': f'message-{ordinal}',
                  'ordinal': ordinal, 'role': 'user', 'text': text})


def test_a_new_conversation_opens_in_the_language_the_owner_speaks(voice):
    # An English interface, a Turkish speaker.
    assert open_call(voice, 'conversation-1')['spokenLanguage'] == 'en'
    say(voice, 'conversation-1', 'Benim hakkımda ne biliyorsun, bana söyle', 1)
    assert open_call(voice, 'conversation-2')['spokenLanguage'] == 'tr'
    # The interface language still names the conversation; an existing one keeps its language.
    assert open_call(voice, 'conversation-1', connection='connection-1')['spokenLanguage'] == 'en'


def test_another_owner_or_agent_is_not_affected(voice, tmp_path):
    open_call(voice, 'conversation-1')
    say(voice, 'conversation-1', 'Benim hakkımda ne biliyorsun, bana söyle', 1)
    other_owner = voice.for_principal(VoicePrincipal('owner-2', 'host-1', 9999999999, 'credential-2'))
    assert open_call(other_owner, 'conversation-9')['spokenLanguage'] == 'en'
    other_agent = voice.open({'conversationId': 'conversation-8', 'connectionId': 'connection-1', 'language': 'en'},
                             profile='writer', bot_id='agent-2')
    assert other_agent['spokenLanguage'] == 'en'


def test_the_models_language_report_is_kept_for_the_next_conversation(voice):
    open_call(voice, 'conversation-1')
    voice.set_language({'conversationId': 'conversation-1', 'connectionId': 'connection-1',
                        'providerEventId': 1, 'language': 'es'})
    assert open_call(voice, 'conversation-2')['spokenLanguage'] == 'es'


def test_without_a_store_conversations_open_in_the_clients_language(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    plain = VoiceSessions(SessionManager(tmp_path / 'workspace'))
    opened = plain.open({'conversationId': 'conversation-1', 'connectionId': 'connection-1', 'language': 'en'},
                        profile='default', bot_id='agent-1')
    plain.append({'conversationId': 'conversation-1', 'connectionId': 'connection-1', 'messageId': 'message-1',
                  'ordinal': 1, 'role': 'user', 'text': 'Benim hakkımda ne biliyorsun, bana söyle'})
    assert opened['spokenLanguage'] == 'en'
