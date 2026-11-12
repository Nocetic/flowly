"""Every place that shows how to pair an app gives the gateway's key.

`flowly gateway` printed the key, but `flowly enroll`, the TUI's /remote and
`flowly service install` still said "TLS off, plain ws://" and made a code
without the pin: once a gateway refuses unencrypted connections from other
machines (gateway.allowPlainRemote false), an app paired from them could not
connect at all.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from flowly.gateway import remote_info
from flowly.gateway.remote_info import connect_block
from flowly.gateway.tls import encrypts, ensure_identity, pairing_pin, short_fingerprint


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(remote_info, "detect_lan_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(remote_info, "detect_public_ip", lambda: "203.0.113.7")
    return tmp_path / "home"


def _link(lines: list[str]) -> str:
    return next(line.strip() for line in lines if line.strip().startswith("flowly://remote?"))


def test_encryption_follows_the_mode_and_the_bind():
    assert encrypts("auto", True) and not encrypts("auto", False)
    assert encrypts("on", False) and not encrypts("off", True)


def test_the_pairing_pin_is_the_key_the_gateway_serves(tmp_path):
    pin = pairing_pin("auto", "0.0.0.0", tmp_path / "tls")
    assert pin == ensure_identity(["0.0.0.0"], tmp_path / "tls").pin
    assert pairing_pin("auto", "127.0.0.1", tmp_path / "other") == ""
    assert pairing_pin("off", "0.0.0.0", tmp_path / "other") == ""


def test_enable_remote_access_gives_the_key(home):
    info = remote_info.enable_remote_access()
    assert info["host"] == "0.0.0.0"
    assert info["pin"] == ensure_identity(directory=home / "gateway" / "tls").pin


def test_the_block_carries_the_key_in_the_lines_the_link_and_the_code(home):
    info = remote_info.enable_remote_access()
    lines, has_code = connect_block(info)
    text = "\n".join(lines)
    assert short_fingerprint(info["pin"]) in text
    assert "TLS     : [b]off" not in text and "plain ws://" not in text
    query = parse_qs(urlsplit(_link(lines)).query)
    assert query["pin"] == [info["pin"]]
    assert query["token"] == [info["token"]]
    assert query["host"] == ["192.168.1.20"]
    assert has_code


def test_without_encryption_the_block_says_so_and_carries_no_pin(home):
    lines, _ = connect_block({"host": "10.0.0.5", "port": 18790, "token": "t", "pin": ""})
    text = "\n".join(lines)
    assert "Encryption: [b]off" in text
    assert "pin" not in parse_qs(urlsplit(_link(lines)).query)


def test_enroll_prints_the_key(home, capsys):
    from flowly.cli import enroll_cmd

    enroll_cmd.console.width = 400
    enroll_cmd.enroll()
    out = capsys.readouterr().out
    pin = ensure_identity(directory=home / "gateway" / "tls").pin
    assert short_fingerprint(pin) in out
    assert "leave 'Use TLS' off" not in out
    assert parse_qs(urlsplit(next(l.strip() for l in out.splitlines() if "flowly://remote?" in l)).query)["pin"] == [pin]
