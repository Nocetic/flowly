"""Windows keeps credentials in DPAPI files, not Credential Manager.

Credential Manager holds 2560 bytes per secret and ``keyring`` writes UTF-16,
so 1280 characters. A Flowly account is longer: a Windows sign-in on
2026-11-16 failed with error 1783 from ``CredWrite``, latched the keychain
off for every store (``.keychain-broken``) and saved the tokens to a plain
``account.json``. These run on any OS against a Windows made of fakes: a
DPAPI that seals with its entropy, and a Credential Manager with the real
size limit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from flowly.account import token_store

try:  # absent before the fix, where these tests must fail on their assertions
    from flowly.account import dpapi_store
except ImportError:  # pragma: no cover
    dpapi_store = None  # type: ignore[assignment]


LONG_ACCOUNT = {"id_token": "i" * 1500, "refresh_token": "r" * 900, "uid": "u-1"}


class CredentialManager:
    """Windows Credential Manager as ``keyring`` reaches it, with its 2560-byte limit."""

    LIMIT_CHARS = 2560 // 2  # UTF-16

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get_keyring(self):
        return self

    def get_password(self, service, account):
        return self.entries.get((service, account))

    def set_password(self, service, account, password):
        if len(password) > self.LIMIT_CHARS:
            raise Exception("Can't store password on keychain: (1783, 'CredWrite', 'The stub received bad data.')")
        self.entries[(service, account)] = password

    def delete_password(self, service, account):
        if self.entries.pop((service, account), None) is None:
            raise Exception("Can't delete password in keychain: (1168, 'CredDelete', 'Element not found.')")


def fake_dpapi() -> tuple:
    """Seals bytes to their entropy, as DPAPI does; the wrong entropy fails with an OSError."""

    def protect(data: bytes, entropy: bytes) -> bytes:
        return b"DPAPI" + len(entropy).to_bytes(4, "big") + entropy + bytes(b ^ 0x5A for b in data)

    def unprotect(sealed: bytes, entropy: bytes) -> bytes:
        size = int.from_bytes(sealed[5:9], "big")
        if not sealed.startswith(b"DPAPI") or sealed[9:9 + size] != entropy:
            raise OSError(13, "The data is invalid")
        return bytes(b ^ 0x5A for b in sealed[9 + size:])

    return protect, unprotect


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setattr(token_store, "_KEYRING_DISABLED", None)
    monkeypatch.setattr(token_store, "_MACOS_KEYCHAIN_MISSING", False)
    monkeypatch.setattr(token_store, "_WINDOWS_STORE", None, raising=False)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> CredentialManager:
    manager = CredentialManager()
    monkeypatch.setitem(sys.modules, "keyring", manager)
    monkeypatch.setattr(token_store, "_on_windows", lambda: True, raising=False)
    if dpapi_store is not None:
        monkeypatch.setattr(dpapi_store, "_crypt_functions", fake_dpapi)
    return manager


def sealed_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "AppData" / "Local" / "flowly" / "credentials").glob("*.bin"))


def test_an_account_past_credential_managers_limit_is_kept_sealed(windows, tmp_path: Path):
    status = token_store.save_credentials(LONG_ACCOUNT)

    assert status.backend == "keyring" and status.secure is True
    assert token_store.load_credentials() == LONG_ACCOUNT
    assert not token_store._fallback_path().exists(), "no tokens in a plain file"
    assert not token_store._keyring_marker_path().exists(), "the keychain stays on"
    [sealed] = sealed_files(tmp_path)
    assert b"iiii" not in sealed.read_bytes()
    assert windows.entries == {}


def test_a_secret_already_in_credential_manager_moves_once(windows, tmp_path: Path):
    # The vault's key (44 characters) and short sign-ins did fit.
    windows.entries[("flowly-secrets", "vault-key")] = "k" * 44
    store = token_store.available_keyring()

    assert store.get_password("flowly-secrets", "vault-key") == "k" * 44
    assert windows.entries == {}, "taken out of Credential Manager"
    assert len(sealed_files(tmp_path)) == 1
    assert store.get_password("flowly-secrets", "vault-key") == "k" * 44


def test_a_sign_in_the_old_limit_pushed_into_a_file_comes_back_sealed(windows, tmp_path: Path):
    # What every Windows sign-in left behind: the marker, and the tokens in clear.
    token_store._fallback_path().parent.mkdir(parents=True)
    token_store._fallback_path().write_text(json.dumps(LONG_ACCOUNT), encoding="utf-8")
    token_store._keyring_marker_path().write_text("# (1783, 'CredWrite', ...)\n", encoding="utf-8")

    assert token_store.load_credentials() == LONG_ACCOUNT

    assert not token_store._fallback_path().exists()
    assert not token_store._keyring_marker_path().exists()
    assert token_store.storage_status().secure is True
    assert len(sealed_files(tmp_path)) == 1
    assert token_store.load_credentials() == LONG_ACCOUNT


def test_signing_out_leaves_no_tokens_anywhere(windows, tmp_path: Path):
    token_store.save_credentials(LONG_ACCOUNT)
    # A plain file from before the fix, at a home that is not the default one.
    token_store._fallback_path().parent.mkdir(parents=True, exist_ok=True)
    token_store._fallback_path().write_text(json.dumps(LONG_ACCOUNT), encoding="utf-8")

    token_store.clear_credentials()

    assert sealed_files(tmp_path) == []
    assert not token_store._fallback_path().exists()
    assert token_store.load_credentials() is None


def test_a_sealed_file_opens_only_under_its_own_name(windows, tmp_path: Path):
    store = token_store.available_keyring()
    store.set_password("flowly-tui", "account", "secret-a")
    [sealed] = sealed_files(tmp_path)
    # Copied onto another credential's name (or another user's file): read as missing, kept.
    other = store._path("flowly-tui:other", "account")
    other.write_bytes(sealed.read_bytes())

    assert store.get_password("flowly-tui:other", "account") is None
    assert other.exists()
    store.set_password("flowly-tui:other", "account", "secret-b")
    assert store.get_password("flowly-tui:other", "account") == "secret-b"


def test_without_dpapi_windows_keeps_its_old_way(windows, monkeypatch: pytest.MonkeyPatch):
    def broken():
        raise OSError("crypt32 is not there")

    monkeypatch.setattr(dpapi_store, "_crypt_functions", broken)

    assert token_store.available_keyring() is windows
    token_store.save_credentials({"id_token": "short"})
    assert windows.entries[(token_store._service_name(), "account")] == '{"id_token":"short"}'


def test_other_systems_never_open_the_windows_store(monkeypatch: pytest.MonkeyPatch):
    manager = CredentialManager()
    monkeypatch.setitem(sys.modules, "keyring", manager)
    monkeypatch.setattr(token_store, "_on_windows", lambda: False)

    assert token_store.available_keyring() is manager
    assert token_store._WINDOWS_STORE is None


def test_the_runtime_builds_self_test(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    # scripts/build_runtime.py runs this against the real DPAPI on the Windows runner.
    monkeypatch.setattr(dpapi_store, "_crypt_functions", fake_dpapi)

    dpapi_store.self_test(tmp_path / "credentials")

    assert capsys.readouterr().out.strip() == "dpapi ok"
    assert list((tmp_path / "credentials").iterdir()) == []
