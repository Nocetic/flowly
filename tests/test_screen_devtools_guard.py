"""A root host's profile browser DevTools: only root may connect, and only to the profile's own listener."""

import types

import pytest

from flowly.desktop_runtime import devtools_guard
from flowly.desktop_runtime.authority import ScreenError


def test_guard_installs_one_replaceable_table_per_layout(monkeypatch):
    scripts = []
    monkeypatch.setattr(devtools_guard, "_nft", lambda script: scripts.append(script) or True)
    devtools_guard.guard("ab12" * 8, 40123)
    script = scripts[0]
    assert script.index("delete table inet flowly_screen_ab12ab12ab12ab12") < script.index("chain output")
    assert 'oif "lo" tcp dport 40123 meta skuid != 0 reject with tcp reset' in script
    devtools_guard.unguard("ab12" * 8)
    assert scripts[1].endswith("delete table inet flowly_screen_ab12ab12ab12ab12\n")


@pytest.mark.parametrize("layout, port", [("../x", 1), ("a" * 33, 1), ("ab", 0), ("ab", 65536), ("ab", "1")])
def test_guard_refuses_invalid_input(monkeypatch, layout, port):
    monkeypatch.setattr(devtools_guard, "_nft", lambda script: pytest.fail("must not run nft"))
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        devtools_guard.guard(layout, port)


def test_screen_does_not_start_without_the_rule(monkeypatch):
    monkeypatch.setattr(devtools_guard, "NFT", "/nonexistent/nft")
    with pytest.raises(ScreenError, match="SCREEN_ISOLATION_UNAVAILABLE"):
        devtools_guard.guard("ab12", 40123)


def test_listener_uid_reads_only_the_ipv4_loopback_listener(tmp_path, monkeypatch):
    table = tmp_path / "tcp"
    table.write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:9CBB 0100007F:1F90 01 00000000:00000000 00:00000000 00000000   0 0 1 1\n"
        "   1: 00000000:9CBB 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001 0 2 1\n"
        "   2: 0100007F:9CBB 00000000:0000 0A 00000000:00000000 00:00000000 00000000   998 0 3 1\n")
    monkeypatch.setattr(devtools_guard, "_PROC_TCP", table)
    assert devtools_guard.listener_uid(0x9CBB) == 998
    assert devtools_guard.listener_uid(0x9CBC) is None


def test_agent_never_attaches_to_a_port_another_account_listens_on(monkeypatch):
    from flowly.desktop_runtime import application_layout, browser_provider
    from flowly.desktop_runtime.browser_provider import HostBrowserProvider

    monkeypatch.setattr(browser_provider.os, "geteuid", lambda: 0)
    monkeypatch.setattr(application_layout, "root_application_layout",
                        lambda directory: types.SimpleNamespace(account=types.SimpleNamespace(uid=998)))
    provider = HostBrowserProvider.__new__(HostBrowserProvider)
    provider.runtime = types.SimpleNamespace(directory="/profile/desktop")
    monkeypatch.setattr(devtools_guard, "listener_uid", lambda port: 1001)
    assert not provider._profile_browser_listens(40123)
    monkeypatch.setattr(devtools_guard, "listener_uid", lambda port: 998)
    assert provider._profile_browser_listens(40123)
