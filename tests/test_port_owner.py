"""The gateway port belongs to the computer, not to one user (2026-11-09).

A second macOS user's agent could never start while the first user's held
18790: launchd restarted it forever and Desktop said "No agent yet". An agent
whose port is held by another user moves to a free one and keeps it in
config, which every reader of the port follows.
"""

from __future__ import annotations

import plistlib

import pytest

from flowly.config.loader import load_config, save_config
from flowly.integrations import port_owner

PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:4A66 00000000:0000 0A 00000000:00000000 00:00000000 00000000   501        0 1 1 0000000000000000 100 0 0 10 0
   1: 0100007F:4A66 0100007F:C3A1 01 00000000:00000000 00:00000000 00000000   502        0 2 1 0000000000000000 20 4 30 10 -1
   2: 00000000:4A67 00000000:0000 0A 00000000:00000000 00:00000000 00000000   0          0 3 1 0000000000000000 100 0 0 10 0
"""


@pytest.mark.parametrize("own, answers, expected", [
    ([], True, True),        # it answers, and none of this user's processes listen: another user
    ([4242], True, False),   # this user's own listener
    ([], False, False),      # nobody there
    (None, True, False),     # lsof unreadable: never guess
])
def test_on_a_mac_a_port_answering_with_none_of_our_listeners_is_another_users(monkeypatch, own, answers, expected):
    monkeypatch.setattr(port_owner.sys, "platform", "darwin")
    monkeypatch.setattr(port_owner, "_own_listener_pids", lambda port: own)
    monkeypatch.setattr(port_owner, "_answers", lambda port: answers)
    assert port_owner.held_by_another_user(18790) is expected


def test_linux_listeners_are_read_with_their_uids():
    assert port_owner.linux_listener_uids(PROC_NET_TCP, 0x4A66) == [501]  # not the connected row
    assert port_owner.linux_listener_uids(PROC_NET_TCP, 0x4A67) == [0]


def test_the_gateway_moves_off_another_users_port_and_keeps_the_new_one(monkeypatch):
    from flowly.cli.gateway_cmd import _leave_another_users_port

    config = load_config()
    config.gateway.port = 18790
    save_config(config)
    monkeypatch.setattr(port_owner, "held_by_another_user", lambda port: port == 18790)
    monkeypatch.setattr(port_owner, "free_port", lambda near: 18793)
    assert _leave_another_users_port(18790) == 18793
    assert load_config().gateway.port == 18793
    # Next start: config already says 18793, which nobody else holds.
    assert _leave_another_users_port(load_config().gateway.port) == 18793


def test_its_own_port_stays_put(monkeypatch):
    from flowly.cli.gateway_cmd import _leave_another_users_port

    persisted = []
    monkeypatch.setattr(port_owner, "held_by_another_user", lambda port: False)
    monkeypatch.setattr("flowly.cli.gateway_cmd.persist_gateway_port", persisted.append)
    assert _leave_another_users_port(18790) == 18790 and persisted == []


def test_a_service_without_a_port_follows_config(tmp_path):
    from flowly.cli.service_cmd import _extract_port_from_plist

    config = load_config()
    config.gateway.port = 18795
    save_config(config)
    plist = tmp_path / "ai.flowly.gateway.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": ["/x/flowly-agent", "gateway"]}))
    assert _extract_port_from_plist(plist) == 18795
    plist.write_bytes(plistlib.dumps({"ProgramArguments": ["/x/flowly-agent", "gateway", "--port", "18790"]}))
    assert _extract_port_from_plist(plist) == 18790
    config.gateway.port = 18790
    save_config(config)


def test_a_free_port_is_one_nobody_answers_on():
    import socket

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        near = taken.getsockname()[1]
        found = port_owner.free_port(near - 1, span=3)
        assert found != near
