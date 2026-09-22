import time
from unittest.mock import AsyncMock

import pytest

from flowly.board.store import BoardError
from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from tests import test_voice_board

board = test_voice_board.board
dispatch = test_voice_board.dispatch


A, B = RequestOwner('account-a'), RequestOwner('account-b')


def owned(orchestrator, owner, key, **changes):
    with request_owner_scope(owner):
        return dispatch(orchestrator, command_id=key, conversation_id='shared-conversation-id', title=key, **changes)


@pytest.fixture
def cards(board):
    store, orchestrator = board
    b = owned(orchestrator, B, 'b-private')
    a = owned(orchestrator, A, 'a-private')
    second = owned(orchestrator, A, 'a-second')
    host = owned(orchestrator, HOST_OWNER, 'host-private')
    shared = store.add_card('Shared host context')
    store.add_note(a.id, 'user', 'Account A note')
    claim = store.claim_card(a.id, worker='creative')
    store.set_worker_run_id(a.id, claim.claim_token, 'account-a-run')
    store.finish_claim(a.id, claim.claim_token, outcome='done', result='Account A result')
    return store, orchestrator, a, second, b, host, shared


@pytest.mark.parametrize('owner', [A, B, HOST_OWNER])
def test_board_lists_counts_and_limit_are_scoped_before_projection(cards, owner):
    store, _, a, second, b, host, shared = cards
    visible = {A: {a.id, second.id, shared.id}, B: {b.id, shared.id}, HOST_OWNER: {host.id, shared.id}}[owner]
    with request_owner_scope(owner):
        assert {card.id for card in store.list_cards()} == visible
        snapshot = store.snapshot(with_notes=True)
        assert snapshot['total'] == len(visible)
        assert sum(snapshot['counts'].values()) == len(visible)
        assert {card['id'] for column in snapshot['columns'] for card in column['cards']} == visible
        assert len(store.list_cards(limit=1)) == 1
        assert store.list_cards(limit=1)[0].id in visible
        runnable = store.list_dispatchable(limit=1)
        assert len(runnable) == 1
        assert runnable[0].id in visible
    assert len(store.list_cards()) == 5


@pytest.mark.parametrize('owner', [B, HOST_OWNER])
def test_wrong_owner_cannot_read_card_notes_runs_commands_or_events(cards, owner):
    store, _, a, _, _, _, _ = cards
    event = store.get_events(a.id)[-1]
    with request_owner_scope(owner):
        assert store.get_card(a.id, with_notes=True) is None
        assert store.get_card_by_idempotency_key('a-private') is None
        assert store.get_runs(a.id) == []
        assert store.get_events(a.id) == []
        assert store.get_dependencies(a.id) == {'parents': [], 'children': []}
        assert store.voice_event('shared-conversation-id', event['id']) is None
        with pytest.raises(BoardError):
            store.voice_commands.list('shared-conversation-id', a.id)


@pytest.mark.parametrize('owner', [B, HOST_OWNER])
@pytest.mark.parametrize('operation', ['status', 'run', 'update', 'note', 'assign', 'unassign', 'delete', 'claim'])
def test_wrong_owner_mutations_leave_the_database_unchanged(cards, owner, operation):
    store, _, a, second, _, _, _ = cards
    target = second if operation in {'assign', 'unassign', 'claim'} else a
    before = '\n'.join(store._conn.iterdump())
    calls = {
        'status': lambda: store.set_status(target.id, 'archived'),
        'run': lambda: store.set_run_id(target.id, 'replacement-run'),
        'update': lambda: store.update_card(target.id, title='Replacement'),
        'note': lambda: store.add_note(target.id, 'user', 'Replacement'),
        'assign': lambda: store.assign_card(target.id, profile='creative', bot_id='replacement'),
        'unassign': lambda: store.unassign_card(target.id),
        'delete': lambda: store.delete_card(target.id),
        'claim': lambda: store.claim_card(target.id, worker='creative'),
    }
    with request_owner_scope(owner):
        try:
            result = calls[operation]()
            assert result is None or result is False
        except BoardError:
            pass
    assert '\n'.join(store._conn.iterdump()) == before


def test_bulk_delete_affects_only_visible_cards_and_does_not_count_hidden_claims(cards):
    store, _, a, second, b, host, shared = cards
    with request_owner_scope(B):
        assert store.delete_by_status('done') == 0
        assert store.delete_by_status('ready') == 1
    assert store.get_card(b.id) is None
    assert store.get_card(a.id) is not None
    assert store.get_card(second.id) is not None
    assert store.get_card(host.id) is not None
    assert store.get_card(shared.id) is not None


