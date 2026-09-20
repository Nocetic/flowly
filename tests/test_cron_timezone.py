"""User and host cron wall times, DST policy, and RPC persistence."""
import zoneinfo
from datetime import datetime

import pytest

from flowly.channels import feature_rpc
from flowly.cron.service import CronService, _compute_grace_ms, _compute_next_run
from flowly.cron.timezone import host_timezone_metadata
from flowly.cron.types import CronSchedule


def ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


@pytest.mark.parametrize('zone,expected', [
    ('Europe/Istanbul', '2026-09-20T22:08:00+00:00'),
    ('UTC', '2026-09-21T01:08:00+00:00'),
])
def test_missing_timezone_uses_execution_host(monkeypatch, zone, expected):
    monkeypatch.setenv('TZ', zone)
    schedule = CronSchedule(kind='cron', expr='8 1 * * *')
    assert _compute_next_run(schedule, ms('2026-09-20T22:00:00+00:00')) == ms(expected)


def test_explicit_timezone_overrides_host(monkeypatch):
    monkeypatch.setenv('TZ', 'Europe/Istanbul')
    madrid = CronSchedule(kind='cron', expr='0 9 * * *', tz='Europe/Madrid')
    new_york = CronSchedule(kind='cron', expr='0 9 * * *', tz='America/New_York')
    now = ms('2026-01-10T07:00:00+00:00')
    assert _compute_next_run(madrid, now) == ms('2026-01-10T08:00:00+00:00')
    assert _compute_next_run(new_york, now) == ms('2026-01-10T14:00:00+00:00')


def test_explicit_timezone_uses_packaged_database_without_os_zoneinfo():
    original_path = zoneinfo.TZPATH
    zoneinfo.reset_tzpath(())
    zoneinfo.ZoneInfo.clear_cache()
    try:
        schedule = CronSchedule(kind='cron', expr='0 9 * * *', tz='Europe/Madrid')
        assert _compute_next_run(
            schedule, ms('2026-01-10T07:00:00+00:00'),
        ) == ms('2026-01-10T08:00:00+00:00')
    finally:
        zoneinfo.reset_tzpath(original_path)
        zoneinfo.ZoneInfo.clear_cache()


@pytest.mark.parametrize('base,expected', [
    ('2026-01-10T00:00:00+00:00', '2026-01-10T14:00:00+00:00'),
    ('2026-07-10T00:00:00+00:00', '2026-07-10T13:00:00+00:00'),
])
def test_host_timezone_uses_dst_rules_not_todays_offset(monkeypatch, base, expected):
    monkeypatch.setenv('TZ', 'America/New_York')
    assert _compute_next_run(CronSchedule(kind='cron', expr='0 9 * * *'), ms(base)) == ms(expected)


def test_absolute_once_and_interval_do_not_shift(monkeypatch):
    now = ms('2026-09-20T22:00:00+00:00')
    for zone in ['UTC', 'Europe/Istanbul', 'America/New_York']:
        monkeypatch.setenv('TZ', zone)
        assert _compute_next_run(CronSchedule(kind='at', at_ms=now+60_000), now) == now+60_000
        assert _compute_next_run(CronSchedule(kind='every', every_ms=60_000), now) == now+60_000


def test_rpc_create_update_list_and_restart_preserve_user_timezone(monkeypatch, tmp_path):
    monkeypatch.setenv('TZ', 'Europe/Istanbul')
    now = ms('2026-01-10T07:00:00+00:00')
    monkeypatch.setattr('flowly.cron.service._now_ms', lambda: now)
    svc = CronService(tmp_path/'jobs.json')
    monkeypatch.setattr(feature_rpc, '_cron', lambda: svc)
    result = feature_rpc.cron_add({'name': 'Mobile 09:00', 'message': 'check',
        'schedule': {'kind': 'cron', 'expr': '0 9 * * *', 'tz': 'Europe/Madrid'}})
    job_id = result['job']['id']
    assert result['job']['schedule']['tz'] == 'Europe/Madrid'
    assert result['job']['state']['nextRunAtMs'] == ms('2026-01-10T08:00:00+00:00')

    updated = feature_rpc.cron_update({'id': job_id, 'updates': {
        'schedule': {'kind': 'cron', 'expr': '0 9 * * *', 'tz': 'America/New_York'},
    }})
    assert updated['job']['schedule']['tz'] == 'America/New_York'
    assert updated['job']['state']['nextRunAtMs'] == ms('2026-01-10T14:00:00+00:00')
    listed = feature_rpc.cron_list({})
    assert listed['jobs'][0]['schedule']['tz'] == 'America/New_York'

    restored = CronService(tmp_path/'jobs.json')
    restored._load_store()
    restored._recompute_next_runs()
    monkeypatch.setattr(feature_rpc, '_cron', lambda: restored)
    restarted_job = feature_rpc.cron_list({})['jobs'][0]
    assert restarted_job['schedule']['tz'] == 'America/New_York'
    assert restarted_job['state']['nextRunAtMs'] == ms('2026-01-10T14:00:00+00:00')
    assert feature_rpc.cron_list({})['schedulerTimeZone'] == {
        'id': 'Europe/Istanbul', 'name': '+03', 'utcOffsetSeconds': 10800,
    }


