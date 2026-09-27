"""An unused running screen stops by itself; any use keeps it running."""

import time

import aiohttp
import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.gateway import display
from flowly.gateway.display import IDLE_STOP_SECONDS, STREAM_PATH, _resolve, idle_sweep
from tests.test_screen_display_gateway import display_gateway as gateway_fixture
from tests.test_screen_display_gateway import observe

display_gateway = gateway_fixture


@pytest.fixture
def idle(display_gateway):
    session = _resolve(display_gateway.gateway, {})
    runtime = session._runtime
    calls = []

    def stop(generation, *, identity_guard=None, idle_revision=None):
        calls.append((generation, idle_revision))
        identity_guard()
        runtime.authority.stop(generation, idle_revision=idle_revision)

    runtime.stop = stop
    # Only the default profile is a candidate here; the writer screen stays in use.
    writer = _resolve(display_gateway.gateway, {"profileId": "writer"})
    writer.streams.add(in_use := object())
    yield display_gateway, session, calls
    writer.streams.discard(in_use)
    session.streams.clear()


def unused_for(session, seconds):
    control = session.runtime.authority.snapshot()
    session.idle_mark = (control["generation"], control["revision"])
    session.activity = time.monotonic() - seconds


async def test_unused_running_screen_is_stopped_only_after_the_idle_limit(idle):
    _, session, calls = idle
    start = time.monotonic()
    assert await idle_sweep(idle[0].gateway, now=start) == []  # First observation starts the clock.
    assert await idle_sweep(idle[0].gateway, now=start + IDLE_STOP_SECONDS - 1) == []
    assert await idle_sweep(idle[0].gateway, now=start + IDLE_STOP_SECONDS + 1) == ["generation-default"]
    assert calls == [("generation-default", session.runtime.authority.snapshot()["revision"] - 1)]
    assert not session.runtime.authority.snapshot()["running"]


async def test_viewer_stream_human_control_and_agent_run_are_use(idle):
    fixture, session, calls = idle
    authority = session.runtime.authority
    session.streams.add(object())
    unused_for(session, IDLE_STOP_SECONDS * 2)
    assert await idle_sweep(fixture.gateway) == []
    session.streams.clear()
    human = authority.acquire("viewer", "generation-default", authority.snapshot()["epoch"])
    unused_for(session, IDLE_STOP_SECONDS * 2)
    assert await idle_sweep(fixture.gateway) == []
    authority.release("viewer", "generation-default", human["epoch"])
    authority.claim_run("original-run", "generation-default")
    unused_for(session, IDLE_STOP_SECONDS * 2)
    assert await idle_sweep(fixture.gateway) == []
    assert calls == [] and authority.snapshot()["running"]


async def test_any_authority_change_restarts_the_idle_clock(idle):
    fixture, session, calls = idle
    authority = session.runtime.authority
    start = time.monotonic()
    await idle_sweep(fixture.gateway, now=start)
    human = authority.acquire("viewer", "generation-default", authority.snapshot()["epoch"])
    authority.release("viewer", "generation-default", human["epoch"])
    later = start + IDLE_STOP_SECONDS - 10
    assert await idle_sweep(fixture.gateway, now=later) == []  # New revision observed now.
    assert await idle_sweep(fixture.gateway, now=start + IDLE_STOP_SECONDS + 1) == []
    assert await idle_sweep(fixture.gateway, now=later + IDLE_STOP_SECONDS + 1) == ["generation-default"]
    assert len(calls) == 1


async def test_viewer_requests_count_as_use(idle):
    fixture, session, calls = idle
    unused_for(session, IDLE_STOP_SECONDS * 2)
    await observe(fixture)
    assert await idle_sweep(fixture.gateway) == []
    assert calls == []


async def test_use_between_decision_and_stop_is_refused_under_the_authority_lock(idle):
    fixture, session, calls = idle
    authority = session.runtime.authority
    stop = session.runtime.stop

    def raced(generation, **kwargs):
        # A takeover lands after the event-loop decision, before the lock.
        human = authority.acquire("viewer", generation, authority.snapshot()["epoch"])
        authority.release("viewer", generation, human["epoch"])
        stop(generation, **kwargs)

    session.runtime.stop = raced
    unused_for(session, IDLE_STOP_SECONDS * 2)
    assert await idle_sweep(fixture.gateway) == []
    assert authority.snapshot()["running"] and not session.idle_stopping


async def test_no_viewer_attaches_while_an_idle_stop_runs(idle):
    fixture, session, _ = idle
    _, headers = await observe(fixture)
    session.idle_stopping = True
    with pytest.raises(aiohttp.WSServerHandshakeError) as busy:
        await fixture.client.ws_connect(fixture.server.make_url(STREAM_PATH), headers=headers)
    assert busy.value.status == 429


async def test_failed_screen_is_left_for_explicit_recovery(idle, monkeypatch):
    fixture, session, calls = idle
    runtime = session._runtime
    runtime.status = lambda: {"phase": "failed", "generation": "generation-default", "error": "RUNTIME_FAILED"}
    unused_for(session, IDLE_STOP_SECONDS * 2)
    assert await idle_sweep(fixture.gateway) == []
    assert calls == []


async def test_idle_stop_checks_the_profile_identity(idle, monkeypatch):
    fixture, session, calls = idle
    unused_for(session, IDLE_STOP_SECONDS * 2)

    def replaced(*args, **kwargs):
        raise ScreenError("PROFILE_IDENTITY_CHANGED")

    monkeypatch.setattr(display, "_resolve", replaced)
    assert await idle_sweep(fixture.gateway) == []
    assert session.runtime.authority.snapshot()["running"]