def test_events_and_instruction_snapshots_do_not_merge_equal_conversation_ids(cards):
    store, _, a, second, b, host, _ = cards
    for number, card in enumerate((a, second, b, host)):
        store.voice_commands.enqueue(conversation_id='shared-conversation-id', card_id=card.id,
                                     command_id=f'steer-{number}', expected_revision=store.get_card(card.id).revision, text='Continue')
    with request_owner_scope(A):
        snapshot = store.voice_events('shared-conversation-id', limit=1)
        assert len(snapshot['events']) == 1
        assert {card['id'] for card in snapshot['cards']} == {a.id, second.id}
        assert set(store.voice_commands.latest_instructions('shared-conversation-id')) == {a.id, second.id}
        events = []
        after = 0
        while True:
            page = store.voice_events('shared-conversation-id', after=after, limit=2)
            events.extend(page['events'])
            if not page['hasMore']:
                break
            after = page['cursor']
        assert {event['cardId'] for event in events} == {a.id, second.id}


@pytest.mark.asyncio
async def test_hidden_manual_task_cannot_be_cancelled_through_orchestrator(cards):
    store, orchestrator, a, _, _, _, _ = cards
    fake = AsyncMock()
    fake.done = lambda: False
    orchestrator._manual_tasks[a.id] = fake
    with request_owner_scope(B):
        assert await orchestrator.cancel_card(a.id) is False
    fake.cancel.assert_not_called()
    assert store.get_card(a.id).status == 'done'


def test_dependencies_cannot_publish_a_private_card_into_shared_or_other_account_work(cards):
    store, _, a, second, b, _, shared = cards
    with request_owner_scope(A):
        for parent, child in ((a, shared), (shared, a), (a, b)):
            with pytest.raises(BoardError):
                store.link_cards(parent.id, child.id)
        store.link_cards(a.id, second.id)
        assert store.get_dependencies(second.id)['parents'] == [a.id]
        with pytest.raises(BoardError):
            store.add_card('Shared child', parent_id=a.id)


def test_expiry_recovery_and_legacy_reset_do_not_mutate_another_accounts_claim(cards):
    store, _, _, second, b, _, _ = cards
    for card in (second, b):
        store.claim_card(card.id, worker='creative', lease_seconds=15)
    with request_owner_scope(B):
        assert store.recover_expired_claims(now=time.time() + 20) == 1
        assert store.reset_orphaned(set()) == 0
    assert store.get_card(second.id).claim_token


@pytest.mark.parametrize('owner', [B, HOST_OWNER])
def test_known_command_and_claim_ids_do_not_authorize_control_or_private_receipts(cards, owner):
    from tests.test_voice_commands import cancel

    store, _, a, second, _, _, _ = cards
    claim = store.claim_card(second.id, worker='creative')
    initial = store.voice_commands.claimed(second.id, claim.claim_token)
    store.set_worker_run_id(second.id, claim.claim_token, 'worker-a')
    stop = cancel(store, store.get_card(second.id))
    _, response = store.voice_commands.response_intent(conversation_id=a.voice_conversation_id, card_id=a.id,
        command_id='answer-a', expected_revision=store.get_card(a.id).revision,
        payload={'requestId': 'request-a', 'answer': 'Private answer'})
    before = '\n'.join(store._conn.iterdump())
    operations = [
        lambda: store.voice_commands.claimed(second.id, claim.claim_token),
        lambda: store.voice_commands.next_locked(claim),
        lambda: store.voice_commands.seed_locked(second.id, 'Injected command', time.time()),
        lambda: store.voice_commands.claim_locked(initial['commandId'], claim.claim_token, time.time()),
        lambda: store.voice_commands.applied_locked(second.id, claim.claim_token, 'replacement', time.time()),
        lambda: store.voice_commands.finish_locked(claim, 'done', time.time()),
        lambda: store.voice_commands.bind_stop_goal(stop['commandId'], {'goalId': None, 'statusAtRequest': None}),
        lambda: store.voice_commands.settle_stop(stop['commandId'], status='stopped'),
        lambda: store.voice_commands.settle_response(response['commandId'], 'applied'),
    ]
    with request_owner_scope(owner), store._lock:
        assert store.voice_commands.pending_stops() == []
        for operation in operations:
            with pytest.raises(BoardError):
                operation()
    assert '\n'.join(store._conn.iterdump()) == before