def test_invalid_explicit_timezone_rejected_without_mutation(tmp_path):
    svc = CronService(tmp_path/'jobs.json')
    good = svc.add_job('valid', CronSchedule(kind='every', every_ms=60_000), 'test')
    bad = CronSchedule(kind='cron', expr='0 9 * * *', tz='Europe/Typo')
    with pytest.raises(ValueError, match='Invalid cron timezone'):
        svc.add_job('bad', bad, 'test')
    with pytest.raises(ValueError, match='Invalid cron timezone'):
        svc.update_job(good.id, {'schedule': bad, 'name': 'changed'})
    assert svc.list_jobs()[0].name == 'valid'
    assert len(svc.list_jobs()) == 1


def test_rpc_rejects_invalid_explicit_timezone_on_create_and_update(monkeypatch, tmp_path):
    svc = CronService(tmp_path/'jobs.json')
    monkeypatch.setattr(feature_rpc, '_cron', lambda: svc)
    created = feature_rpc.cron_add({'name': 'valid', 'message': 'check',
        'schedule': {'kind': 'cron', 'expr': '0 9 * * *', 'tz': 'Europe/Madrid'}})
    with pytest.raises(feature_rpc.FeatureRpcError, match='Invalid cron timezone'):
        feature_rpc.cron_add({'name': 'invalid', 'message': 'check',
            'schedule': {'kind': 'cron', 'expr': '0 9 * * *', 'tz': 'Europe/Typo'}})
    with pytest.raises(feature_rpc.FeatureRpcError, match='Invalid cron timezone'):
        feature_rpc.cron_update({'id': created['job']['id'], 'updates': {
            'name': 'mutated',
            'schedule': {'kind': 'cron', 'expr': '0 9 * * *', 'tz': '../../etc/passwd'},
        }})
    assert feature_rpc.cron_list({})['jobs'][0]['name'] == 'valid'


@pytest.mark.parametrize('zone,base,expected', [
    # A normal wall time remains 09:00 on the first day after spring-forward.
    ('Europe/Madrid', '2026-03-28T12:00:00+00:00', '2026-03-29T07:00:00+00:00'),
    ('America/New_York', '2026-03-07T15:00:00+00:00', '2026-03-08T13:00:00+00:00'),
])
def test_daily_wall_time_does_not_drift_after_spring_forward(zone, base, expected):
    schedule = CronSchedule(kind='cron', expr='0 9 * * *', tz=zone)
    assert _compute_next_run(schedule, ms(base)) == ms(expected)


@pytest.mark.parametrize('zone,base,expected', [
    # 02:30 Madrid and 02:30 New York do not exist on these dates.
    ('Europe/Madrid', '2026-03-28T12:00:00+00:00', '2026-03-30T00:30:00+00:00'),
    ('America/New_York', '2026-03-07T12:00:00+00:00', '2026-03-09T06:30:00+00:00'),
])
def test_nonexistent_spring_forward_wall_time_is_skipped(zone, base, expected):
    schedule = CronSchedule(kind='cron', expr='30 2 * * *', tz=zone)
    assert _compute_next_run(schedule, ms(base)) == ms(expected)


