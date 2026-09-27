"""Files sent into Live Voice are stored only on the agent, once per identity."""
import base64
from pathlib import Path

import pytest

from flowly.live_voice.authority import RequestOwner, request_owner_scope
from flowly.live_voice.service import LiveVoiceService
from flowly.live_voice.sessions import VoiceError, VoiceSessions
from flowly.session.manager import SessionManager
from flowly.session.ownership import SessionAccessError

CONVERSATION = 'conversation-1'


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    return tmp_path


def open_voice(home, owner: RequestOwner | None = None):
    sessions = VoiceSessions(SessionManager(home / 'workspace'))
    if owner is not None:
        sessions = sessions.for_owner(owner)
    sessions.open({'conversationId': CONVERSATION, 'connectionId': 'connection-1', 'language': 'en'},
                  profile='default', bot_id='agent-1')
    return sessions


def request(**changes):
    return {'conversationId': CONVERSATION, 'commandId': 'command-1',
            'attachments': [{'fileName': 'report.txt', 'mimeType': 'text/plain', 'content': encoded(b'quarterly numbers')}],
            **changes}


@pytest.mark.asyncio
async def test_files_are_stored_on_the_agent_once_and_listed_with_history(home):
    sessions = open_voice(home)
    service = LiveVoiceService(sessions, lambda: (None, None))
    result = await service.call('voice.attachments', request())
    [file] = result['record']['files']
    assert file['fileName'] == 'report.txt' and file['mimeType'] == 'text/plain' and file['size'] == 17
    assert Path(file['path']).read_bytes() == b'quarterly numbers'
    assert Path(file['path']).is_relative_to(home / 'home' / 'media')
    retry = await service.call('voice.attachments', request(attachments=[{'fileName': 'other.txt',
                                                                          'mimeType': 'text/plain', 'content': encoded(b'x')}]))
    assert retry['replayed'] is True and retry['record'] == result['record']
    assert len([path for path in (home / 'home' / 'media').rglob('*') if path.is_file() and path.suffix == '.txt']) == 1
    history = service.call('voice.history', {'conversationId': CONVERSATION})
    assert [row['id'] for row in history['attachments']] == ['command-1']


@pytest.mark.asyncio
@pytest.mark.parametrize('row', [
    {'fileName': 'a.png', 'mimeType': 'image/png', 'cdnUrl': 'https://cdn.example/a.png'},
    {'fileName': 'a.txt', 'mimeType': 'text/plain', 'filePath': '/etc/passwd'},
    {'fileName': '../escape.txt', 'mimeType': 'text/plain', 'content': 'eA=='},
    {'fileName': 'a.txt', 'mimeType': 'not a type', 'content': 'eA=='},
    {'fileName': 'a.txt', 'mimeType': 'text/plain', 'content': 'not base64!'},
    {'fileName': 'a.txt', 'mimeType': 'text/plain', 'content': ''},
])
async def test_only_inline_named_valid_content_is_accepted(home, row):
    sessions = open_voice(home)
    with pytest.raises(VoiceError):
        await LiveVoiceService(sessions, lambda: (None, None)).call('voice.attachments', request(attachments=[row]))
    assert sessions.history({'conversationId': CONVERSATION})['attachments'] == []


@pytest.mark.asyncio
async def test_file_count_and_total_size_are_bounded(home, monkeypatch):
    from flowly.live_voice import attachments

    sessions = open_voice(home)
    service = LiveVoiceService(sessions, lambda: (None, None))
    row = {'fileName': 'a.txt', 'mimeType': 'text/plain', 'content': encoded(b'x')}
    with pytest.raises(VoiceError):
        await service.call('voice.attachments', request(attachments=[row] * 11))
    monkeypatch.setattr(attachments, 'MAX_TOTAL_BYTES', 10)
    with pytest.raises(VoiceError) as error:
        await service.call('voice.attachments', request(attachments=[{**row, 'content': encoded(b'x' * 11)}]))
    assert error.value.code == 'LIMIT'


@pytest.mark.asyncio
async def test_an_ended_call_or_another_connection_cannot_add_files(home):
    sessions = open_voice(home)
    service = LiveVoiceService(sessions, lambda: (None, None))
    with pytest.raises(VoiceError) as error:
        await service.call('voice.attachments', request(connectionId='connection-2'))
    assert error.value.code == 'STALE_CONNECTION'
    sessions.end({'conversationId': CONVERSATION, 'connectionId': 'connection-1'})
    with pytest.raises(VoiceError) as error:
        await service.call('voice.attachments', request())
    assert error.value.code == 'STALE_CONNECTION'


@pytest.mark.asyncio
async def test_account_owned_files_stay_with_their_owner(home):
    owner = RequestOwner('account-a')
    with request_owner_scope(owner):
        sessions = open_voice(home, owner)
        result = await LiveVoiceService(sessions, lambda: (None, None)).call('voice.attachments', request())
    assert Path(result['record']['files'][0]['path']).read_bytes() == b'quarterly numbers'
    other = VoiceSessions(SessionManager(home / 'workspace')).for_owner(RequestOwner('account-b'))
    # The RPC layer maps both to NOT_FOUND; another account never sees the conversation.
    with request_owner_scope(RequestOwner('account-b')), pytest.raises((VoiceError, SessionAccessError)):
        await LiveVoiceService(other, lambda: (None, None)).call('voice.attachments', request(commandId='command-2'))


def test_deleting_the_conversation_removes_attachment_records(home):
    sessions = open_voice(home)
    sessions.record_attachments(CONVERSATION, 'command-1', [{'fileName': 'a.txt', 'mimeType': 'text/plain',
                                                             'size': 1, 'path': '/tmp/a.txt'}])
    sessions.end({'conversationId': CONVERSATION, 'connectionId': 'connection-1'})
    assert sessions.delete({'conversationId': CONVERSATION, 'ifEmpty': True})['deleted'] is False
    assert sessions.delete({'conversationId': CONVERSATION})['deleted'] is True
