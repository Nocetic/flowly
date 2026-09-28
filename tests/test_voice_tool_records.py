"""Display records for Relay-run voice tools (web search): stored on the agent,
idempotent, bounded, and never able to name or run another tool."""
import pytest

from flowly.live_voice.service import METHODS, LiveVoiceService
from flowly.live_voice.sessions import VoiceError, VoiceSessions
from flowly.session.manager import SessionManager

CONVERSATION = 'conversation-1'
TEXT = 'Results for: weather\n\n1. Forecast\n   https://weather.example'


@pytest.fixture
def voice(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    sessions = VoiceSessions(SessionManager(tmp_path / 'workspace'))
    sessions.open({'conversationId': CONVERSATION, 'connectionId': 'connection-1', 'language': 'tr'},
                  profile='default', bot_id='agent-1')
    return LiveVoiceService(sessions, board=lambda: (None, None))


def call(**changes):
    return {'conversationId': CONVERSATION, 'connectionId': 'connection-1', 'commandId': 'b' * 64, 'name': 'web_search',
            'arguments': {'query': 'weather'}, 'status': 'running', 'anchorMessageId': 'connection-1:m1', **changes}


def test_records_a_search_once_and_returns_it_with_history(voice):
    assert 'voice.tools.record' in METHODS
    running = voice.call('voice.tools.record', call())['record']
    assert running['status'] == 'running' and running['name'] == 'web_search'
    done = voice.call('voice.tools.record', call(status='completed', output=TEXT))['record']
    assert done['status'] == 'completed' and done['output'] == TEXT and done['finishedAt']
    # A replay neither rewrites nor duplicates the settled record.
    again = voice.call('voice.tools.record', call(status='failed'))['record']
    assert again['status'] == 'completed'
    tools = voice.call('voice.history', {'conversationId': CONVERSATION})['tools']
    assert [(row['name'], row['arguments'], row['anchorMessageId']) for row in tools] == [
        ('web_search', {'query': 'weather'}, 'connection-1:m1')]


def test_a_settled_record_may_arrive_without_the_running_one(voice):
    record = voice.call('voice.tools.record', call(status='failed'))['record']
    assert record['status'] == 'failed' and record['output'] is None


@pytest.mark.parametrize('changes', [
    {'name': 'exec', 'arguments': {'command': 'rm -rf /'}},
    {'arguments': {'query': 'x', 'url': 'http://internal'}},
    {'arguments': {'query': 'x' * 401}},
    {'arguments': {'query': 'x', 'news': 'yes'}},
    {'status': 'denied'},
    {'status': 'running', 'output': TEXT},
    {'status': 'completed', 'output': 'x' * 16_385},
    {'status': 'completed'},
])
def test_rejects_other_tools_and_unbounded_records(voice, changes):
    with pytest.raises(VoiceError) as error:
        voice.call('voice.tools.record', call(**changes))
    assert error.value.code == 'INVALID_PARAMS'


def test_an_identity_keeps_its_arguments_and_needs_the_live_connection(voice):
    voice.call('voice.tools.record', call())
    with pytest.raises(VoiceError) as conflict:
        voice.call('voice.tools.record', call(arguments={'query': 'other'}))
    assert conflict.value.code == 'CONFLICT'
    with pytest.raises(VoiceError) as stale:
        voice.call('voice.tools.record', call(commandId='c' * 64, connectionId='connection-2'))
    assert stale.value.code == 'STALE_CONNECTION'
