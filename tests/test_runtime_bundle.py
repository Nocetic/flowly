"""Core running from the Flowly runtime Desktop ships (scripts/build_runtime.py).

Inside that folder Desktop owns updates, and the background service must run
the runtime's own interpreter, never whatever `flowly` is first on PATH.
"""
from __future__ import annotations

import json
import sys

import pytest

from flowly import runtime_bundle
from flowly.cli import service_cmd, update_cmd


def _runtime(tmp_path, manifest: dict | None = None):
    root = tmp_path / "flowly-runtime"
    python = root / "python"
    python.mkdir(parents=True)
    body = manifest if manifest is not None else {
        "name": "flowly-runtime", "version": "3.4.0", "commit": "f" * 40,
        "target": "darwin-arm64", "python": "3.12.13",
    }
    (root / runtime_bundle.MANIFEST).write_text(json.dumps(body))
    bin_dir = root / "bin"
    bin_dir.mkdir()
    (bin_dir / ("flowly.cmd" if sys.platform == "win32" else "flowly")).write_text("")
    return root, python


def test_a_runtime_is_found_from_its_python(tmp_path):
    root, python = _runtime(tmp_path)

    assert runtime_bundle.runtime_root(str(python)) == root
    assert runtime_bundle.runtime_launcher(str(python)).parent == root / "bin"


def test_an_ordinary_python_is_not_a_runtime(tmp_path):
    (tmp_path / "venv").mkdir()

    assert runtime_bundle.runtime_root(str(tmp_path / "venv")) is None
    assert runtime_bundle.runtime_launcher(str(tmp_path / "venv")) is None


@pytest.mark.parametrize("manifest", [{"name": "something-else"}, ["flowly-runtime"]])
def test_a_foreign_manifest_is_not_a_runtime(tmp_path, manifest):
    _, python = _runtime(tmp_path, manifest)

    assert runtime_bundle.runtime_root(str(python)) is None


def test_update_leaves_a_desktop_runtime_to_desktop(tmp_path, monkeypatch):
    _, python = _runtime(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(python))

    assert update_cmd.detect_install_mode() == "managed"
    assert update_cmd.run_update(check_only=True) == 0


def test_the_service_runs_the_runtime_python_not_the_flowly_on_path(tmp_path, monkeypatch):
    _, python = _runtime(tmp_path)
    interpreter = python / "bin" / "python3"
    interpreter.parent.mkdir()
    interpreter.write_text("")
    elsewhere = tmp_path / "checkout-bin" / "flowly"
    elsewhere.parent.mkdir()
    elsewhere.write_text("")
    monkeypatch.setattr(sys, "prefix", str(python))
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr(sys, "argv", [str(python / "lib" / "flowly" / "__main__.py")])
    monkeypatch.setattr(service_cmd.shutil, "which", lambda name: str(elsewhere))

    assert service_cmd._resolve_flowly_exec_argv() == [str(interpreter), "-s", "-P", "-B", "-m", "flowly"]


def _desktop_mac_runtime(tmp_path):
    """The runtime as Desktop lays it out on macOS: inside FlowlyAgent.app, naming its launcher."""
    contents = tmp_path / "Flowly.app" / "Contents" / "Helpers" / "FlowlyAgent.app" / "Contents"
    launcher = contents / "MacOS" / "flowly-agent"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("")
    root, python = _runtime(contents / "Resources", {
        "name": "flowly-runtime", "version": "3.4.0", "commit": "f" * 40,
        "target": "darwin-arm64", "python": "3.12.13", "launcher": "../../MacOS/flowly-agent",
    })
    return root, python, launcher


def test_the_launcher_desktop_names_is_the_runtimes_launcher(tmp_path):
    root, python, launcher = _desktop_mac_runtime(tmp_path)

    assert runtime_bundle.runtime_root(str(python)) == root
    assert runtime_bundle.host_launcher(str(python)) == launcher
    assert runtime_bundle.runtime_launcher(str(python)) == launcher


