"""A named agent's link to its host drops; the agent and its turn go on.

On a cloud machine starved of CPU (Fly throttling a shared CPU, 2026-11-17)
the host answered the agent's ping late. The agent closed the socket, and
the host stopped the agent mid-turn: the turn was lost and the app showed it
running for ten minutes. In the same stall the host dropped every event
signed more than 30 s earlier, the agent's request for an approval among
them, so it never reached the owner.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from flowly import profile_host as host_module
from flowly.live_voice.authority import ProfileHopVerifier, VoiceAuthorityError, sign_profile_hop
from flowly.live_voice.event_transport import EVENT_MAX_AGE_SECONDS, ProfileEventVerifier
from flowly.profile_host import RESTART_CLOSE_CODE, ProfileHost, _Runtime
from tests.test_voice_profile_event_authority import INSTANCE, PARENT_KEY, setup, source  # noqa: F401

# ── the link ─────────────────────────────────────────────────────────────────


class _DroppedLink:
    """The socket as the starved host saw it: answering the agent's ping failed."""

    def __init__(self, close_code: int | None = None) -> None:
        self.close_code = close_code

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.close_code is not None:
            raise StopAsyncIteration
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")

    async def close(self) -> None:
        pass


class _Process:
    pid = 4242
    returncode = None


#: Where a bot that is up listens, as its lease says once it is.
LISTENING = {"port": 41234, "authToken": "t" * 48}


def _host(monkeypatch, lease: dict) -> tuple[ProfileHost, dict]:
    host = ProfileHost.__new__(ProfileHost)
    host._closed = False
    host._runtimes = {}
    host._starting = {}
    host._background_tasks = set()
    host._profile_message_requests = {}
    seen: dict = {"terminated": [], "attached": [], "emitted": []}

    async def terminate(process):
        seen["terminated"].append(process)

    async def attach(name, found, *, recheck=None):
        seen["attached"].append(found["instanceId"])
        # As the real attach does: the new connection is the agent's from now on.
        runtime = _Runtime(name, None, SimpleNamespace(close=AsyncMock()), _DroppedLink(), found["instanceId"], owned=False)
        host._runtimes[name] = runtime
        return runtime

    async def emit(name, event, data):
        seen["emitted"].append(data.get("state"))

    monkeypatch.setattr(host, "_terminate_process", terminate)
    monkeypatch.setattr(host, "_attach_runtime", attach)
    monkeypatch.setattr(host, "_emit", emit)
    monkeypatch.setattr(host_module, "describe_profile", lambda name: SimpleNamespace(path=None))
    monkeypatch.setattr(host_module, "read_runtime_lease", lambda path: lease)
    monkeypatch.setattr(host_module, "_RESTART_POLL_SECONDS", 0.01)
    return host, seen


def _owned(link: _DroppedLink) -> _Runtime:
    runtime = _Runtime("marketing-manager", _Process(), SimpleNamespace(close=AsyncMock()), link, INSTANCE)
    runtime.owned = True
    return runtime


async def _settle(host: ProfileHost) -> None:
    for task in list(host._starting.values()):
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_a_dropped_link_reconnects_to_the_same_agent_and_keeps_it_running(monkeypatch):
    host, seen = _host(monkeypatch, {"instanceId": INSTANCE, "pid": _Process.pid, **LISTENING})
    runtime = _owned(_DroppedLink())
    host._runtimes["marketing-manager"] = runtime

    await asyncio.wait_for(host._read_runtime(runtime), 2)
    await _settle(host)

    assert seen["terminated"] == [], "the agent was stopped mid-turn"
    assert seen["attached"] == [INSTANCE]
    again = host._runtimes["marketing-manager"]
    assert again.process is runtime.process and again.owned is True
    assert seen["emitted"][0] == "starting"


@pytest.mark.asyncio
async def test_an_agent_too_busy_to_answer_is_tried_again(monkeypatch):
    host, seen = _host(monkeypatch, {"instanceId": INSTANCE, "pid": _Process.pid, **LISTENING})
    attach = host._attach_runtime
    failures = [host_module.ProfileHostError("PROFILE_ATTACH_FAILED", "busy", retryable=True)]

    async def slow(name, found, *, recheck=None):
        if failures:
            raise failures.pop()
        return await attach(name, found, recheck=recheck)

    monkeypatch.setattr(host, "_attach_runtime", slow)
    runtime = _owned(_DroppedLink())
    host._runtimes["marketing-manager"] = runtime

    await asyncio.wait_for(host._read_runtime(runtime), 2)
    await _settle(host)

    assert seen["terminated"] == []
    assert host._runtimes["marketing-manager"].instance_id == INSTANCE


