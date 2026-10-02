"""An approval decision takes effect at once, whatever the surfaces and phones are doing."""
from __future__ import annotations

import asyncio
import time

import pytest

from flowly.exec.approval_manager import ApprovalManager
from flowly.exec.types import ExecRequest, PendingApproval
from flowly.push import approval_push, notifications, presence, relay_push


def pending(identifier: str = 'approval-1') -> PendingApproval:
    return PendingApproval(id=identifier, request=ExecRequest(command='ls ~/Desktop', session_key=None),
                           session_key=None, created_at=time.time(), expires_at=time.time() + 60)


@pytest.mark.asyncio
async def test_a_slow_surface_never_delays_the_decision_and_close_follows_the_request():
    manager = ApprovalManager()
    events: list[str] = []
    told = asyncio.Event()

    async def slow_surface(request):
        events.append('request')
        told.set()
        await asyncio.sleep(30)  # e.g. pushing to every registered phone in turn
        events.append('late')

    async def close(approval_id, reason, session_key):
        events.append(f'close:{reason}')

    manager.add_notify_callback(slow_surface)
    manager.add_close_callback(close)
    started = time.monotonic()
    wait = asyncio.create_task(manager.request_and_wait(pending()))
    await asyncio.wait_for(told.wait(), 1)
    assert manager.resolve('approval-1', 'allow-once')
    assert await asyncio.wait_for(wait, 1) == 'allow-once'
    assert time.monotonic() - started < 1
    assert events == ['request', 'close:allow-once']


@pytest.mark.asyncio
async def test_a_decision_before_delivery_closes_without_a_late_request():
    manager = ApprovalManager()
    events: list[str] = []
    gate = asyncio.Event()

    async def late_surface(request):
        await gate.wait()
        events.append('request')

    async def close(approval_id, reason, session_key):
        events.append(f'close:{reason}')

    manager.add_notify_callback(late_surface)
    manager.add_close_callback(close)
    wait = asyncio.create_task(manager.request_and_wait(pending('approval-2')))
    await asyncio.sleep(0)
    assert manager.resolve('approval-2', 'deny')
    assert await asyncio.wait_for(wait, 1) == 'deny'
    gate.set()
    await asyncio.sleep(0)
    assert events == ['close:deny']


@pytest.mark.asyncio
async def test_the_approval_push_waits_for_screens_and_never_blocks(monkeypatch):
    sent = []

    async def phones(title, body, **kwargs):
        sent.append(kwargs['data']['eventKey'])

    monkeypatch.setattr(relay_push, 'notify_devices', phones)
    monkeypatch.setattr(notifications, 'APPROVAL_PUSH_DELAY_SECONDS', 0.05)
    # In a voice call on the computer: it can be answered there, so it waits.
    presence.report('desktop-a', True, [], 90, in_call=True)
    approval_push.schedule_approval_push(pending('approval-unanswered'))
    approval_push.schedule_approval_push(pending('approval-answered'))
    approval_push.cancel_approval_push('approval-answered')
    assert sent == []
    await asyncio.sleep(0.15)
    assert sent == ['approval:approval-unanswered']


@pytest.mark.asyncio
async def test_phones_are_pushed_in_parallel_and_dead_registrations_dropped(tmp_path, monkeypatch):
    registry = relay_push.PushRegistry(tmp_path / 'push_subs.json')
    for index in range(16):
        registry.register(push_id=f'push-{index:02d}', push_secret=f'secret-{index}', gateway_id='server-1', platform='ios', kind='relay')
    monkeypatch.setattr(relay_push, 'get_push_registry', lambda: registry)
    statuses = {'push-03': 410, 'push-07': 404, 'push-09': 502}

    def send(base, sub, title, body, data):
        time.sleep(0.25)
        return statuses.get(sub['pushId'], 200)

    monkeypatch.setattr(relay_push, '_send_one', send)
    monkeypatch.setattr(relay_push, 'RETRY_DELAYS', (0.01, 0.01))
    started = time.monotonic()
    await relay_push.notify_devices('Approval required', 'ls ~/Desktop')
    # 16 one after another would take 4 s.
    assert time.monotonic() - started < 1.5
    remaining = {sub['pushId'] for sub in registry.list()}
    assert 'push-03' not in remaining and 'push-07' not in remaining
    assert 'push-09' in remaining and len(remaining) == 14