def test_the_service_runs_the_launcher_desktop_names(tmp_path, monkeypatch):
    # The path the background service has always run on macOS, so an update
    # that replaces the runtime never leaves the service pointing at nothing.
    _, python, launcher = _desktop_mac_runtime(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(python))
    monkeypatch.setattr(sys, "executable", str(python / "bin" / "python3"))

    assert service_cmd._resolve_flowly_exec_argv() == [str(launcher)]


def test_a_named_launcher_that_is_missing_falls_back_to_the_interpreter(tmp_path, monkeypatch):
    _, python, launcher = _desktop_mac_runtime(tmp_path)
    launcher.unlink()
    interpreter = python / "bin" / "python3"
    monkeypatch.setattr(sys, "prefix", str(python))
    monkeypatch.setattr(sys, "executable", str(interpreter))

    assert runtime_bundle.host_launcher(str(python)) is None
    assert runtime_bundle.runtime_launcher(str(python)).name.startswith("flowly")
    assert service_cmd._resolve_flowly_exec_argv() == [str(interpreter), "-s", "-P", "-B", "-m", "flowly"]


def test_desktop_names_itself_as_the_host(tmp_path):
    root, python = _runtime(tmp_path, {"name": "flowly-runtime", "host": "flowly-desktop"})

    assert runtime_bundle.runtime_host(str(python)) == "flowly-desktop"
    assert runtime_bundle.runtime_host(str(_runtime(tmp_path / "plain")[1])) is None


def test_a_desktop_runtime_does_not_wrap_itself_in_the_sandbox(tmp_path, monkeypatch):
    # Desktop has never wrapped its agent; the runtime must not change what
    # the agent on a person's Mac may reach.
    from unittest import mock

    from flowly.sandbox import cli_wrap

    _, python = _runtime(tmp_path, {"name": "flowly-runtime", "host": "flowly-desktop"})
    monkeypatch.setattr(sys, "prefix", str(python))
    monkeypatch.setenv("FLOWLY_SANDBOX", "1")
    monkeypatch.delenv("FLOWLY_SANDBOX_WRAPPED", raising=False)
    with mock.patch("os.execve") as execve, mock.patch.object(cli_wrap, "_reexec_macos") as macos, \
            mock.patch.object(cli_wrap, "_reexec_linux") as linux:
        cli_wrap.maybe_reexec_sandboxed()

    execve.assert_not_called()
    macos.assert_not_called()
    linux.assert_not_called()


def test_a_runtime_without_a_host_still_wraps_itself(tmp_path, monkeypatch):
    from unittest import mock

    from flowly.sandbox import cli_wrap

    _, python = _runtime(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(python))
    monkeypatch.setenv("FLOWLY_SANDBOX", "1")
    monkeypatch.delenv("FLOWLY_SANDBOX_WRAPPED", raising=False)
    monkeypatch.setattr(cli_wrap.platform, "system", lambda: "Linux")
    with mock.patch.object(cli_wrap, "_reexec_linux") as linux:
        cli_wrap.maybe_reexec_sandboxed()

    linux.assert_called_once()


def test_core_starts_its_own_children_with_the_runtime_flags(tmp_path, monkeypatch):
    # -B above all: a child writing .pyc files into a signed app breaks its seal.
    from flowly import profile_host
    from flowly.mcp.server import managed_tools

    _, python = _runtime(tmp_path)
    interpreter = str(python / "bin" / "python3")
    monkeypatch.setattr(sys, "prefix", str(python))
    monkeypatch.setattr(sys, "executable", interpreter)

    assert profile_host._runtime_command() == [interpreter, "-s", "-P", "-B", "-m", "flowly.cli.entry"]
    assert managed_tools._callback_command() == (interpreter, ["-s", "-P", "-B", "-m", "flowly", "mcp", "tools"])


def test_outside_a_runtime_children_run_as_before(tmp_path, monkeypatch):
    from flowly import profile_host
    from flowly.mcp.server import managed_tools

    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    assert profile_host._runtime_command() == ["/usr/bin/python3", "-m", "flowly.cli.entry"]
    assert managed_tools._callback_command() == ("/usr/bin/python3", ["-m", "flowly", "mcp", "tools"])
