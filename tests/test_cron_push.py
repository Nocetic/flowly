"""Cron push notification helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from flowly.cli.gateway_cmd import (
    _schedule_cron_push_notification,
    _should_push_persisted_cron_completion,
    _should_register_cron_with_relay,
)


class _Job:
    id = "job-1"
    name = "Daily report"


@pytest.mark.asyncio
async def test_schedule_cron_push_without_chat_target(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    _schedule_cron_push_notification(_Job(), "first line\nsecond line")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Without a run id the event cannot be recognized again: a unique key.
    assert calls[0]["data"].pop("eventKey").startswith("cron:job-1:")
    assert calls == [{
        "title": "Daily report",
        "body": "first line",
        "conversation_id": "",
        "data": {
            "type": "cron",
            "jobId": "job-1",
            "jobName": "Daily report",
        },
    }]


@pytest.mark.asyncio
async def test_chat_origin_cron_push_keeps_conversation_target(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    _schedule_cron_push_notification(
        _Job(),
        "chat result",
        conversation_id="ios:chat-1",
        run_id="run-42",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == [{
        "title": "Daily report",
        "body": "chat result",
        "conversation_id": "ios:chat-1",
        "data": {
            "type": "cron",
            "jobId": "job-1",
            "jobName": "Daily report",
            "runId": "run-42",
            "eventKey": "cron:job-1:run-42",
        },
    }]


@pytest.mark.parametrize(
    ("lifecycle", "enabled", "expected"),
    [
        ("scheduled", True, True),
        ("scheduled", False, False),
        ("paused", False, False),
        ("completed", False, False),
        ("archived", False, False),
    ],
)
def test_relay_reconciliation_only_registers_executable_schedules(
    lifecycle: str,
    enabled: bool,
    expected: bool,
) -> None:
    job = type("Job", (), {"lifecycle": lifecycle, "enabled": enabled})()
    assert _should_register_cron_with_relay(job) is expected


@pytest.mark.parametrize(
    ("deliver", "channel", "target", "persisted", "silent", "expected"),
    [
        (True, "desktop", "chat-1", True, False, True),
        (True, "ios", "chat-1", True, False, True),
        (True, "web", None, True, False, True),
        (True, None, None, True, False, True),
        (True, "telegram", "chat-1", True, False, False),
        (False, "desktop", "chat-1", True, False, False),
        (True, "desktop", "chat-1", False, False, False),
        (True, "desktop", "chat-1", True, True, False),
    ],
)
def test_persisted_completion_push_preserves_delivery_eligibility(
    deliver: bool,
    channel: str | None,
    target: str | None,
    persisted: bool,
    silent: bool,
    expected: bool,
) -> None:
    job = SimpleNamespace(
        payload=SimpleNamespace(deliver=deliver, channel=channel, to=target),
        origin=None,
    )
    data = {"outputPersisted": persisted, "silent": silent}
    assert _should_push_persisted_cron_completion(job, data) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['desktop', 'ios', 'android'])
async def test_real_cron_persists_exact_result_before_push_even_if_ws_fails(tmp_path, monkeypatch, channel):
    from flowly.cli.gateway_cmd import _publish_cron_lifecycle
    from flowly.cron.service import CronService
    from flowly.cron.types import CronSchedule
    from flowly.push import relay_push

    pushes = []
    async def execute(job):
        return 'Retained completion'
    service = CronService(tmp_path / 'jobs.json', on_job=execute)
    job = service.add_job('Mobile result', CronSchedule(kind='at', at_ms=1), 'hello', deliver=True, channel=channel, to='conversation-1')
    async def notify(title, body, **kwargs):
        run_id = kwargs['data']['runId']
        records = service._read_run_records(job.id, include_content=True)
        assert records[0]['runId'] == run_id
        assert 'Retained completion' in records[0]['content']
        assert service.current_run(job.id) is None
        pushes.append(kwargs)
    async def broken_ws(event, data):
        if event == 'cron.completed':
            raise ConnectionError('client disconnected')
    monkeypatch.setattr(relay_push, 'notify_devices', notify)
    gateway = SimpleNamespace(broadcast_cron_event=broken_ws)
    async def lifecycle(event, data):
        await _publish_cron_lifecycle(service, gateway, event, data)
    service.on_complete = lifecycle
    assert await service.run_job(job.id, force=True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(pushes) == 1
    assert pushes[0]['conversation_id'] == f'{channel}:conversation-1'
    assert pushes[0]['data']['jobId'] == job.id
    assert CronService(service.store_path)._read_run_records(job.id, include_content=True)[0]['runId'] == pushes[0]['data']['runId']
