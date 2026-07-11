from __future__ import annotations

import time

from flowly.tui.panes.session_picker import _fmt_age, _to_epoch


def test_to_epoch_accepts_iso_seconds_and_milliseconds() -> None:
    now = time.time()

    assert _to_epoch(None) is None
    assert _to_epoch("") is None
    assert _to_epoch("not-a-date") is None
    assert _to_epoch(now) == now
    # feature_rpc's sessions.list sends st_mtime * 1000.
    assert _to_epoch(int(now * 1000)) == int(now * 1000) / 1000
    iso = "2026-07-10T04:00:00"
    parsed = _to_epoch(iso)
    assert parsed is not None and parsed > 0


def test_fmt_age_handles_feature_rpc_millisecond_payload() -> None:
    """Regression: every session rendered '0s ago' because millisecond
    epochs were read as seconds and landed in the future."""
    an_hour_ago_ms = int((time.time() - 3600) * 1000)
    assert _fmt_age(an_hour_ago_ms) == "1h ago"

    two_days_ago_ms = int((time.time() - 2 * 86_400) * 1000)
    assert _fmt_age(two_days_ago_ms) == "2d ago"