@pytest.mark.asyncio
async def test_an_agent_that_is_gone_is_not_waited_for(monkeypatch):
    host, seen = _host(monkeypatch, {"instanceId": INSTANCE, "pid": _Process.pid, **LISTENING})
    runtime = _owned(_DroppedLink())
    runtime.process = SimpleNamespace(pid=_Process.pid, returncode=1)
    host._runtimes["marketing-manager"] = runtime

    await asyncio.wait_for(host._read_runtime(runtime), 2)

    assert host._starting == {} and seen["attached"] == []
    assert seen["emitted"] == ["error"]


@pytest.mark.asyncio
async def test_a_restarting_agent_is_taken_back_only_as_its_new_instance(monkeypatch):
    lease = {"instanceId": INSTANCE, "pid": _Process.pid, **LISTENING}
    host, seen = _host(monkeypatch, lease)
    runtime = _owned(_DroppedLink(close_code=RESTART_CLOSE_CODE))
    host._runtimes["marketing-manager"] = runtime

    await asyncio.wait_for(host._read_runtime(runtime), 2)
    await asyncio.sleep(0.05)
    assert seen["attached"] == [], "the old instance is not the restarted one"
    lease["instanceId"] = "runtime-events-2"
    await _settle(host)

    assert seen["attached"] == ["runtime-events-2"]
    assert seen["terminated"] == []


@pytest.mark.asyncio
async def test_a_restarting_agent_is_attached_once_it_listens_not_before(monkeypatch):
    # Its new instance is in the lease a moment before its port and token are.
    lease = {"instanceId": "runtime-events-2", "pid": _Process.pid, "port": 0}
    host, seen = _host(monkeypatch, lease)
    refused = []

    async def strict(name, found, *, recheck=None):
        if not found.get("port"):
            refused.append(found["instanceId"])
            raise host_module.ProfileHostError("PROFILE_OWNERSHIP_CONFLICT", "older runtime")
        seen["attached"].append(found["instanceId"])
        runtime = _Runtime(name, None, SimpleNamespace(close=AsyncMock()), _DroppedLink(), found["instanceId"], owned=False)
        host._runtimes[name] = runtime
        return runtime

    monkeypatch.setattr(host, "_attach_runtime", strict)
    runtime = _owned(_DroppedLink(close_code=RESTART_CLOSE_CODE))
    host._runtimes["marketing-manager"] = runtime

    await asyncio.wait_for(host._read_runtime(runtime), 2)
    await asyncio.sleep(0.05)
    lease.update(LISTENING)
    await _settle(host)

    assert refused == [] and seen["terminated"] == []
    assert seen["attached"] == ["runtime-events-2"]


# ── late events ──────────────────────────────────────────────────────────────


def test_an_event_that_waited_through_a_stall_still_counts(setup):  # noqa: F811
    frame, sessions = source(setup)
    # Two minutes in the socket while the host had no CPU.
    verifier = ProfileEventVerifier(INSTANCE, key=PARENT_KEY, now=lambda: time.time() + 120)

    event = verifier.verify(frame, sessions_dir=sessions.sessions_dir)

    assert event["data"]["state"] == "final"
    with pytest.raises(VoiceAuthorityError):
        verifier.verify(frame, sessions_dir=sessions.sessions_dir)


def test_a_request_hop_keeps_its_short_window():
    proof = sign_profile_hop(PARENT_KEY, INSTANCE, "r-1", "m", {}, None, now=int(time.time()) - 60)
    with pytest.raises(VoiceAuthorityError):
        ProfileHopVerifier(INSTANCE, key=PARENT_KEY).verify("r-1", "m", {}, proof)


def test_ids_are_let_go_in_the_order_they_expire():
    clock = [1_000.0]
    verifier = ProfileHopVerifier(INSTANCE, key=PARENT_KEY, monotonic=lambda: clock[0], max_age=30)
    now = int(time.time())
    for index in range(3):
        verifier.verify(f"r-{index}", "m", {}, sign_profile_hop(PARENT_KEY, INSTANCE, f"r-{index}", "m", {}, None, now=now))
        clock[0] += 10
    clock[0] = 1_000.0 + 45 + 5  # r-0 is past its 45 s, r-1 and r-2 are not

    verifier.verify("r-3", "m", {}, sign_profile_hop(PARENT_KEY, INSTANCE, "r-3", "m", {}, None, now=now))

    assert list(verifier._seen) == ["r-1", "r-2", "r-3"]


def test_a_long_turns_events_fit_in_what_is_remembered():
    # A turn streaming 100 events a second for the whole window is not refused.
    assert EVENT_MAX_AGE_SECONDS * 100 <= 131_072 * 2
