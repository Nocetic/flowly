"""Boundary tests for the CLI sandbox wrapper.

SECURITY.md §2.2: ``flowly`` CLI invocations re-exec themselves
under the platform's OS sandbox (sandbox-exec on macOS, bwrap on
Linux). This file pins the gate-decision logic, profile generation,
and (on macOS) the actual sandbox-exec syntax + behaviour.

The intent is to catch regressions in:

  • The decision matrix that picks between "wrap" and "skip" based
    on env vars + config.
  • The SBPL profile (macOS) — syntactic validity and the specific
    deny rules that close the filesystem exfil vectors.
  • The bwrap argv (Linux) — structural shape and the specific
    isolation flags.

End-to-end sandbox enforcement testing (does ``ls ~/.ssh`` actually
fail under the profile?) runs only when ``/usr/bin/sandbox-exec``
exists, which means CI macOS runners only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from flowly.sandbox import cli_wrap


# ── Gate decision: env var precedence ────────────────────────────────


class TestGateEnvVar:
    """``FLOWLY_SANDBOX`` env var overrides everything else."""

    @pytest.mark.parametrize("val", ["0", "false", "off", "no", "FALSE", "Off"])
    def test_off_values_skip_reexec(self, val):
        with mock.patch.dict(os.environ, {"FLOWLY_SANDBOX": val}, clear=False):
            with mock.patch("os.execve") as execve:
                cli_wrap.maybe_reexec_sandboxed()
                execve.assert_not_called()

    def test_already_wrapped_skip_reexec(self):
        # Recursion guard — even with sandbox enabled, if we're
        # already inside, must not re-exec again.
        with mock.patch.dict(
            os.environ, {"FLOWLY_SANDBOX_WRAPPED": "1"}, clear=False
        ):
            with mock.patch("os.execve") as execve:
                cli_wrap.maybe_reexec_sandboxed()
                execve.assert_not_called()


# ── Gate decision: Nuitka-compiled bundle skip ───────────────────────


class TestGateNuitkaBundle:
    """In a Nuitka --standalone bundle ``sys.executable`` is a
    placeholder path (``<dist>/python3``) that does not exist on
    disk, so ``os.execve`` would ENOENT and the process would abort
    with exit 71 before any flowly code runs. The desktop bundle
    establishes its own outer sandbox, so the self-wrap is both
    redundant and broken inside Nuitka. This guards the early-return
    branch that detects the bundle.
    """

    def test_compiled_global_skips_reexec(self, monkeypatch):
        # Nuitka injects ``__compiled__`` into every compiled module's
        # globals. We inject it manually here to simulate the bundle
        # without actually compiling — both the function-local globals
        # lookup and the module attribute land in the same place.
        monkeypatch.setattr(cli_wrap, "__compiled__", object(), raising=False)
        # Force-on env to make sure the only reason we skip is the
        # compiled detection (not the off-switch).
        with mock.patch.dict(os.environ, {"FLOWLY_SANDBOX": "1"}, clear=False):
            with mock.patch("os.execve") as execve:
                cli_wrap.maybe_reexec_sandboxed()
                execve.assert_not_called()

    def test_sys_frozen_skips_reexec(self, monkeypatch):
        # Belt-and-braces fallback: PyInstaller-style ``sys.frozen``
        # flag also triggers the skip in case a future Nuitka version
        # drops ``__compiled__``.
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        with mock.patch.dict(os.environ, {"FLOWLY_SANDBOX": "1"}, clear=False):
            with mock.patch("os.execve") as execve:
                cli_wrap.maybe_reexec_sandboxed()
                execve.assert_not_called()

    def test_unfrozen_cpython_does_not_skip_on_nuitka_branch(self, monkeypatch):
        # Make sure plain CPython does NOT take the Nuitka branch.
        # We can't easily prove "execve called" without actually
        # re-execing — that's what the macos-only integration test
        # below covers — but we can prove the function gets past the
        # compiled check by triggering the next gate (recursion guard)
        # and observing the early-return there instead.
        monkeypatch.delattr(cli_wrap, "__compiled__", raising=False)
        monkeypatch.delattr(sys, "frozen", raising=False)
        with mock.patch.dict(
            os.environ, {"FLOWLY_SANDBOX_WRAPPED": "1"}, clear=False
        ):
            with mock.patch("os.execve") as execve:
                cli_wrap.maybe_reexec_sandboxed()
                execve.assert_not_called()  # Stopped by recursion guard, not Nuitka guard


# ── Gate decision: config fallback ───────────────────────────────────


class TestGateConfig:
    """When ``FLOWLY_SANDBOX`` is unset, ``config.security.sandbox``
    decides. Missing config or read errors default to enabled
    (fail-safe stance per SECURITY.md §2.2).
    """

    def test_config_sandbox_false_skips(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"security": {"sandbox": False}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        flowly_dir = tmp_path / ".flowly"
        flowly_dir.mkdir()
        (flowly_dir / "config.json").write_text(
            json.dumps({"security": {"sandbox": False}})
        )

        # Clear FLOWLY_SANDBOX so config decides
        monkeypatch.delenv("FLOWLY_SANDBOX", raising=False)
        with mock.patch("os.execve") as execve:
            cli_wrap.maybe_reexec_sandboxed()
            execve.assert_not_called()

    def test_config_missing_defaults_on(self, tmp_path, monkeypatch):
        # No config.json at all → default-on. On a non-Darwin/Linux
        # host the platform check returns without execve; we use
        # the gate helper directly to verify the decision portion.
        monkeypatch.setenv("HOME", str(tmp_path))
        # No .flowly dir created
        assert cli_wrap._config_sandbox_enabled() is True

    def test_config_corrupt_defaults_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        flowly_dir = tmp_path / ".flowly"
        flowly_dir.mkdir()
        (flowly_dir / "config.json").write_text("not valid json {")
        assert cli_wrap._config_sandbox_enabled() is True

    def test_config_sandbox_true_explicit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        flowly_dir = tmp_path / ".flowly"
        flowly_dir.mkdir()
        (flowly_dir / "config.json").write_text(
            json.dumps({"security": {"sandbox": True}})
        )
        assert cli_wrap._config_sandbox_enabled() is True


# ── SBPL profile (macOS) ──────────────────────────────────────────────


class TestSBPLProfile:
    """The generated profile string locks in the filesystem deny
    list. A regression that empties ``_DENY_READ_PATHS_REL`` would
    pass typecheck and runtime tests; only this assertion catches
    it.
    """

    def test_includes_ssh_deny(self):
        profile = cli_wrap._build_sbpl_profile("/Users/x")
        assert '(subpath "/Users/x/.ssh")' in profile

    def test_includes_aws_deny(self):
        profile = cli_wrap._build_sbpl_profile("/Users/x")
        assert '(subpath "/Users/x/.aws")' in profile

    def test_includes_keychain_deny(self):
        profile = cli_wrap._build_sbpl_profile("/Users/x")
        assert '(subpath "/Users/x/Library/Keychains")' in profile

    def test_includes_chrome_storage_deny(self):
        profile = cli_wrap._build_sbpl_profile("/Users/x")
        assert "Google/Chrome" in profile

    def test_emits_deny_file_read_block(self):
        profile = cli_wrap._build_sbpl_profile("/Users/x")
        assert "(deny file-read*" in profile

    def test_emits_write_allow_block(self):
        profile = cli_wrap._build_sbpl_profile("/Users/x")
        assert "(deny file-write*)" in profile
        assert '(subpath "/Users/x/.flowly")' in profile
        assert '(literal "/dev/null")' in profile

    def test_allows_process_exec(self):
        profile = cli_wrap._build_sbpl_profile("/Users/x")
        assert "(allow process-exec*)" in profile
        assert "(allow process-fork)" in profile

    def test_starts_with_version(self):
        profile = cli_wrap._build_sbpl_profile("/Users/x")
        assert profile.startswith("(version 1)")


class TestSBPLEscaping:
    def test_quotes_escaped(self):
        # SBPL strings use Lisp-style escaping. A path with a quote
        # in it (rare but possible) must be escaped or sandbox-exec
        # rejects the profile.
        assert cli_wrap._sbpl_string('a"b') == '"a\\"b"'

    def test_backslash_escaped(self):
        assert cli_wrap._sbpl_string("a\\b") == '"a\\\\b"'

    def test_newline_in_path_rejected(self):
        # Newline in a path string would confuse the SBPL tokenizer.
        # No real macOS path has one, so refuse outright.
        with pytest.raises(ValueError):
            cli_wrap._sbpl_string("a\nb")


# ── Real sandbox-exec invocation (macOS only) ────────────────────────


SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


@pytest.mark.skipif(
    sys.platform != "darwin" or not SANDBOX_EXEC.exists(),
    reason="requires macOS sandbox-exec",
)
class TestSandboxExecIntegration:
    """End-to-end: feed the generated profile to sandbox-exec and
    verify the kernel actually enforces the deny rules.
    """

    def _run_under_sandbox(self, profile_text, *cmd, timeout=5):
        with tempfile.NamedTemporaryFile(
            suffix=".sb", mode="w", delete=False
        ) as f:
            f.write(profile_text)
            f.flush()
            profile_path = f.name
        try:
            return subprocess.run(
                [str(SANDBOX_EXEC), "-f", profile_path, *cmd],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        finally:
            os.unlink(profile_path)

    def test_profile_syntactically_valid(self):
        profile = cli_wrap._build_sbpl_profile(str(Path.home()))
        result = self._run_under_sandbox(profile, "/bin/echo", "hello")
        assert result.returncode == 0, f"profile rejected: {result.stderr}"
        assert "hello" in result.stdout

    def test_ssh_read_denied(self):
        ssh_path = Path.home() / ".ssh"
        if not ssh_path.exists():
            pytest.skip("~/.ssh does not exist on this host")
        profile = cli_wrap._build_sbpl_profile(str(Path.home()))
        result = self._run_under_sandbox(
            profile, "/bin/ls", str(ssh_path),
        )
        # ``ls`` returns 1 when it can't access the path, and the
        # macOS sandboxd writes "Operation not permitted" to stderr.
        assert result.returncode != 0
        assert (
            "Operation not permitted" in result.stderr
            or "Operation not permitted" in result.stdout
        )

    def test_flowly_dir_read_allowed(self):
        flowly_dir = Path.home() / ".flowly"
        if not flowly_dir.exists():
            pytest.skip("~/.flowly does not exist on this host")
        profile = cli_wrap._build_sbpl_profile(str(Path.home()))
        # Just listing the directory is enough — it's well outside
        # the deny list and should succeed.
        result = self._run_under_sandbox(
            profile, "/bin/ls", str(flowly_dir),
        )
        assert result.returncode == 0


# ── bwrap argv (Linux side) ──────────────────────────────────────────


class TestBwrapArgs:
    """The bwrap argv pins the Linux isolation contract. Linux CI
    runners exec the binary; this file pins the *intent* (the
    flags) so a regression that swaps ``--unshare-net`` for
    ``--share-net`` (or vice versa) is a visible diff.
    """

    def test_includes_ro_root_bind(self):
        args = cli_wrap._build_bwrap_args("/home/x")
        # bwrap argument pairs aren't trivially detectable with
        # ``in`` because the flag and value are separate elements;
        # we walk the list looking for the (--ro-bind, "/", "/")
        # triple.
        for i in range(len(args) - 2):
            if args[i] == "--ro-bind" and args[i + 1] == "/" and args[i + 2] == "/":
                return
        pytest.fail("--ro-bind / / not found in bwrap args")

    def test_share_net_default(self):
        args = cli_wrap._build_bwrap_args("/home/x")
        assert "--share-net" in args
        # Phase A/B: must not unshare net (would kill all outbound).
        assert "--unshare-net" not in args

    def test_unshare_pid_uts_ipc(self):
        args = cli_wrap._build_bwrap_args("/home/x")
        for flag in ("--unshare-pid", "--unshare-uts", "--unshare-ipc"):
            assert flag in args, f"missing {flag}"

    def test_die_with_parent(self):
        args = cli_wrap._build_bwrap_args("/home/x")
        assert "--die-with-parent" in args

    def test_flowly_dir_writable(self):
        args = cli_wrap._build_bwrap_args("/home/x")
        # ``--bind-try /home/x/.flowly /home/x/.flowly`` must appear
        for i in range(len(args) - 2):
            if (
                args[i] == "--bind-try"
                and args[i + 1] == "/home/x/.flowly"
                and args[i + 2] == "/home/x/.flowly"
            ):
                return
        pytest.fail("~/.flowly not in bwrap allow-write list")
