"""OS-keychain fallback: pre-flight, plain-language latch, doctor, retry.

Background — a first-run user on a Mac with no default keychain got a
blocking "Keychain Not Found" panel mid-sign-in; cancelling it returned
``errAuthorizationCanceled`` (-60006), which surfaced as
``PasswordSetError: ... (-60006, 'Unknown Error')``. Sign-in still worked
(the token fell back to an owner-only file) but nothing about that was
legible, and the three ``flowly.auth`` stores never wrote the latch, so the
panel came back for each of them in turn.
"""

from __future__ import annotations

import plistlib
import stat
import sys
import time
from pathlib import Path

import pytest

from flowly.account import token_store
from flowly.auth import xai_oauth
from flowly.diagnostics.checks import check_credential_storage
from flowly.diagnostics.models import DoctorContext, Status


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh FLOWLY_HOME + cleared latches for every test.

    Both latches are module globals that survive between tests in a session,
    so leaving them set would make these tests order-dependent.
    """
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(token_store, "_KEYRING_DISABLED", None)
    monkeypatch.setattr(token_store, "_MACOS_KEYCHAIN_MISSING", None)


def _fake_mac_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend to be macOS with ``mac_home`` as the user's home directory."""
    mac_home = tmp_path / "mac-home"
    (mac_home / "Library" / "Keychains").mkdir(parents=True)
    (mac_home / "Library" / "Preferences").mkdir(parents=True)
    monkeypatch.setattr(token_store.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: mac_home))
    return mac_home


# ── macOS pre-flight ───────────────────────────────────────────────


def test_preflight_is_inert_off_macos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(token_store.sys, "platform", "linux")
    assert token_store._macos_keychain_missing() is False


def test_preflight_passes_when_login_keychain_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mac_home = _fake_mac_home(tmp_path, monkeypatch)
    (mac_home / "Library" / "Keychains" / "login.keychain-db").write_bytes(b"kc")

    assert token_store._macos_keychain_missing() is False


def test_preflight_passes_on_the_pre_db_keychain_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mac_home = _fake_mac_home(tmp_path, monkeypatch)
    (mac_home / "Library" / "Keychains" / "login.keychain").write_bytes(b"kc")

    assert token_store._macos_keychain_missing() is False


def test_preflight_detects_a_mac_with_no_keychain_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A healthy Mac has NO com.apple.security.plist — it is only written once
    # the default keychain is explicitly changed. So "no plist + no login
    # keychain file" is the one shape that proves there's nothing to write to.
    _fake_mac_home(tmp_path, monkeypatch)

    assert token_store._macos_keychain_missing() is True


def test_preflight_honours_a_configured_default_that_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mac_home = _fake_mac_home(tmp_path, monkeypatch)
    custom = mac_home / "Library" / "Keychains" / "work.keychain-db"
    custom.write_bytes(b"kc")
    prefs = mac_home / "Library" / "Preferences" / "com.apple.security.plist"
    prefs.write_bytes(plistlib.dumps({"DefaultKeychain": {"path": str(custom)}}))

    assert token_store._macos_keychain_missing() is False


def test_preflight_detects_a_configured_default_that_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mac_home = _fake_mac_home(tmp_path, monkeypatch)
    prefs = mac_home / "Library" / "Preferences" / "com.apple.security.plist"
    prefs.write_bytes(
        plistlib.dumps({"DefaultKeychain": str(mac_home / "deleted.keychain-db")})
    )

    assert token_store._macos_keychain_missing() is True


@pytest.mark.parametrize(
    "payload",
    [
        {"DefaultKeychain": {"unfamiliar": "shape"}},
        {"DefaultKeychain": 42},
        {"SomethingElse": "entirely"},
    ],
)
def test_preflight_gives_the_keychain_the_benefit_of_the_doubt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict
):
    # One-sided by design: anything we can't read confidently must fall
    # through to "try it". A wrong True silently downgrades a working Mac.
    mac_home = _fake_mac_home(tmp_path, monkeypatch)
    prefs = mac_home / "Library" / "Preferences" / "com.apple.security.plist"
    prefs.write_bytes(plistlib.dumps(payload))

    assert token_store._macos_keychain_missing() is False


def test_preflight_survives_an_unreadable_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mac_home = _fake_mac_home(tmp_path, monkeypatch)
    prefs = mac_home / "Library" / "Preferences" / "com.apple.security.plist"
    prefs.write_bytes(b"not a plist at all")

    assert token_store._macos_keychain_missing() is False


def test_preflight_skips_keyring_without_latching_it_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _fake_mac_home(tmp_path, monkeypatch)

    assert token_store._try_keyring() is None
    # Nothing persisted: a Mac that gets repaired must recover on its own,
    # with no `flowly keychain retry` and no dotfile to delete.
    assert not token_store._keyring_marker_path().exists()


# ── plain-language failures ────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (-60006, "the macOS keychain prompt was dismissed"),
        (-25294, "this Mac has no keychain to save into"),
        (-25308, "macOS couldn't show the keychain prompt"),
        (-25293, "macOS turned down the keychain request"),
    ],
)
def test_os_status_codes_become_readable(status: int, expected: str):
    exc = Exception(f"Can't store password on keychain: ({status}, 'Unknown Error')")

    assert token_store.explain_keyring_error(exc) == expected


def test_unknown_failures_still_say_something_useful():
    assert (
        token_store.explain_keyring_error(RuntimeError("dbus went away"))
        == "the OS keychain wasn't available"
    )


