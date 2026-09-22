import pytest

import flowly.profile as profiles
from flowly.board.orchestrator import BoardOrchestrator
from flowly.board.store import BoardError, BoardStore
from flowly.live_voice.authority import (
    HOST_OWNER,
    RequestOwner,
    current_request_owner,
    request_owner_scope,
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.delenv('FLOWLY_HOME', raising=False)
    monkeypatch.setattr(profiles, '_DEFAULT_HOME', home)
    monkeypatch.setattr(profiles, '_PROFILES_ROOT', home / 'profiles')
    profiles.create_profile('creative', local_runtime=True)
    store = BoardStore(tmp_path / 'board.db')
    yield store, BoardOrchestrator(store, lambda *_args, **_kwargs: None)
    store.close()


def dispatch(orchestrator, **changes):
    return orchestrator.dispatch_voice(**{
        'conversation_id': 'voice-1', 'command_id': 'command-1',
        'profile': 'creative', 'title': 'Prepare the report', 'body': 'Include sources',
        **changes,
    })


def test_dispatch_is_ready_and_visible_in_one_durable_write(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    assert card.status == 'ready'
    assert card.assignee_profile == 'creative'
    assert card.session_key == f'desktop:voice-work:{card.id}'
    assert card.to_dict()['executionMode'] == 'voice'
    assert 'request_fingerprint' not in card.to_dict()
    assert dispatch(orchestrator).id == card.id
    reopened = BoardStore(store.db_path)
    assert reopened.get_card(card.id).session_key == card.session_key
    assert [c.id for c in reopened.list_dispatchable()] == [card.id]
    reopened.close()


@pytest.mark.parametrize('changes', [
    {'title': 'Different work'}, {'profile': 'default'}, {'conversation_id': 'voice-2'},
])
def test_dispatch_rejects_conflicting_command_without_an_extra_card(board, changes):
    store, orchestrator = board
    dispatch(orchestrator)
    with pytest.raises(BoardError, match='different request'):
        dispatch(orchestrator, **changes)
    assert len(store.list_cards()) == 1


def test_unknown_target_creates_no_work(board):
    store, orchestrator = board
    with pytest.raises(BoardError):
        dispatch(orchestrator, profile='missing')
    assert store.list_cards() == []


def test_account_owner_is_persisted_without_changing_legacy_host_idempotency(board):
    store, orchestrator = board
    owner = RequestOwner('account-a')
    with request_owner_scope(owner):
        card = dispatch(orchestrator)
        assert card.voice_owner_uid == owner.uid
        assert dispatch(orchestrator).id == card.id
    reopened = BoardStore(store.db_path)
    try:
        assert reopened.get_card(card.id).voice_owner_uid == owner.uid
        assert 'voice_owner_uid' not in card.to_dict()
        assert owner.uid not in repr(card.to_dict())
    finally:
        reopened.close()
    for other in (RequestOwner('account-b'), HOST_OWNER):
        with request_owner_scope(other):
            with pytest.raises(BoardError):
                dispatch(orchestrator)
    host_card = dispatch(orchestrator, command_id='host-command')
    assert host_card.voice_owner_uid == ''
    assert dispatch(orchestrator, command_id='host-command').id == host_card.id


@pytest.mark.asyncio
async def test_restarted_dispatcher_restores_the_accepted_owner_in_worker_session(board, tmp_path, monkeypatch):
    from flowly.session.manager import SessionManager
    from flowly.session.ownership import SessionAccessError

    store, orchestrator = board
    with request_owner_scope(RequestOwner('account-a')):
        card = dispatch(orchestrator)
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'worker-home'))
    sessions = SessionManager(tmp_path / 'worker-workspace')
    reopened = BoardStore(store.db_path)

    async def spawn(prompt, **kwargs):
        assert current_request_owner() == RequestOwner('account-a')
        sessions.reserve_voice_work(card.session_key)
        session = sessions.get_or_create(card.session_key)
        session.add_message('assistant', 'Accepted account result')
        sessions.save(session)
        return 'Accepted account result'

    resumed = BoardOrchestrator(reopened, spawn)
    try:
        assert current_request_owner() is None
        await resumed.run_card(card.id, deliver=False)
        assert reopened.get_card(card.id).status == 'done'
        assert sessions.read(card.session_key).metadata['voiceOwner'] == {'kind': 'account', 'uid': 'account-a'}
        with request_owner_scope(RequestOwner('account-b')):
            with pytest.raises(SessionAccessError):
                sessions.get_full_messages(card.session_key)
        assert current_request_owner() is None
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_pending_stop_uses_the_cards_owner_after_the_request_ends(board):
    from tests.test_voice_commands import cancel

    store, orchestrator = board
    with request_owner_scope(RequestOwner('account-a')):
        card = dispatch(orchestrator)
    claim = store.claim_card(card.id, worker='creative')
    store.set_worker_run_id(card.id, claim.claim_token, 'worker-1')
    command = cancel(store, store.get_card(card.id))
    observed = []

    async def control(**kwargs):
        observed.append(current_request_owner())
        assert kwargs['run_id'] == 'worker-1'
        return {'status': 'status_unknown'}

    orchestrator._voice_control = control
    await orchestrator._control_voice(command)
    assert observed == [RequestOwner('account-a')]
    assert current_request_owner() is None


