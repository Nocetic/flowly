import json
import socket
import struct
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.input_broker import ScreenInputBroker

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="private Unix transport")
KEY = struct.pack("!BBHI", 4, 1, 0, 0xFFE1)
POINTER = struct.pack("!BBHH", 5, 1, 50, 60)


def read(peer, size):
    value = bytearray()
    while len(value) < size:
        part = peer.recv(size - len(value))
        if not part:
            raise EOFError()
        value.extend(part)
    return bytes(value)


class Peer:
    def __init__(self, path):
        self.events = []
        self.fences = 0
        self.fault_at = 0
        self.fault = "eof"
        self.hold_at = 0
        self.reached = threading.Event()
        self.proceed = threading.Event()
        self.errors = []
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.listener.settimeout(5)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            peer, _ = self.listener.accept()
            with peer:
                peer.settimeout(5)
                peer.sendall(b"RFB 003.008\n")
                assert read(peer, 12) == b"RFB 003.008\n"
                peer.sendall(b"\x01\x01")
                assert read(peer, 1) == b"\x01"
                peer.sendall(bytes(4))
                assert read(peer, 1) == b"\x01"
                peer.sendall(bytes(24))
                assert read(peer, 8) == struct.pack("!BBHi", 2, 0, 1, -312)
                peer.sendall(struct.pack("!B3xIB", 248, 1 << 31, 0))
                assert read(peer, 9) == struct.pack("!B3xIB", 248, 0, 0)
                while True:
                    kind = read(peer, 1)[0]
                    if kind == 248:
                        header = read(peer, 8)
                        token = read(peer, header[7])
                        assert int.from_bytes(header[3:7], "big") == (1 << 31) | 1
                        self.fences += 1
                        if self.fences == self.hold_at:
                            self.reached.set()
                            assert self.proceed.wait(5)
                        flags = 1
                        if self.fences == self.fault_at:
                            if self.fault == "eof":
                                return
                            if self.fault == "token":
                                token = bytes(len(token))
                            if self.fault == "flags":
                                flags = 0
                            if self.fault == "clipboard":
                                fake = struct.pack("!B3xIB", 248, 1, len(token)) + token
                                peer.sendall(struct.pack("!B3xI", 3, len(fake)) + fake)
                                return
                            if self.fault == "timeout":
                                self.reached.set()
                                assert self.proceed.wait(5)
                                return
                        peer.sendall(struct.pack("!B3xIB", 248, flags, len(token)) + token)
                    elif kind in (4, 5):
                        self.events.append(bytes([kind]) + read(peer, {4: 7, 5: 5}[kind]))
                    elif kind == 6:
                        header = read(peer, 7)
                        self.events.append(bytes([kind]) + header + read(peer, int.from_bytes(header[3:7], "big")))
                    else:
                        raise AssertionError(f"Unexpected protocol type {kind}")
        except (EOFError, OSError):
            pass
        except Exception as error:
            self.errors.append(error)

    def close(self):
        self.proceed.set()
        self.listener.close()
        self.thread.join(6)
        assert not self.thread.is_alive()
        assert not self.errors


@pytest.fixture
def environment(tmp_path):
    authority = ScreenAuthority(tmp_path / "authority")
    authority.start("generation")
    human = authority.acquire("viewer", "generation", 1)
    with tempfile.TemporaryDirectory(prefix="rfb-input-") as directory:
        path = Path(directory) / "rfb.sock"
        peer = Peer(path)
        brokers = []

        def connect(**kwargs):
            broker = ScreenInputBroker(authority, "viewer", "generation", human["epoch"], path, **kwargs)
            brokers.append(broker)
            return broker

        yield authority, peer, connect
        for broker in brokers:
            broker.abandon()
        peer.close()


def test_drained_input_releases_pressed_keys_buttons_and_original_run(environment):
    authority, peer, connect = environment
    broker = connect()
    broker.send(KEY + POINTER)
    assert authority.snapshot()["inputDrainRequired"]
    state = broker.finish(release=True)
    assert state["control"] == "agent" and not state["inputDrainRequired"]
    assert peer.events == [KEY, POINTER, struct.pack("!BBHI", 4, 0, 0, 0xFFE1),
                           struct.pack("!BBHH", 5, 0, 50, 60)]
    admission = authority.begin_action("original-run", "next", "generation")
    assert authority.end_action(admission)


def test_handback_cannot_overtake_unacknowledged_input_or_key_release(environment):
    authority, peer, connect = environment
    broker = connect()
    broker.send(KEY)
    peer.hold_at = 3
    with ThreadPoolExecutor(max_workers=1) as workers:
        finish = workers.submit(broker.finish, release=True)
        assert peer.reached.wait(2)
        assert not finish.done()
        restored = ScreenAuthority(authority.directory)
        state = restored.snapshot()
        for operation in (
            lambda: restored.release("viewer", "generation", state["epoch"]),
            lambda: restored.acquire("other", "generation", state["epoch"], reclaim=True),
        ):
            with pytest.raises(ScreenError, match="INPUT_DRAIN_REQUIRED"):
                operation()
        with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
            restored.begin_action("run", "action", "generation")
        peer.proceed.set()
        assert finish.result(2)["control"] == "agent"


