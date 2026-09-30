"""Bounded Voice log handoff; intentionally does not exercise backend runtime here."""
import queue

from flowly.live_voice import telemetry_sink


def test_full_sink_drops_without_blocking_or_recursing(monkeypatch):
    pending = queue.Queue(maxsize=1)
    pending.put_nowait('already pending')
    monkeypatch.setattr(telemetry_sink, '_QUEUE', pending)
    monkeypatch.setattr(telemetry_sink, '_STARTED', True)
    before = telemetry_sink.health()['dropped']
    telemetry_sink.emit_voice_diagnostic({'event': 'live_voice_stage', 'component': 'core'})
    assert pending.qsize() == 1
    assert telemetry_sink.health()['dropped'] == before + 1


def test_oversize_payload_never_enters_sink(monkeypatch):
    pending = queue.Queue(maxsize=1)
    monkeypatch.setattr(telemetry_sink, '_QUEUE', pending)
    monkeypatch.setattr(telemetry_sink, '_STARTED', True)
    before = telemetry_sink.health()['oversize']
    telemetry_sink.emit_voice_diagnostic({'event': 'live_voice_stage', 'private': 'x' * 5000})
    assert pending.empty()
    assert telemetry_sink.health()['oversize'] == before + 1
