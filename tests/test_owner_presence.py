"""A phone is not rung for what the owner already sees on a computer.

Flowly Desktop reports that its user is active and which kinds of
notification it shows itself; while that report is fresh, those kinds stay
off the phone. Everything about it fails toward ringing the phone.
"""
from __future__ import annotations

import pytest

from flowly.channels import feature_rpc
from flowly.push import notifications, presence, relay_push


@pytest.fixture
def phones(monkeypatch):
    sent: list[str] = []

    async def push(title, body, **kwargs):
        sent.append(kwargs['data']['eventKey'])

    monkeypatch.setattr(relay_push, 'notify_devices', push)
    return sent


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(presence.time, 'monotonic', lambda: now[0])
    return now


def notice(kind: str, key: str) -> notifications.Notice:
    return notifications.Notice(kind=kind, key=key, title='Flowly', body='Weekly report is ready')


@pytest.mark.asyncio
async def test_what_the_computer_shows_stays_off_the_phone(phones, clock):
    presence.report('desktop-a', True, ['cron', 'board'], 90)
    await notifications.deliver(notice('cron', 'cron:j:1'))
    await notifications.deliver(notice('board', 'board:c:done'))
    await notifications.deliver(notice('flowlet', 'flowlet:f:1'))
    assert phones == ['flowlet:f:1']


@pytest.mark.parametrize('kind', ['approval', 'clarify', 'plan'])
@pytest.mark.asyncio
async def test_what_the_agent_waits_on_is_never_held(phones, clock, kind):
    presence.report('desktop-a', True, [kind, 'cron', 'board', 'flowlet', 'chat'], 90)
    await notifications.deliver(notice(kind, f'{kind}:1'))
    assert phones == [f'{kind}:1']


@pytest.mark.asyncio
async def test_a_report_that_stops_coming_stops_holding_the_phone(phones, clock):
    presence.report('desktop-a', True, ['cron'], 90)
    clock[0] += 89
    await notifications.deliver(notice('cron', 'cron:j:1'))
    clock[0] += 2
    await notifications.deliver(notice('cron', 'cron:j:2'))
    assert phones == ['cron:j:2']


@pytest.mark.asyncio
async def test_leaving_the_computer_rings_the_phone_at_once(phones, clock):
    presence.report('desktop-a', True, ['cron'], 90)
    presence.report('desktop-a', False, ['cron'], 90)
    await notifications.deliver(notice('cron', 'cron:j:1'))
    assert phones == ['cron:j:1']


@pytest.mark.asyncio
async def test_one_computer_leaving_does_not_speak_for_another(phones, clock):
    presence.report('desktop-a', True, ['chat'], 90)
    presence.report('desktop-b', True, ['chat'], 90)
    presence.report('desktop-a', False, [], 90)
    await notifications.deliver(notice('chat', 'chat:s:1'))
    assert phones == []


@pytest.mark.asyncio
async def test_a_held_event_is_not_pushed_later_either(phones, clock):
    presence.report('desktop-a', True, ['cron'], 90)
    await notifications.deliver(notice('cron', 'cron:j:1'))
    presence.report('desktop-a', False, [], 90)
    await notifications.deliver(notice('cron', 'cron:j:1'))
    assert phones == []


def test_reports_are_bounded_and_unknown_kinds_hold_nothing(clock):
    presence.report('desktop-a', True, ['approval', 'everything', 7], 90)
    assert not any(presence.shown_at_computer(kind) for kind in ('approval', 'cron', 'chat'))
    presence.report('desktop-a', True, ['cron'], 10_000)
    clock[0] += presence.MAX_TTL_SECONDS + 1
    assert not presence.shown_at_computer('cron')
    presence.report('desktop-a', True, ['cron'], 'soon')
    clock[0] += presence.MIN_TTL_SECONDS + 1
    assert not presence.shown_at_computer('cron')
    for index in range(presence.MAX_SOURCES + 5):
        presence.report(f'desktop-{index}', True, ['board'], 90)
    assert len(presence._reports) == presence.MAX_SOURCES
    with pytest.raises(ValueError):
        presence.report('  ', True, ['cron'], 90)


@pytest.mark.asyncio
async def test_the_owner_reports_over_the_feature_rpc(clock):
    result, restart = await feature_rpc.dispatch('presence.report', {
        'sourceId': 'desktop-a', 'present': True, 'kinds': ['cron'], 'ttlSeconds': 90,
    })
    assert result == {'ok': True} and restart is False
    assert presence.shown_at_computer('cron')
    # Only a literal true counts as present.
    await feature_rpc.dispatch('presence.report', {'sourceId': 'desktop-a', 'present': 'yes', 'kinds': ['cron']})
    assert not presence.shown_at_computer('cron')
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch('presence.report', {'present': True, 'kinds': ['cron']})


@pytest.mark.asyncio
async def test_shared_voice_access_cannot_silence_the_owner(clock):
    from flowly.live_voice.authority import RequestOwner, request_owner_scope

    with request_owner_scope(RequestOwner(uid='guest')):
        with pytest.raises(feature_rpc.FeatureRpcError):
            await feature_rpc.dispatch('presence.report', {
                'sourceId': 'desktop-x', 'present': True, 'kinds': ['cron'], 'ttlSeconds': 90,
            })
    assert not presence.shown_at_computer('cron')


def test_a_request_waits_only_where_the_owner_can_answer_it(clock):
    from flowly.push import approval_push, notifications

    assert approval_push.waiting_push_delay('web:conv-a') == 0
    presence.report('desktop-a', True, ['chat'], 90, watching=['web:conv-a', '', 7])
    assert approval_push.waiting_push_delay('web:conv-a') == notifications.APPROVAL_PUSH_DELAY_SECONDS
    assert approval_push.waiting_push_delay('web:conv-b') == 0
    assert approval_push.waiting_push_delay('') == 0
    presence.report('desktop-b', True, [], 90, in_call=True)
    assert approval_push.waiting_push_delay('web:conv-b') == notifications.APPROVAL_PUSH_DELAY_SECONDS
    clock[0] += 91
    assert approval_push.waiting_push_delay('web:conv-a') == 0


@pytest.mark.asyncio
async def test_the_owner_reports_screen_and_call_over_the_feature_rpc(clock):
    await feature_rpc.dispatch('presence.report', {
        'sourceId': 'desktop-a', 'present': True, 'kinds': [], 'ttlSeconds': 90,
        'watching': ['web:conv-a'], 'inCall': 'yes',
    })
    assert presence.owner_watching('web:conv-a')
    assert not presence.owner_watching('web:conv-b')
    await feature_rpc.dispatch('presence.report', {
        'sourceId': 'desktop-a', 'present': True, 'kinds': [], 'ttlSeconds': 90, 'inCall': True,
    })
    assert presence.owner_watching('web:conv-b')
    assert not presence.shown_at_computer('chat')
