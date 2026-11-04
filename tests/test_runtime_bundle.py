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

    assert service_cmd._resolve_flowly_exec_argv() == [str(interpreter), "-s", "-P", "-m", "flowly"]
