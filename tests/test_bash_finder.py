"""Tests for bash_finder — Mac regression anchor + Windows resolution paths.

Two invariants these tests enforce:

  1. Mac / Linux return None unconditionally, with ZERO filesystem or
     environment access. If any of the regression-anchor tests start
     failing, the Mac pipeline has drifted and needs investigation before
     merge. The Windows shell work must not alter Mac behaviour.

  2. Windows resolution order: FLOWLY_BASH_PATH > standard Git install
     paths > PATH lookup. This order is safety-critical because the
     flowly-desktop bundle injects FLOWLY_BASH_PATH to guarantee version
     pinning; if the order ever got flipped, users' arbitrary system Git
     installs would start winning over our vetted bundle.
"""

from unittest.mock import patch

import pytest

from flowly.exec.bash_finder import find_bash, reset_bash_cache


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear cache before and after every test — cache state would otherwise
    leak across tests and hide ordering bugs."""
    reset_bash_cache()
    yield
    reset_bash_cache()


# ── Mac / Linux regression anchors ────────────────────────────────────────
# These must remain untouched if the Windows shell strategy ever evolves.


def test_macos_returns_none_without_any_filesystem_access(monkeypatch):
    """Mac: must never touch filesystem, never read env vars.

    Uses AssertionError side-effects on filesystem mocks so any accidental
    call during the Mac branch immediately fails the test loudly.
    """
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "darwin")
    with patch("os.path.isfile", side_effect=AssertionError("must not be called on Mac")), \
         patch("shutil.which", side_effect=AssertionError("must not be called on Mac")):
        assert find_bash() is None


def test_linux_returns_none_without_any_filesystem_access(monkeypatch):
    """Linux shares the Mac contract — bash resolution is a Windows-only concern."""
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "linux")
    with patch("os.path.isfile", side_effect=AssertionError("must not be called on Linux")), \
         patch("shutil.which", side_effect=AssertionError("must not be called on Linux")):
        assert find_bash() is None


def test_macos_ignores_flowly_bash_path_env_var(tmp_path, monkeypatch):
    """Stray FLOWLY_BASH_PATH in a dev shell must not alter Mac behaviour."""
    fake_bash = tmp_path / "bash.exe"
    fake_bash.write_text("")
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "darwin")
    monkeypatch.setenv("FLOWLY_BASH_PATH", str(fake_bash))
    assert find_bash() is None


# ── Windows resolution order ──────────────────────────────────────────────


def test_windows_env_var_wins(tmp_path, monkeypatch):
    """FLOWLY_BASH_PATH has highest priority — the bundled version must win."""
    fake_bash = tmp_path / "bundled-bash.exe"
    fake_bash.write_text("")
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "win32")
    monkeypatch.setenv("FLOWLY_BASH_PATH", str(fake_bash))
    assert find_bash() == str(fake_bash)


def test_windows_env_var_ignored_when_file_missing(monkeypatch):
    """Stale env var pointing at a removed binary falls through, doesn't error."""
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "win32")
    monkeypatch.setenv("FLOWLY_BASH_PATH", r"C:\nonexistent\bash.exe")
    with patch("os.path.isfile", return_value=False), \
         patch("shutil.which", return_value=None):
        assert find_bash() is None


def test_windows_standard_git_path(monkeypatch):
    """Second tier: user has Git for Windows installed at the default path."""
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "win32")
    monkeypatch.delenv("FLOWLY_BASH_PATH", raising=False)
    std_path = r"C:\Program Files\Git\bin\bash.exe"
    with patch("os.path.isfile", side_effect=lambda p: p == std_path), \
         patch("shutil.which", return_value=None):
        assert find_bash() == std_path


def test_windows_localappdata_path(monkeypatch):
    """Per-user Git install under LOCALAPPDATA is still discovered."""
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "win32")
    monkeypatch.delenv("FLOWLY_BASH_PATH", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    expected = r"C:\Users\test\AppData\Local\Programs\Git\bin\bash.exe"
    with patch("os.path.isfile", side_effect=lambda p: p == expected), \
         patch("shutil.which", return_value=None):
        assert find_bash() == expected


def test_windows_path_lookup_fallback(monkeypatch):
    """Last tier: bash on PATH from a non-standard install."""
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "win32")
    monkeypatch.delenv("FLOWLY_BASH_PATH", raising=False)
    path_bash = r"C:\custom\tools\bash.exe"
    with patch("os.path.isfile", return_value=False), \
         patch("shutil.which", return_value=path_bash):
        assert find_bash() == path_bash


def test_windows_no_bash_anywhere_returns_none(monkeypatch):
    """All tiers miss → None. Executor will fall back to PowerShell."""
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "win32")
    monkeypatch.delenv("FLOWLY_BASH_PATH", raising=False)
    with patch("os.path.isfile", return_value=False), \
         patch("shutil.which", return_value=None):
        assert find_bash() is None


# ── Caching behaviour ─────────────────────────────────────────────────────


def test_caching_does_not_redo_disk_checks(monkeypatch):
    """Second call must not hit the filesystem again — bundled prod binary
    runs find_bash() from the hot path, cache miss is a perf regression."""
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "win32")
    monkeypatch.setenv("FLOWLY_BASH_PATH", r"C:\test\bash.exe")

    call_count = {"n": 0}

    def counting_isfile(p):
        call_count["n"] += 1
        return True

    with patch("os.path.isfile", counting_isfile):
        first = find_bash()
        # Flip the env var between calls — cache must ignore the change.
        monkeypatch.setenv("FLOWLY_BASH_PATH", r"C:\different\bash.exe")
        second = find_bash()

    assert first == second == r"C:\test\bash.exe"
    # Only the env-var branch's isfile check ran. A second hit means
    # re-resolution happened and the cache is broken.
    assert call_count["n"] == 1


def test_reset_cache_allows_re_search(monkeypatch):
    """reset_bash_cache() is the test-only escape hatch — must work."""
    monkeypatch.setattr("flowly.exec.bash_finder.sys.platform", "win32")
    monkeypatch.setenv("FLOWLY_BASH_PATH", r"C:\a\bash.exe")
    with patch("os.path.isfile", return_value=True):
        assert find_bash() == r"C:\a\bash.exe"

    reset_bash_cache()
    monkeypatch.setenv("FLOWLY_BASH_PATH", r"C:\b\bash.exe")
    with patch("os.path.isfile", return_value=True):
        assert find_bash() == r"C:\b\bash.exe"