@pytest.mark.parametrize('owner', [A, B])
def test_recovery_pagination_and_settlement_respect_the_card_owner(cards, owner):
    store, _, _, second, b, _, _ = cards
    for card in (second, b):
        store.claim_card(card.id, worker='creative', lease_seconds=15)
    store.recover_expired_claims(now=time.time() + 20)
    all_candidates = store.voice_commands.pending_reconciliations()
    expected = second.id if owner == A else b.id
    foreign = next(candidate for candidate in all_candidates if candidate['cardId'] != expected)
    before = '\n'.join(store._conn.iterdump())
    with request_owner_scope(owner):
        own = store.voice_commands.pending_reconciliations(limit=1)
        assert len(own) == 1 and own[0]['cardId'] == expected
        assert store.voice_commands.settle_reconciliation(foreign, {
            'runId': foreign['runId'], 'status': 'completed', 'response': 'Injected result',
        }) is None
    assert '\n'.join(store._conn.iterdump()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
async def test_board_rpc_scopes_detail_and_mutations_before_any_worker_action(cards, monkeypatch, transport):
    import flowly.profile as profiles
    from flowly.bus.queue import MessageBus
    from flowly.channels import feature_rpc
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig
    from flowly.gateway.server import GatewayServer
    from flowly.live_voice.access import VoiceAccessVerifier
    from tests.test_chat_command_transport import Socket
    from tests.test_voice_access import HOST, fixture

    store, orchestrator, a, _, b, _, shared = cards
    token, _, keys, _, _ = fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    monkeypatch.setattr(feature_rpc, '_board_provider', lambda: (store, orchestrator))
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    server = GatewayServer(board_store=store, board_orchestrator=orchestrator)
    socket = Socket()

    async def rpc(method, **params):
        frame = {'id': 'rpc-1', 'method': method, 'params': {**params, 'voiceAccess': token({'sub': B.uid})}}
        if transport == 'gateway':
            await server._handle_ws_rpc(socket, 'client', frame)
        else:
            from tests.relay_voice_helpers import relay_rpc

            await relay_rpc(channel, socket, {**frame, 'sessionId': 'relay-1'}, uid=B.uid)
        return socket.sent[-1]

    snapshot = (await rpc('board.snapshot'))['result']['snapshot']
    assert snapshot['total'] == 2
    assert {card['id'] for column in snapshot['columns'] for card in column['cards']} == {b.id, shared.id}
    assert (await rpc('board.card', cardId=a.id))['result'] == {'card': None, 'run': None}
    before = '\n'.join(store._conn.iterdump())
    for action in ('move', 'note', 'delete', 'run', 'cancel', 'retry'):
        result = await rpc('board.action', action=action, cardId=a.id, status='archived', text='Injected note')
        if action == 'cancel':
            assert 'error' in result
    assert '\n'.join(store._conn.iterdump()) == before
    assert 'Account A' not in repr(socket.sent)


@pytest.mark.asyncio
async def test_real_http_board_never_inherits_internal_authority_and_accepts_only_verified_header(cards, monkeypatch):
    import aiohttp

    import flowly.profile as profiles
    from flowly.channels import feature_rpc
    from flowly.gateway.server import GatewayServer
    from flowly.live_voice.access import VoiceAccessVerifier
    from tests.test_voice_access import HOST, fixture

    store, orchestrator, a, second, b, host, shared = cards
    token, _, keys, calls, _ = fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    server = GatewayServer(host='127.0.0.1', port=0, auth_token='g' * 48, require_loopback_auth=True,
                           advertise_control=False, board_store=store, board_orchestrator=orchestrator)
    await server.start()
    try:
        base = f'http://127.0.0.1:{server.port}'
        async with aiohttp.ClientSession() as session:
            denied = await session.get(base + '/api/board', headers={'X-Flowly-Voice-Access': token({'sub': A.uid})})
            assert denied.status == 401 and not calls
            expected = {None: {host.id, shared.id}, A: {a.id, second.id, shared.id}, B: {b.id, shared.id}}
            for owner, ids in expected.items():
                headers = {'Authorization': 'Bearer ' + 'g' * 48}
                if owner is not None:
                    headers['X-Flowly-Voice-Access'] = token({'sub': owner.uid})
                reply = await session.get(base + '/api/board', headers=headers)
                snapshot = await reply.json()
                assert reply.status == 200
                assert reply.headers['Cache-Control'] == 'no-store'
                assert {card['id'] for column in snapshot['columns'] for card in column['cards']} == ids
            headers = {'Authorization': 'Bearer ' + 'g' * 48, 'X-Flowly-Voice-Access': token({'sub': B.uid})}
            before = '\n'.join(store._conn.iterdump())
            reply = await session.post(base + '/api/board/action', headers=headers,
                                       json={'action': 'note', 'cardId': a.id, 'text': 'Injected HTTP note'})
            assert reply.status == 400
            assert '\n'.join(store._conn.iterdump()) == before
            for invalid in ('', 'bad-certificate'):
                reply = await session.get(base + '/api/board', headers={**headers, 'X-Flowly-Voice-Access': invalid})
                assert reply.status == 401
                assert reply.headers['Cache-Control'] == 'no-store'
                assert (await reply.json())['code'] == 'VOICE_AUTH_REQUIRED'
            duplicate = await session.get(base + '/api/board', headers=[
                ('Authorization', 'Bearer ' + 'g' * 48),
                ('X-Flowly-Voice-Access', token({'sub': A.uid})),
                ('X-Flowly-Voice-Access', token({'sub': B.uid})),
            ])
            assert duplicate.status == 401
    finally:
        await server.stop()
