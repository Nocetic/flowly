from concurrent.futures import ThreadPoolExecutor

import pytest

from flowly.session.commands import ChatCommandConflictError, ChatCommandStore


def test_acceptance_survives_connections_and_terminal_replay(tmp_path):
    path = tmp_path / "commands.sqlite3"
    first = ChatCommandStore(path)
    second = ChatCommandStore(path)
    assert first.accept("desktop:voice-job", "command-1", {"message": "Report"})[0]
    created, receipt = second.accept("desktop:voice-job", "command-1", {"message": "Report"})
    assert not created
    assert receipt == {"runId": "command-1", "status": "accepted", "replayed": True}
    first.settle("desktop:voice-job", "command-1", "aborted")
    # A late successful event must not turn an interrupted run into success.
    second.settle("desktop:voice-job", "command-1", "completed")
    first.close()
    second.close()
    restarted = ChatCommandStore(path, owner_id="new-process")
    assert restarted.lookup("desktop:voice-job", "command-1")["status"] == "aborted"
    assert restarted.lookup("desktop:other", "command-1") is None
    restarted.close()


def test_crash_does_not_redispatch_an_uncertain_command(tmp_path):
    path = tmp_path / "commands.sqlite3"
    first = ChatCommandStore(path, owner_id="before-crash")
    first.accept("web:one", "command-1", {"message": "Change a file"})
    first.close()
    restarted = ChatCommandStore(path, owner_id="after-crash")
    created, receipt = restarted.accept("web:one", "command-1", {"message": "Change a file"})
    assert not created
    assert receipt["status"] == "status_unknown"
    restarted.close()


@pytest.mark.parametrize("session,params", [
    ("web:other", {"message": "Report"}),
    ("web:one", {"message": "Different work"}),
    ("web:one", {"message": "Report", "modelOverride": "different-model"}),
])
def test_reused_command_rejects_different_request(tmp_path, session, params):
    store = ChatCommandStore(tmp_path / "commands.sqlite3")
    store.accept("web:one", "command-1", {"message": "Report"})
    with pytest.raises(ChatCommandConflictError):
        store.accept(session, "command-1", params)
    store.close()


def test_concurrent_connections_only_accept_once(tmp_path):
    path = tmp_path / "commands.sqlite3"
    stores = [ChatCommandStore(path) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(lambda s: s.accept("web:one", "command-1", {"message": "Report"})[0], stores))
    assert sum(accepted) == 1
    for store in stores:
        store.close()


def observation(goal=None, *, available=True):
    return {'available': available, 'goal': goal}


def goal_snapshot(goal_id='goal-1', status='active', revision=1, **extra):
    return {'goalId': goal_id, 'status': status, 'revision': revision, **extra}


def test_execution_goal_binding_survives_restart_and_cannot_be_replaced(tmp_path):
    path = tmp_path / 'commands.sqlite3'
    store = ChatCommandStore(path, owner_id='first')
    store.accept('desktop:voice-work:one', 'initial', {'message': 'Report'})
    store.begin_execution('desktop:voice-work:one', 'initial', observation())
    assert store.lookup('desktop:voice-work:one', 'initial')['goalBinding']['state'] == 'pending'
    created = goal_snapshot(createdByRunId='initial')
    store.finish_execution('desktop:voice-work:one', 'initial', observation(created), 'completed')
    binding = store.lookup('desktop:voice-work:one', 'initial')['goalBinding']
    assert binding == {'version': 1, 'state': 'goal', 'goalId': 'goal-1', 'revision': 1}
    store.close()
    restarted = ChatCommandStore(path, owner_id='second')
    restarted.begin_execution('desktop:voice-work:one', 'initial', observation(goal_snapshot('replacement')))
    restarted.finish_execution('desktop:voice-work:one', 'initial', observation(), 'error')
    assert restarted.lookup('desktop:voice-work:one', 'initial')['goalBinding'] == binding
    assert restarted.lookup('desktop:voice-work:one', 'initial')['status'] == 'completed'
    restarted.close()


@pytest.mark.parametrize('before,after,state,goal_id', [
    (observation(), observation(), 'none', None),
    (observation(available=False), observation(), 'unknown', None),
    (observation(), observation(available=False), 'unknown', None),
    (observation(), observation(goal_snapshot(createdByRunId='unrelated')), 'unknown', None),
    (observation(goal_snapshot(status='done')), observation(goal_snapshot(status='done')), 'none', None),
    (observation(goal_snapshot(status='done')), observation(goal_snapshot('new', createdByRunId='run')), 'goal', 'new'),
    (observation(goal_snapshot()), observation(goal_snapshot('replacement')), 'goal', 'goal-1'),
    (observation(goal_snapshot()), observation(), 'goal', 'goal-1'),
])
def test_goal_binding_uses_causal_turn_evidence(tmp_path, before, after, state, goal_id):
    store = ChatCommandStore(tmp_path / 'commands.sqlite3')
    try:
        store.accept('session', 'run', {'message': 'Report'})
        store.begin_execution('session', 'run', before)
        store.finish_execution('session', 'run', after, 'completed')
        binding = store.lookup('session', 'run')['goalBinding']
        assert binding['state'] == state
        assert binding.get('goalId') == goal_id
    finally:
        store.close()


def test_goal_binding_rejects_wrong_session_and_unobserved_completion(tmp_path):
    store = ChatCommandStore(tmp_path / 'commands.sqlite3')
    try:
        store.accept('session', 'run', {'message': 'Report'})
        store.begin_execution('wrong-session', 'run', observation(goal_snapshot()))
        assert 'goalBinding' not in store.lookup('session', 'run')
        store.finish_execution('session', 'run', observation(goal_snapshot()), 'completed')
        assert store.lookup('session', 'run')['goalBinding']['state'] == 'unknown'
    finally:
        store.close()
