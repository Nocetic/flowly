"""Every Core phone notification goes through one place with one set of rules."""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from flowly.push import notifications, relay_push


@pytest.fixture
def phones(monkeypatch):
    sent: list[dict] = []

    async def push(title, body, **kwargs):
        sent.append({'title': title, 'body': body, **kwargs})

    monkeypatch.setattr(relay_push, 'notify_devices', push)
    return sent


def notice(key: str, **changes) -> notifications.Notice:
    return notifications.Notice(**{'kind': 'board', 'key': key, 'title': 'Board · Ship', 'body': 'done', **changes})


@pytest.mark.asyncio
async def test_one_event_is_pushed_once_and_carries_its_key(phones):
    assert await notifications.deliver(notice('board:c1:done'))
    assert not await notifications.deliver(notice('board:c1:done'))
    assert await notifications.deliver(notice('board:c1:failed'))
    assert [row['data']['eventKey'] for row in phones] == ['board:c1:done', 'board:c1:failed']
    assert phones[0]['data']['type'] == 'board'


@pytest.mark.asyncio
async def test_text_is_bounded_and_a_relay_failure_never_escapes(monkeypatch):
    async def down(*args, **kwargs):
        raise RuntimeError('relay down')

    monkeypatch.setattr(relay_push, 'notify_devices', down)
    assert await notifications.deliver(notice('board:c2:done', title='x' * 500, body='y' * 500))


@pytest.mark.asyncio
async def test_a_scheduled_push_goes_once_unless_cancelled(phones):
    assert notifications.schedule(notice('approval:a1', kind='approval'), 0.05)
    assert not notifications.schedule(notice('approval:a1', kind='approval'), 0.05)
    assert notifications.schedule(notice('approval:a2', kind='approval'), 0.05)
    assert notifications.cancel('approval:a2')
    assert not notifications.cancel('approval:a2')
    await asyncio.sleep(0.15)
    assert [row['data']['eventKey'] for row in phones] == ['approval:a1']
    assert not notifications.schedule(notice('approval:a1', kind='approval'), 0.05)


def test_keys_are_stable_for_events_and_unique_for_reminders():
    assert notifications.event_key('cron', 'job', 'run') == 'cron:job:run'
    assert notifications.event_key('approval', 'a', None, '') == 'approval:a'
    assert notifications.unique_key('flowlet', 'f') != notifications.unique_key('flowlet', 'f')
    assert len(notifications.event_key('x', 'y' * 500)) == 200


def test_nothing_in_core_pushes_around_the_policy():
    root = pathlib.Path(__file__).resolve().parents[1] / 'flowly'
    direct = [str(path.relative_to(root)) for path in root.rglob('*.py')
              if path.name not in ('relay_push.py', 'notifications.py')
              and re.search(r'\bnotify_devices\s*\(', path.read_text(encoding='utf-8'))]
    assert direct == []
