"""Phase-two Core diagnostic contract; no speech/credential data is emitted."""
import json
from flowly.live_voice import diagnostics


def test_auth_and_handler_timings_use_explicit_boundaries(monkeypatch):
    rows = []
    monkeypatch.setattr(diagnostics.logger, 'info', lambda _message, payload: rows.append(json.loads(payload)))
    params = {'voiceAccess': 'secret', 'text': 'private speech'}
    diagnostics.voice_rpc_diagnostic('voice.open', params, 10.0, 'ok', stage='core_auth', finished=10.125)
    diagnostics.voice_rpc_diagnostic('voice.open', params, 10.125, 'failed', 'UNAVAILABLE',
                                     stage='core_handler', finished=10.500)
    assert [(r['stage'], r['durationMs'], r['outcome']) for r in rows] == [
        ('core_auth', 125, 'ok'), ('core_handler', 375, 'failed')]
    assert all(r['correlation'] == 'unbound' for r in rows)
    assert 'private speech' not in json.dumps(rows)
    assert 'secret' not in json.dumps(rows)


def test_unknown_stages_and_background_reads_are_not_logged(monkeypatch):
    rows = []
    monkeypatch.setattr(diagnostics.logger, 'info', lambda *args: rows.append(args))
    diagnostics.voice_rpc_diagnostic('voice.open', {}, 0, 'ok', stage='private_stage')
    diagnostics.voice_rpc_diagnostic('voice.history', {}, 0, 'ok')
    assert rows == []


def test_logger_failure_cannot_change_rpc_outcome(monkeypatch):
    def failed(*_args):
        raise RuntimeError('sink unavailable')
    monkeypatch.setattr(diagnostics.logger, 'info', failed)
    diagnostics.voice_rpc_diagnostic('voice.open', {}, 0, 'failed', 'UNAVAILABLE', stage='core_auth')
