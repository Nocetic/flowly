"""Goal controls fail closed at the durable mutation boundary."""
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.agent.loop import AgentLoop
from flowly.channels import feature_rpc
from flowly.goals.manager import GoalManager
from flowly.goals.store import GoalStore, GoalStoreConflictError


@pytest.fixture
def owner(tmp_path):
    agent = object.__new__(AgentLoop)
    agent.goal_manager = GoalManager(GoalStore(tmp_path), Mock())
    agent.goal_runtime = Mock()
    agent.abort_autonomous_run = Mock()
    agent.context_epoch = lambda _: 0
    agent.publish_goal_snapshot = AsyncMock()
    agent._gateway_server = None
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['replace', 'complete'])
async def test_late_stop_cannot_cancel_a_changed_goal_or_its_runtime(owner, change):
    state = owner.goal_manager.set('desktop:work', 'Prepare the report')
    if change == 'replace':
        current = owner.goal_manager.set('desktop:work', 'A new goal')
    else:
        def complete(goal):
            goal.status = type(goal.status)('done')
            return goal
        current = owner.goal_manager.store.compare_and_update(state, complete)
    with pytest.raises(GoalStoreConflictError):
        await owner.goal_control('desktop:work', 'stop',
                                 expected_goal_id=state.goal_id, expected_revision=state.revision)
    assert owner.goal_manager.get('desktop:work') == current
    owner.goal_runtime.cancel_session.assert_not_called()
    owner.abort_autonomous_run.assert_not_called()
    owner.publish_goal_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_guarded_stop_persists_before_cancelling_runtime(owner):
    state = owner.goal_manager.set('desktop:work', 'Prepare the report')
    def cancelled(session):
        assert owner.goal_manager.get(session).status.value == 'cleared'
    owner.goal_runtime.cancel_session.side_effect = cancelled
    result = await owner.goal_control('desktop:work', 'stop',
                                      expected_goal_id=state.goal_id, expected_revision=state.revision)
    assert result['goal']['status'] == 'cleared'
    owner.abort_autonomous_run.assert_called_once_with('desktop:work')


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', [
    {'expectedGoalId': 'g'}, {'expectedRevision': 1},
    {'expectedGoalId': '', 'expectedRevision': 1},
    {'expectedGoalId': 'g', 'expectedRevision': True},
    {'expectedGoalId': 'g', 'expectedRevision': -1},
])
async def test_incomplete_goal_guards_never_fall_back_to_unguarded_control(monkeypatch, guard):
    callback = AsyncMock()
    monkeypatch.setattr(feature_rpc, '_goal_control_cb', callback)
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.goal_stop({'sessionKey': 'desktop:work', **guard})
    assert error.value.code == 'INVALID_PARAMS'
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_goal_conflict_has_a_stable_rpc_receipt(owner, monkeypatch):
    state = owner.goal_manager.set('desktop:work', 'Prepare the report')
    owner.goal_manager.set('desktop:work', 'A new goal')
    monkeypatch.setattr(feature_rpc, '_goal_control_cb', owner.goal_control)
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.goal_stop({
            'sessionKey': 'desktop:work', 'expectedGoalId': state.goal_id,
            'expectedRevision': state.revision,
        })
    assert error.value.code == 'GOAL_STATE_CHANGED'
    owner.abort_autonomous_run.assert_not_called()
