import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.bus.events import InboundMessage
from flowly.goals.models import GoalStatus
from flowly.goals.runtime import GoalRuntime
from tests.test_goal_failures import Delivery
from tests.test_voice_goal_provenance import execution_loop


@pytest.mark.asyncio
async def test_bus_execution_exception_produces_one_scoped_failure_that_pauses_its_goal(tmp_path):
    loop, commands = execution_loop(tmp_path)
    state = loop.goal_manager.set('web:chat', 'Report')
    commands.accept('web:chat', 'failed-run', {'message': 'Continue'})
    loop._notify_agent_state = AsyncMock()
    loop._process_message_unlocked = AsyncMock(side_effect=RuntimeError('private runtime detail'))
    loop.bus = SimpleNamespace(publish_outbound=AsyncMock())
    loop.goal_runtime = Mock()
    message = InboundMessage(channel='web', chat_id='chat', sender_id='user', content='Continue', metadata={'run_id': 'failed-run'})
    try:
        await loop._process_turn(message)
        assert loop.bus.publish_outbound.await_count == 1
        response = loop.bus.publish_outbound.await_args.args[0]
        assert response.metadata['session_key'] == 'web:chat'
        assert response.metadata['stream_run_id'] == 'failed-run'
        assert response.metadata['error']['code'] == 'AGENT_INTERNAL_ERROR'
        assert 'private runtime detail' not in response.content
        turn = loop.goal_runtime.delivered.call_args.args[0]
        assert turn.goal_binding['goalId'] == state.goal_id
        assert turn.provider_error
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
async def test_direct_execution_exception_returns_native_failure_metadata(tmp_path):
    loop, commands = execution_loop(tmp_path)
    original = loop.goal_manager.set('web:chat', 'Report')
    commands.accept('web:chat', 'failed-run', {'message': 'Continue'})
    loop._process_message_unlocked = AsyncMock(side_effect=RuntimeError('private runtime detail'))
    try:
        text, metadata = await loop.process_direct('Continue', session_key='web:chat', run_id='failed-run',
                                                   return_metadata=True, defer_goal_delivery=True)
        assert metadata['error']['code'] == 'AGENT_INTERNAL_ERROR'
        assert metadata['stream_run_id'] == 'failed-run'
        assert metadata['_goal_execution_binding']['goalId'] == original.goal_id
        assert 'private runtime detail' not in text
        assert commands.lookup('web:chat', 'failed-run')['status'] == 'error'
    finally:
        commands.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('superseded', [False, True])
async def test_failure_to_submit_an_autonomous_turn_pauses_only_the_current_goal(tmp_path, superseded):
    loop, commands = execution_loop(tmp_path)
    original = loop.goal_manager.set('web:chat', 'Report')
    epoch = 1
    delivery = Delivery()

    async def submit(**_kwargs):
        nonlocal epoch
        if superseded:
            epoch = 2
        raise RuntimeError('test queue unavailable')

    delivery.run_continuation = submit
    runtime = GoalRuntime(loop.goal_manager, current_user_epoch=lambda _: epoch)
    try:
        await runtime._run_next('web:chat', original.goal_id, 1, delivery, kickoff=False)
        current = loop.goal_manager.get('web:chat')
        assert current.goal_id == original.goal_id
        assert current.status is (GoalStatus.ACTIVE if superseded else GoalStatus.PAUSED)
        assert current.turns_used == 0
        assert delivery.deliver_notice.await_count == (0 if superseded else 1)
        loop.goal_manager.judge.evaluate.assert_not_awaited()
    finally:
        await runtime.close()
        commands.close()


@pytest.mark.asyncio
async def test_cancelled_direct_turn_preserves_cancellation_semantics(tmp_path):
    loop, commands = execution_loop(tmp_path)
    commands.accept('web:chat', 'cancelled-run', {'message': 'Continue'})
    loop._process_message_unlocked = AsyncMock(side_effect=asyncio.CancelledError())
    try:
        with pytest.raises(asyncio.CancelledError):
            await loop.process_direct('Continue', session_key='web:chat', run_id='cancelled-run',
                                       return_metadata=True, defer_goal_delivery=True)
        assert commands.lookup('web:chat', 'cancelled-run')['status'] == 'aborted'
    finally:
        commands.close()
