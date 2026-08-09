"""Machine identity isolation for non-default homes.

``register_machine`` is idempotent by ``machineId``: same id → same
Firestore server. Before this fix a dev instance (``FLOWLY_HOME`` set, e.g.
a ``scripts/dev-install.sh`` setup) shared the desktop's ``.machine-id``,
so logging in from it re-bound the user's REAL server — the dev bot took
over the relay connection and received the real agent's traffic.

Contract (mirrors test_account_home_isolation.py):
  * default home → exactly the historical behavior, byte for byte;
  * non-default home → its own ``.machine-id`` inside the home, and a
    ``-dev`` suffix on the human-readable machine name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flowly.account import fingerprint


@pytest.fixture(autouse=True)
def _fresh_caches():
    fingerprint.machine_id.cache_clear()
    fingerprint.machine_name.cache_clear()
    yield
    fingerprint.machine_id.cache_clear()
    fingerprint.machine_name.cache_clear()


def test_default_home_uses_shared_identity(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FLOWLY_HOME", raising=False)
    monkeypatch.setattr(fingerprint, "_read_shared_machine_id", lambda: "shared-uuid-1234")
    assert fingerprint.machine_id() == "shared-uuid-1234"
    assert not fingerprint.machine_name().split(" ")[0].endswith("-dev")


def test_dev_home_never_uses_the_shared_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fingerprint, "_read_shared_machine_id", lambda: "shared-uuid-1234")
    dev_id = fingerprint.machine_id()
    assert dev_id != "shared-uuid-1234"
    # Persisted inside the home, so the dev registration is stable across runs.
    assert (tmp_path / "home" / ".machine-id").read_text().strip() == dev_id


def test_dev_home_identity_is_stable_across_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    first = fingerprint.machine_id()
    fingerprint.machine_id.cache_clear()
    assert fingerprint.machine_id() == first


def test_two_dev_homes_get_distinct_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "a"))
    id_a = fingerprint.machine_id()
    fingerprint.machine_id.cache_clear()
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "b"))
    assert fingerprint.machine_id() != id_a


def test_dev_home_name_carries_dev_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    name = fingerprint.machine_name()
    host = name.split(" ")[0]
    assert host.endswith("-dev")


def test_unwritable_dev_home_still_diverges_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    monkeypatch.setattr(fingerprint, "_read_shared_machine_id", lambda: "shared-uuid-1234")

    def _no_write(self, *a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(Path, "write_text", _no_write)
    dev_id = fingerprint.machine_id()
    assert dev_id != "shared-uuid-1234"
    fingerprint.machine_id.cache_clear()
    assert fingerprint.machine_id() == dev_id
