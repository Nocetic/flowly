import socket
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.input_service import InputService, InputServiceClient, _receive, _send
from flowly.desktop_runtime.observers import ObserverRegistry

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="private Unix transport")


@pytest.fixture
def environment(tmp_path):
    authority = ScreenAuthority(tmp_path / "authority")
    authority.start("generation")
    peers = []
    with tempfile.TemporaryDirectory(prefix="screen-observers-") as directory:
        registry = ObserverRegistry(authority, "generation", Path(directory))
        service = InputService(authority, "generation", Path(directory), observers=registry)

        def observe(viewer="other", generation="generation"):
            connection = socket.socket(socket.AF_UNIX)
            connection.settimeout(3)
            peers.append(connection)
            connection.connect(str(registry.path))
            _send(connection, dict(viewer=viewer, generation=generation))
            reply = _receive(connection)
            if reply != {"ok": True}:
                raise ScreenError(reply["code"])
            return connection

        yield authority, registry, InputServiceClient(service.path), observe
        for peer in peers:
            peer.close()
        registry.close()
        service.close()


def test_takeover_ack_waits_for_other_gateway_capture_teardown(environment):
    authority, _, client, observe = environment
    other = observe()
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(client.request, "acquire", generation="generation", viewer="holder", epoch=1)
        assert other.recv(1) == b"R"
        assert not pending.done()
        state = authority.snapshot()
        assert state["control"] == "human-pending"
        assert not authority.may_input("holder", "generation", state["epoch"])
        with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
            authority.begin_action("run", "action", "generation")
        # Production gateway closes its RFB and public WS before this lease.
        other.close()
        acquired = pending.result(3)
    assert acquired["control"] == "human"
    assert authority.may_input("holder", "generation", acquired["epoch"])


def test_unresponsive_observer_keeps_takeover_pending_after_timeout(environment):
    authority, _, client, observe = environment
    other = observe()
    state = client.request("acquire", generation="generation", viewer="holder", epoch=1)
    assert other.recv(1) == b"R"
    assert state["control"] == "human-pending"
    assert not authority.may_input("holder", "generation", state["epoch"])
    other.close()
    state = client.request("acquire", generation="generation", viewer="holder", epoch=state["epoch"])
    assert state["control"] == "human"


def test_holder_can_keep_viewing_but_other_viewers_cannot_join(environment):
    authority, _, client, observe = environment
    holder = observe("holder")
    state = client.request("acquire", generation="generation", viewer="holder", epoch=1)
    assert state["control"] == "human"
    holder.settimeout(0.05)
    with pytest.raises(socket.timeout):
        holder.recv(1)
    assert authority.may_observe("holder", "generation")
    assert not authority.may_observe("other", "generation")
    with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
        observe("other")


def test_finishing_observer_drain_does_not_bypass_an_agent_action(environment):
    authority, _, client, _ = environment
    action = authority.begin_action("original", "in-flight", "generation")
    state = client.request("acquire", generation="generation", viewer="holder", epoch=1)
    assert state["control"] == "human-pending"
    assert not authority.end_action(action)
    acquired = client.request("acquire", generation="generation", viewer="holder", epoch=state["epoch"])
    assert acquired["control"] == "human" and acquired["runId"] == "original"


def test_supervisor_caps_observers_across_gateway_clients_and_scopes_generation(environment):
    _, _, _, observe = environment
    for index in range(4):
        observe(str(index))
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        observe("fifth")
    with pytest.raises(ScreenError, match="STALE_GENERATION"):
        observe("holder", "different-runtime")


def test_registry_shutdown_and_viewer_eof_never_release_human_ownership(environment):
    authority, registry, client, observe = environment
    holder = observe("holder")
    state = client.request("acquire", generation="generation", viewer="holder", epoch=1)
    holder.close()
    registry.close()
    assert authority.snapshot() == state
    with pytest.raises(ScreenError, match="CONTROL_UNAVAILABLE"):
        client.request("acquire", generation="generation", viewer="holder", epoch=state["epoch"])