@pytest.mark.asyncio
async def test_passive_recovery_uses_the_durable_card_owner(board):
    from tests.test_voice_recovery import orphan

    store, orchestrator = board
    with request_owner_scope(RequestOwner('account-a')):
        orphan(board)
    observed = []

    async def reconcile(**kwargs):
        observed.append(current_request_owner())
        return {'status': 'status_unknown'}

    orchestrator._voice_reconcile = reconcile
    candidate = store.voice_commands.pending_reconciliations()[0]
    await orchestrator._reconcile_voice(candidate)
    assert observed == [RequestOwner('account-a')]
    assert current_request_owner() is None


def test_agent_recreated_during_dispatch_cannot_replace_the_selected_identity(board):
    store, orchestrator = board
    with pytest.raises(BoardError, match='identity has changed'):
        dispatch(orchestrator, expected_bot_id='previous-agent-identity')
    assert store.list_cards() == []


def test_voice_events_resume_without_other_conversations(board):
    store, orchestrator = board
    first = dispatch(orchestrator)
    dispatch(orchestrator, command_id='other-command', conversation_id='other-voice')
    store.add_note(first.id, author='user', text='Use the newer numbers')
    first_page = store.voice_events('voice-1', limit=1)
    second_page = store.voice_events('voice-1', after=first_page['cursor'])
    assert first_page['hasMore']
    assert all(e['cardId'] == first.id for e in first_page['events'] + second_page['events'])
    assert set(e['id'] for e in first_page['events']).isdisjoint(e['id'] for e in second_page['events'])
    assert [c['id'] for c in second_page['cards']] == [first.id]


def test_completion_snapshot_survives_event_gaps_and_excludes_restarted_work(board):
    store, orchestrator = board
    card = dispatch(orchestrator)
    claim = store.claim_card(card.id, worker='creative')
    store.finish_claim(card.id, claim.claim_token, outcome='done', result='Ready')
    event = next(e for e in store.get_events(card.id) if e['kind'] == 'run_finished')
    # A reconnect need not replay every old progress event to discover results.
    snapshot = store.voice_events('voice-1', after=event['id'] + 100, limit=1)
    assert snapshot['events'] == []
    assert snapshot['cards'][0]['completionEvent'] == {'id': event['id'], 'attempt': 1}
    assert store.voice_events('other-voice')['cards'] == []
    store.set_status(card.id, 'ready')
    assert 'completionEvent' not in store.voice_events('voice-1')['cards'][0]
    store.claim_card(card.id, worker='creative')
    assert 'completionEvent' not in store.voice_events('voice-1')['cards'][0]


@pytest.mark.asyncio
async def test_voice_card_executes_as_an_interactive_task(board):
    store, orchestrator = board
    calls = []

    async def spawn(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return 'Report ready'

    orchestrator._spawn = spawn
    card = dispatch(orchestrator)
    await orchestrator.run_card(card.id, deliver=False)
    assert store.get_card(card.id).status == 'done'
    assert calls[0][1]['interactive'] is True
    assert calls[0][1]['profile'] == 'creative'
    assert calls[0][1]['task_id'] == card.id