@pytest.mark.parametrize('zone,before_fold,first_fold,after_first,following_day', [
    ('Europe/Madrid', '2026-10-24T12:00:00+00:00', '2026-10-25T00:30:00+00:00',
     '2026-10-25T00:45:00+00:00', '2026-10-26T01:30:00+00:00'),
    ('America/New_York', '2026-10-31T12:00:00+00:00', '2026-11-01T05:30:00+00:00',
     '2026-11-01T05:45:00+00:00', '2026-11-02T06:30:00+00:00'),
])
def test_ambiguous_fall_back_wall_time_runs_once_at_earlier_fold(
    zone, before_fold, first_fold, after_first, following_day,
):
    # Madrid repeats 02:30; New York repeats 01:30.
    expr = '30 2 * * *' if zone == 'Europe/Madrid' else '30 1 * * *'
    schedule = CronSchedule(kind='cron', expr=expr, tz=zone)
    assert _compute_next_run(schedule, ms(before_fold)) == ms(first_fold)
    assert _compute_next_run(schedule, ms(after_first)) == ms(following_day)


def test_metadata_describes_utc_vps(monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    assert host_timezone_metadata() == {'id': 'UTC', 'name': 'UTC', 'utcOffsetSeconds': 0}


def test_cron_grace_keeps_existing_daily_bound(monkeypatch):
    monkeypatch.setenv('TZ', 'Europe/Istanbul')
    assert _compute_grace_ms(CronSchedule(kind='cron', expr='8 1 * * *')) == 7_200_000


def test_existing_implicit_timezone_job_recomputes_on_restart(monkeypatch, tmp_path):
    now = ms('2026-09-20T22:00:00+00:00')
    monkeypatch.setattr('flowly.cron.service._now_ms', lambda: now)
    monkeypatch.setenv('TZ', 'UTC')
    svc = CronService(tmp_path/'jobs.json')
    implicit = svc.add_job('host time', CronSchedule(kind='cron', expr='8 1 * * *'), 'test')
    explicit = svc.add_job('UTC time', CronSchedule(kind='cron', expr='8 1 * * *', tz='UTC'), 'test')
    assert implicit.state.next_run_at_ms == ms('2026-09-21T01:08:00+00:00')
    monkeypatch.setenv('TZ', 'Europe/Istanbul')
    restored = CronService(tmp_path/'jobs.json')
    restored._load_store()
    restored._recompute_next_runs()
    by_id = {job.id: job for job in restored.list_jobs()}
    assert by_id[implicit.id].state.next_run_at_ms == ms('2026-09-20T22:08:00+00:00')
    assert by_id[explicit.id].state.next_run_at_ms == ms('2026-09-21T01:08:00+00:00')
    assert by_id[implicit.id].schedule.expr == '8 1 * * *'


def test_restart_pauses_legacy_job_with_invalid_explicit_timezone(tmp_path):
    svc = CronService(tmp_path/'jobs.json')
    job = svc.add_job(
        'legacy typo', CronSchedule(kind='cron', expr='0 9 * * *', tz='UTC'), 'test',
    )
    job.schedule.tz = 'Europe/Typo'
    svc._save_store()

    restored = CronService(tmp_path/'jobs.json')
    restored._load_store()
    restored._recompute_next_runs()
    restored_job = restored.list_jobs(include_disabled=True)[0]
    assert restored_job.schedule.tz == 'Europe/Typo'
    assert restored_job.lifecycle == 'paused'
    assert restored_job.enabled is False
    assert restored_job.state.next_run_at_ms is None

    repaired = restored.update_job(job.id, {
        'schedule': CronSchedule(kind='cron', expr='0 9 * * *', tz='Europe/Madrid'),
    })
    assert repaired is not None
    assert repaired.lifecycle == 'scheduled'
    assert repaired.enabled is True
    assert repaired.state.next_run_at_ms is not None


def test_update_recalculates_using_host_wall_time(monkeypatch, tmp_path):
    monkeypatch.setenv('TZ', 'Europe/Istanbul')
    monkeypatch.setattr('flowly.cron.service._now_ms', lambda: ms('2026-09-20T22:00:00+00:00'))
    svc = CronService(tmp_path/'jobs.json')
    job = svc.add_job('update', CronSchedule(kind='every', every_ms=60_000), 'test')
    updated = svc.update_job(job.id, {'schedule': CronSchedule(kind='cron', expr='8 1 * * *')})
    assert updated.state.next_run_at_ms == ms('2026-09-20T22:08:00+00:00')
