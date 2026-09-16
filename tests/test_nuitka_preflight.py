from types import SimpleNamespace

import pytest

from scripts import nuitka_preflight as preflight


@pytest.fixture
def build_environment(tmp_path, monkeypatch):
    (tmp_path / "flowly/cli").mkdir(parents=True)
    (tmp_path / "flowly/cli/entry.py").write_text(
        '#    nuitka-project: --include-package=fastembed\n'
        '#    nuitka-project: --include-distribution-metadata=fastembed\n'
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "3.3.0"\n')
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "nuitka"\nversion = "2.8.10"\n')
    monkeypatch.setattr(preflight.importlib.metadata, "version", lambda name: "2.8.10" if name == "Nuitka" else "3.3.0")
    monkeypatch.setattr(preflight.importlib.metadata, "distribution", lambda name: object())
    monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(preflight.importlib, "import_module", lambda name: object())
    monkeypatch.setenv("FLOWLY_NUITKA_NO_SEMANTIC", "0")
    return tmp_path


def test_missing_packages_are_reported_together_before_compilation(build_environment, monkeypatch):
    monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda name: None if name in {"httpx_sse", "jwt"} else object())
    errors = preflight.validate(["--include-package=httpx_sse", "--include-package=jwt"], build_environment)
    assert errors == ["missing module: httpx_sse", "missing module: jwt"]


def test_intel_omits_only_entry_semantic_directives(build_environment, monkeypatch):
    monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda name: None if name == "fastembed" else object())
    assert "missing module: fastembed" in preflight.validate(["--include-package=mcp"], build_environment)
    monkeypatch.setenv("FLOWLY_NUITKA_NO_SEMANTIC", "1")
    assert preflight.validate(["--include-package=mcp"], build_environment) == []


def test_wrong_compiler_or_agent_version_is_rejected(build_environment, monkeypatch):
    monkeypatch.setattr(preflight.importlib.metadata, "version", lambda name: "0.0.0")
    errors = preflight.validate(["--include-package=mcp"], build_environment)
    assert any("Nuitka: installed 0.0.0, expected 2.8.10" in error for error in errors)
    assert any("flowly-ai: installed 0.0.0, expected 3.3.0" in error for error in errors)


def test_missing_data_and_metadata_are_rejected(build_environment, monkeypatch):
    def missing(name):
        raise preflight.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(preflight.importlib.metadata, "distribution", missing)
    errors = preflight.validate([
        "--include-distribution-metadata=mcp-types",
        "--include-data-dir=flowly/mcp_catalog=flowly/mcp_catalog",
        "--include-data-files=flowly/plugins_bundled/disk-cleanup/*.py=flowly/plugins_bundled/disk-cleanup/",
    ], build_environment)
    assert "missing metadata: mcp-types" in errors
    assert "missing data directory: flowly/mcp_catalog" in errors
    assert any("no data files match:" in error for error in errors)


def test_broken_lazy_transport_is_rejected(build_environment, monkeypatch):
    def broken(name):
        raise ImportError("missing transport dependency")

    monkeypatch.setattr(preflight.importlib, "import_module", broken)
    errors = preflight.validate(["--include-package=mcp"], build_environment)
    assert len(errors) == 3
    assert all("transport import failed" in error for error in errors)


@pytest.mark.parametrize("check_only,errors,expected,compiled", [
    (False, ["missing module: example"], 1, False),
    (True, [], 0, False),
    (False, [], 7, True),
])
def test_compile_is_gated_and_preserves_exit_status(monkeypatch, check_only, errors, expected, compiled):
    calls = []
    arguments = ["--standalone", "flowly/cli/entry.py"]
    monkeypatch.setattr(preflight, "validate", lambda args: errors)
    monkeypatch.setattr(preflight.os, "chdir", lambda path: None)
    monkeypatch.setattr(preflight.subprocess, "run", lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=7))
    assert preflight.main((["--check-only"] if check_only else []) + arguments) == expected
    assert bool(calls) is compiled
    if compiled:
        assert calls == [[preflight.sys.executable, "-m", "nuitka", *arguments]]
