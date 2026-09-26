"""Failed voice work pauses durably without judging or spending another turn."""
from unittest.mock import AsyncMock

import pytest

from flowly.goals.manager import GoalManager
from flowly.goals.models import GoalStatus, GoalVerdict
from flowly.goals.runtime import DeliveredGoalTurn, GoalRuntime
from flowly.goals.store import GoalStore
from tests.test_voice_goal_provenance import execution_loop


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [{'provider_error': True}, {'turn_succeeded': False}, {'latest_response': ''}])
async def test_unsuccessful_turn_pauses_without_overwriting_the_last_verified_run(tmp_path, failure):
    judge = AsyncMock()
    manager = GoalManager(GoalStore(tmp_path), judge)
    state = manager.set('work', 'Finish the report')
    state.turns_used, state.last_run_id = 3, 'last-success'
    manager.store.save(state)
    decision = await manager.evaluate_after_turn('work', **{
        'latest_response': 'Provider unavailable', 'run_id': 'failed-run', **failure,
    })
    assert decision.status is GoalStatus.PAUSED
    assert decision.verdict is GoalVerdict.NEEDS_INPUT
    assert not decision.should_continue
    persisted = GoalStore(tmp_path).get('work')
    assert persisted.turns_used == 3
    assert persisted.last_run_id == 'last-success'
    assert persisted.to_public_dict()['lastFailedRunId'] == 'failed-run'
    assert persisted.last_reason
    assert persisted.paused_reason
    judge.evaluate.assert_not_awaited()
    resumed = manager.resume_for_user_input('work')
    assert resumed.status is GoalStatus.ACTIVE
    assert resumed.goal_id == state.goal_id
    assert resumed.turns_used == 3


class Delivery:
    def __init__(self):
        self.deliver_notice = AsyncMock()
        self.run_continuation = AsyncMock()


@pytest.mark.asyncio
async def test_failure_is_not_hidden_behind_a_pending_plan(tmp_path):
    manager = GoalManager(GoalStore(tmp_path), AsyncMock())
    state = manager.set('work', 'Finish report')
    runtime = GoalRuntime(manager, current_user_epoch=lambda _: 1, pending_plan=lambda _: 'pending-plan')
    delivery = Delivery()
    try:
        await runtime._after_turn(DeliveredGoalTurn('work', 'Error', user_epoch=1,
                                  provider_error=True, succeeded=False, run_id='failed-run'), delivery)
        current = manager.get('work')
        assert current.goal_id == state.goal_id
        assert current.status is GoalStatus.PAUSED
        assert not current.has_wait
        delivery.run_continuation.assert_not_awaited()
        assert delivery.deliver_notice.await_args.args[0].verdict is GoalVerdict.NEEDS_INPUT
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_old_turn_failure_cannot_pause_a_new_goal_generation(tmp_path):
    manager = GoalManager(GoalStore(tmp_path), AsyncMock())
    old = manager.set('work', 'Old report')
    current = manager.set('work', 'New report')
    runtime = GoalRuntime(manager, current_user_epoch=lambda _: 1)
    delivery = Delivery()
    try:
        await runtime._after_turn(DeliveredGoalTurn('work', 'Old error', user_epoch=1, succeeded=False,
                                  provider_error=True, run_id='old-run',
                                  goal_binding={'version': 1, 'state': 'goal', 'goalId': old.goal_id,
                                                'revision': old.revision}), delivery)
        assert manager.get('work') == current
        delivery.deliver_notice.assert_not_awaited()
        manager.judge.evaluate.assert_not_awaited()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_superseded_failure_cannot_pause_a_newer_user_turn(tmp_path):
    manager = GoalManager(GoalStore(tmp_path), AsyncMock())
    original = manager.set('work', 'Report')
    runtime = GoalRuntime(manager, current_user_epoch=lambda _: 2)
    delivery = Delivery()
    try:
        await runtime._after_turn(DeliveredGoalTurn('work', 'Earlier failure', user_epoch=1,
                                  succeeded=False, provider_error=True, run_id='old-run'), delivery)
        assert manager.get('work') == original
        delivery.deliver_notice.assert_not_awaited()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_goal_change_during_pre_evaluation_read_invalidates_the_old_turn(tmp_path):
    manager = GoalManager(GoalStore(tmp_path), AsyncMock())
    manager.set('work', 'Old report')
    replacement = None

    async def processes(_session):
        nonlocal replacement
        replacement = manager.set('work', 'New report')
        return []

    runtime = GoalRuntime(manager, current_user_epoch=lambda _: 1, background_processes=processes)
    delivery = Delivery()
    try:
        await runtime._after_turn(DeliveredGoalTurn('work', 'Earlier result', user_epoch=1, run_id='old-run'), delivery)
        assert manager.get('work') == replacement
        delivery.deliver_notice.assert_not_awaited()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_actual_turn_carries_causal_goal_binding_into_failure_delivery(tmp_path):
    from flowly.bus.events import InboundMessage, OutboundMessage

    loop, commands = execution_loop(tmp_path)
    original = loop.goal_manager.set('web:chat', 'Report')
    commands.accept('web:chat', 'failed-run', {'message': 'Continue'})
    loop._process_message_unlocked = AsyncMock(return_value=OutboundMessage(
        channel='web', chat_id='chat', content='Model unavailable',
        metadata={'error': {'code': 'unavailable'}, 'stream_run_id': 'failed-run', '_goal_eligible': True},
    ))
    try:
        message = InboundMessage(channel='web', chat_id='chat', sender_id='user', content='Continue', metadata={'run_id': 'failed-run'})
        response = await loop._process_message(message)
        turn = loop._goal_turn_from_outbound('web:chat', response, user_epoch=1)
        assert turn.goal_binding['goalId'] == original.goal_id
        assert turn.goal_binding == commands.lookup('web:chat', 'failed-run')['goalBinding']
        runtime = GoalRuntime(loop.goal_manager, current_user_epoch=lambda _: 1)
        try:
            await runtime._after_turn(turn, Delivery())
            assert loop.goal_manager.get('web:chat').status is GoalStatus.PAUSED
            assert commands.lookup('web:chat', 'failed-run')['status'] == 'error'
        finally:
            await runtime.close()
    finally:
        commands.close()


@pytest.mark.asyncio
async def test_goal_replaced_during_announcement_does_not_execute_or_bind_to_its_replacement(tmp_path):
    from flowly.bus.events import InboundMessage

    loop, commands = execution_loop(tmp_path)
    original = loop.goal_manager.set('web:chat', 'Original work')
    loop._process_message_unlocked = AsyncMock()

    async def announce(_text):
        loop.goal_manager.set('web:chat', 'New work')

    message = InboundMessage(channel='web', chat_id='chat', sender_id='goal', content='', metadata={
        'run_id': 'stale-run', '_goal_continuation_goal_id': original.goal_id,
        '_goal_base_user_epoch': 0, 'goal_run': True, 'on_user_message': announce,
    })
    try:
        response = await loop._process_message(message)
        assert response is None
        loop._process_message_unlocked.assert_not_awaited()
        assert commands.lookup('web:chat', 'stale-run')['status'] == 'aborted'
        assert loop.goal_manager.get('web:chat').goal_id != original.goal_id
    finally:
        commands.close()