def test_latch_records_both_the_reason_and_the_raw_error(caplog):
    exc = Exception("Can't store password on keychain: (-60006, 'Unknown Error')")

    with caplog.at_level("WARNING"):
        token_store.disable_keyring_for("set_password", exc)

    marker = token_store._keyring_marker_path().read_text(encoding="utf-8")
    assert "the macOS keychain prompt was dismissed" in marker
    assert "-60006" in marker  # raw code kept for support
    assert "flowly keychain retry" in caplog.text
    assert "Unknown Error" not in caplog.text  # …but not in the user's face


# ── one latch shared by every credential store ─────────────────────


class _RefusingBackend:
    __module__ = "keyring.backends.macOS"


class _RefusingKeyring:
    """Stand-in for a Mac that puts up the panel and gets cancelled.

    Patched into ``sys.modules`` rather than over ``_try_keyring`` so the
    real probe still runs — the latch it sets is the whole point here.
    """

    @staticmethod
    def get_keyring():
        return _RefusingBackend()

    @staticmethod
    def get_password(service, account):
        raise Exception("Can't get password from keychain: (-60006, 'Unknown Error')")

    @staticmethod
    def set_password(service, account, blob):
        raise Exception("Can't store password on keychain: (-60006, 'Unknown Error')")

    @staticmethod
    def delete_password(service, account):
        raise Exception("Can't delete password in keychain: (-60006, 'Unknown Error')")


@pytest.fixture
def refusing_keychain(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "keyring", _RefusingKeyring)


def test_account_save_falls_back_to_an_owner_only_file(refusing_keychain):
    status = token_store.save_credentials({"id_token": "abc"})

    assert status.backend == "file"
    assert status.secure is False
    assert token_store.load_credentials() == {"id_token": "abc"}
    mode = stat.S_IMODE(token_store._fallback_path().stat().st_mode)
    assert mode == 0o600


def test_subscription_store_writes_the_shared_latch(refusing_keychain):
    # Regression: xai/codex/zai only *read* the marker and never wrote it, so
    # a refusing keychain re-opened the OS panel on every single save.
    payload = xai_oauth.XAITokenPayload(
        access_token="access-123",
        refresh_token="refresh-123",
        expires_at=int(time.time()) + 3600,
    )

    backend = xai_oauth.save_token_payload(payload)

    assert backend.startswith("file:")
    assert token_store._keyring_marker_path().exists()
    assert xai_oauth.load_token_payload().access_token == "access-123"


def test_the_latch_is_honoured_across_stores(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(token_store, "_KEYRING_DISABLED", None)
    token_store._keyring_marker_path().parent.mkdir(parents=True, exist_ok=True)
    token_store._keyring_marker_path().write_text("# broken\n", encoding="utf-8")

    assert token_store._try_keyring() is None
    assert xai_oauth._try_keyring() is None


def test_retry_clears_both_latches(refusing_keychain):
    token_store.save_credentials({"id_token": "abc"})
    assert token_store._keyring_marker_path().exists()

    assert token_store.reset_keyring_disable() is True

    assert not token_store._keyring_marker_path().exists()
    assert token_store._KEYRING_DISABLED is None
    assert token_store._MACOS_KEYCHAIN_MISSING is None
    # Idempotent — a second retry has nothing to clear.
    assert token_store.reset_keyring_disable() is False


def test_fallback_hint_does_not_tell_mac_users_to_install_gnome_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _fake_mac_home(tmp_path, monkeypatch)

    detail = token_store.storage_status().detail

    assert "gnome-keyring" not in detail
    assert "this Mac has no keychain" in detail


# ── doctor surfaces it ─────────────────────────────────────────────


def _context(home: Path) -> DoctorContext:
    return DoctorContext(config_path=home / "config.json", data_dir=home)


def test_doctor_is_quiet_when_the_keychain_is_fine(tmp_path: Path):
    home = tmp_path / "home"
    (home / "credentials").mkdir(parents=True)
    ctx = _context(home)

    check_credential_storage(ctx)

    assert [r.status for r in ctx.results] == [Status.OK]


def test_doctor_warns_with_the_recorded_reason_and_the_way_out(tmp_path: Path):
    home = tmp_path / "home"
    (home / "credentials").mkdir(parents=True)
    (home / "credentials" / ".keychain-broken").write_text(
        "# Flowly disabled the OS keychain after this error:\n"
        "# the macOS keychain prompt was dismissed\n"
        "# set_password: PasswordSetError: (-60006, 'Unknown Error')\n"
        "# Recorded at unix=1\n"
        "# Delete this file to let Flowly try the keychain again.\n",
        encoding="utf-8",
    )
    ctx = _context(home)

    check_credential_storage(ctx)

    result = ctx.results[0]
    assert result.status is Status.WARN
    assert "the macOS keychain prompt was dismissed" in result.detail
    assert "flowly keychain retry" in result.detail
    # Not doctor's to repair — `--fix` must not claim it can.
    assert result.fixable is False


def test_doctor_reads_a_legacy_marker_written_by_an_older_build(tmp_path: Path):
    home = tmp_path / "home"
    (home / "credentials").mkdir(parents=True)
    (home / "credentials" / ".keychain-broken").write_text(
        "# Flowly disabled the OS keychain after this error:\n"
        "# set_password: PasswordSetError: Can't store password\n"
        "# Recorded at unix=1\n",
        encoding="utf-8",
    )
    ctx = _context(home)

    check_credential_storage(ctx)

    assert "set_password: PasswordSetError" in ctx.results[0].detail


def test_doctor_never_writes_while_checking(tmp_path: Path):
    home = tmp_path / "home"
    (home / "credentials").mkdir(parents=True)
    marker = home / "credentials" / ".keychain-broken"
    marker.write_text("# broken\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}

    check_credential_storage(_context(home))

    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