@pytest.mark.parametrize("fault", ["eof", "token", "flags", "clipboard"])
def test_unknown_input_outcome_never_releases_or_retries(environment, fault):
    authority, peer, connect = environment
    broker = connect()
    peer.fault_at, peer.fault = 2, fault
    with pytest.raises(ScreenError, match="INPUT_OUTCOME_UNKNOWN"):
        broker.send(KEY)
    with pytest.raises(ScreenError, match="INPUT_OUTCOME_UNKNOWN"):
        broker.finish(release=True)
    state = ScreenAuthority(authority.directory).snapshot()
    assert state["control"] == "human" and state["inputDrainRequired"]
    assert peer.events == [KEY]


def test_input_timeout_preserves_the_reservation(environment):
    authority, peer, connect = environment
    broker = connect(timeout=0.2)
    peer.fault_at, peer.fault = 2, "timeout"
    with pytest.raises(ScreenError, match="INPUT_OUTCOME_UNKNOWN"):
        broker.send(KEY)
    assert peer.reached.is_set()
    assert authority.snapshot()["inputDrainRequired"]
    peer.proceed.set()


def test_failed_final_drain_does_not_acknowledge_handback(environment):
    authority, peer, connect = environment
    broker = connect()
    broker.send(KEY)
    peer.fault_at = 3
    with pytest.raises(ScreenError, match="INPUT_OUTCOME_UNKNOWN"):
        broker.finish(release=True)
    assert authority.snapshot()["control"] == "human"
    assert authority.snapshot()["inputDrainRequired"]


def test_only_the_exact_input_reservation_can_end_and_handback(environment):
    authority, _, connect = environment
    broker = connect()
    with pytest.raises(ScreenError, match="INPUT_DRAIN_REQUIRED"):
        connect()
    with pytest.raises(ScreenError, match="INPUT_ADMISSION_CHANGED"):
        authority.end_input(replace(broker._admission, nonce="forged"), release=True)
    broker.finish()
    second = authority.begin_input("viewer", "generation", 2)
    with pytest.raises(ScreenError, match="INPUT_ADMISSION_CHANGED"):
        authority.end_input(broker._admission, release=True)
    assert authority.snapshot()["control"] == "human"
    authority.end_input(second)  # no socket/input was opened for this reservation


def test_failed_capability_probe_keeps_human_control_but_does_not_leak_input_lease(environment):
    authority, peer, connect = environment
    peer.fault_at, peer.fault = 1, "flags"
    with pytest.raises(ScreenError, match="INPUT_TRANSPORT_UNAVAILABLE"):
        connect()
    state = authority.snapshot()
    assert state["control"] == "human" and not state["inputDrainRequired"]
    assert peer.events == []


def test_abandon_and_new_authority_do_not_imply_acknowledged_drain(environment):
    authority, peer, connect = environment
    broker = connect()
    broker.send(KEY)
    broker.abandon()
    restored = ScreenAuthority(authority.directory)
    with pytest.raises(ScreenError, match="INPUT_DRAIN_REQUIRED"):
        restored.release("viewer", "generation", 2)
    assert peer.events == [KEY]


@pytest.mark.parametrize("value", [b"", b"\x04", KEY + b"\xff", b"\x05\x80\0\0\0\0\x01",
    b"\x06\0\0\0\xff\xff\xff\xff", struct.pack("!BBHHHH", 3, 0, 0, 0, 10, 10),
    b"".join(struct.pack("!BBHI", 4, 1, 0, key) for key in range(257))])
def test_unvalidated_or_noninput_bytes_never_reach_the_input_socket(environment, value):
    _, peer, connect = environment
    broker = connect()
    with pytest.raises(ScreenError, match="INVALID_RFB_INPUT"):
        broker.send(value)
    broker.finish()
    assert peer.events == []


def test_input_content_is_not_persisted_and_drain_can_retain_human_control(environment):
    authority, _, connect = environment
    broker = connect()
    text = b"test-sensitive-clipboard"
    broker.send(b"\x06\0\0\0" + struct.pack("!I", len(text)) + text)
    for path in authority.directory.iterdir():
        assert text not in path.read_bytes()
    state = broker.finish()
    assert state["control"] == "human" and not state["inputDrainRequired"]


def test_schema_one_upgrades_without_dropping_a_version_two_fence(tmp_path):
    authority = ScreenAuthority(tmp_path)
    authority.start("generation")
    path = tmp_path / "control.json"
    old = json.loads(path.read_text())
    old.pop("input")
    old["schema"] = 1
    path.write_text(json.dumps(old))
    human = authority.acquire("viewer", "generation", 1)
    assert json.loads(path.read_text())["schema"] == 2
    authority.begin_input("viewer", "generation", human["epoch"])
    damaged = json.loads(path.read_text())
    damaged.pop("input")
    path.write_text(json.dumps(damaged))
    with pytest.raises(ScreenError, match="CONTROL_STATE_INVALID"):
        authority.snapshot()
