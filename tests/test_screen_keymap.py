"""Character keysyms outside the seeded keymap are mapped before they are typed."""

import struct

import pytest

from flowly.desktop_runtime import desktop_keymap
from flowly.desktop_runtime.desktop_keymap import (
    EXTRA_SLOTS,
    KEYMAP_KEYSYMS,
    RemoteKeymap,
    needs_mapping,
)
from tests.test_screen_input_broker import environment  # noqa: F401


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    monkeypatch.setattr(RemoteKeymap, "SETTLE_SECONDS", 0)


def test_only_unseeded_character_keysyms_need_mapping():
    assert not any(needs_mapping(value) for value in (0x41, 0x7E, 0xFF0D, 0xFFE1, 0xFE03, 0xFF7E, 0x1008FF11))
    assert not any(needs_mapping(value) for value in KEYMAP_KEYSYMS)  # ş ğ ı İ é € – …
    assert all(needs_mapping(value) for value in (0x1000259, 0x1002713, 0x10065E5, 0xA9, 0x1B1, 0x10000E7))
    assert len(KEYMAP_KEYSYMS) == len(set(KEYMAP_KEYSYMS)) == 84


def test_new_keysyms_upload_once_and_evict_the_least_recently_used():
    uploads = []
    keymap = RemoteKeymap(lambda extras: uploads.append(list(extras)) or True)
    keymap.prepare([0x41, 0x1BA, 0x1000259])  # A and ş are already keys.
    keymap.prepare([0x1000259])
    assert uploads == [[0x1000259]]
    keymap.prepare(0x1002700 + index for index in range(EXTRA_SLOTS - 1))
    keymap.prepare([0x1000259])  # Refreshes ə without an upload.
    keymap.prepare([0x10065E5])  # Full: the least recently used (✀) is evicted, not ə.
    assert len(uploads) == 3 and len(uploads[-1]) == EXTRA_SLOTS
    assert 0x1002700 not in uploads[-1] and {0x1000259, 0x10065E5} <= set(uploads[-1])


def test_a_failed_upload_is_retried_next_time():
    results = iter([False, True])
    uploads = []
    keymap = RemoteKeymap(lambda extras: uploads.append(list(extras)) or next(results))
    keymap.prepare([0x1000259])
    keymap.prepare([0x1000259])
    assert uploads == [[0x1000259], [0x1000259]]


def test_broker_maps_new_characters_before_they_reach_the_server(environment):  # noqa: F811
    _, peer, connect = environment
    order = []

    class Recorder:
        def prepare(self, keysyms):
            order.append(("prepare", list(keysyms), len(peer.events)))

    broker = connect(keymap=Recorder())
    down, up = struct.pack("!BBHI", 4, 1, 0, 0x1000259), struct.pack("!BBHI", 4, 0, 0, 0x1000259)
    broker.send(down + up + struct.pack("!BBHH", 5, 0, 1, 2))
    assert order == [("prepare", [0x1000259], 0)]
    assert peer.events[:2] == [down, up]
    assert desktop_keymap.KEYMAP_SOURCE.count("EXTRA_SLOTS = 8") == 1


def test_broker_maps_at_most_the_slot_count_per_part_and_keeps_order(environment):  # noqa: F811
    _, peer, connect = environment
    prepared = []

    class Recorder:
        def prepare(self, keysyms):
            prepared.append((list(keysyms), len(peer.events)))

    broker = connect(keymap=Recorder())
    characters = [0x1004E00 + index for index in range(EXTRA_SLOTS + 3)]
    keys = [struct.pack("!BBHI", 4, down, 0, keysym) for keysym in characters for down in (1, 0)]
    broker.send(b"".join(keys))
    assert [keysyms for keysyms, _ in prepared] == [characters[:EXTRA_SLOTS], characters[EXTRA_SLOTS:]]
    # The second batch is mapped only after the first part reached the server.
    assert prepared[1][1] >= 2 * EXTRA_SLOTS
    assert peer.events[:len(keys)] == keys
