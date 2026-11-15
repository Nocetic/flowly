"""What the runtime builder (scripts/build_runtime.py) installs.

The runtime carries Core's optional extras up front, as the open agent's
desktop bundle does, so a Desktop user never waits on a download for them.
Only the extras the builder names in LEFT_OUT_EXTRAS stay out.
"""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _builder():
    spec = importlib.util.spec_from_file_location("build_runtime", ROOT / "scripts" / "build_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exported_extras(builder, monkeypatch, tmp_path, source: Path) -> list[str]:
    calls: list[list[str]] = []

    def output(argv, **_):
        calls.append([str(a) for a in argv])
        return ""

    def run(argv, **_):
        if "build" in argv:
            out = Path(argv[argv.index("--out-dir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "flowly_ai-0-py3-none-any.whl").write_text("")

    monkeypatch.setattr(builder, "output", output)
    monkeypatch.setattr(builder, "run", run)
    builder.install_core(source, tmp_path / "python", tmp_path)
    [export] = [c for c in calls if c[:2] == ["uv", "export"]]
    return [export[i + 1] for i, arg in enumerate(export) if arg == "--extra"]


def test_the_runtime_installs_every_extra_but_the_left_out_ones(monkeypatch, tmp_path):
    builder = _builder()
    with (ROOT / "pyproject.toml").open("rb") as f:
        declared = set(tomllib.load(f)["project"]["optional-dependencies"])

    exported = _exported_extras(builder, monkeypatch, tmp_path, ROOT)

    assert set(exported) == declared - builder.LEFT_OUT_EXTRAS
    assert "search" in exported


def test_test_tools_and_the_linux_screen_stay_out(monkeypatch, tmp_path):
    builder = _builder()

    exported = _exported_extras(builder, monkeypatch, tmp_path, ROOT)

    assert "dev" not in exported
    assert "screen" not in exported


def test_a_new_extra_ships_without_touching_the_builder(monkeypatch, tmp_path):
    builder = _builder()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n'
        '[project.optional-dependencies]\nnew = ["a"]\ndev = ["b"]\nscreen = ["c"]\n'
    )

    exported = _exported_extras(builder, monkeypatch, tmp_path, tmp_path / "src")

    assert exported == ["new"]


def test_the_minor_version_link_uv_leaves_is_not_a_second_python(monkeypatch, tmp_path):
    # Beside the install uv leaves cpython-3.12-<platform>, a symlink on macOS
    # and Linux but a directory junction on Windows, which is_symlink() does
    # not see. The builder must still copy the one real install.
    builder = _builder()
    installs = tmp_path / "work" / "pythons"

    def run(argv, **_):
        real = installs / "cpython-3.12.13-windows-x86_64-none"
        (real).mkdir(parents=True)
        (real / "python.exe").write_text("")
        (real / "bin").mkdir()
        (real / "bin" / "python3").write_text("")
        # A junction reads as a plain directory to Path.is_symlink().
        (installs / "cpython-3.12-windows-x86_64-none").mkdir()

    monkeypatch.setattr(builder, "run", run)
    exe = builder.install_python("3.12", tmp_path / "work", tmp_path / "python")
    assert exe.exists()


# ── What ships: left-out files, and .pyc that never go stale ─────────────────

FBS = "onnxruntime/tools/ort_format_model/ort_flatbuffers_py/fbs"


def _fake_python(root: Path) -> tuple[Path, Path, Path]:
    """A Windows-shaped Python prefix with a little of everything the builder keeps or drops."""
    prefix = root / "python"
    stdlib = prefix / "Lib"
    site = stdlib / "site-packages"
    files = {
        "include/Python.h": "", "libs/python312.lib": "", "tcl/tcl8.6/init.tcl": "",
        "DLLs/_tkinter.pyd": "", "DLLs/tcl86t.dll": "", "DLLs/_testcapi.pyd": "", "DLLs/_ssl.pyd": "",
        "Lib/idlelib/__init__.py": "", "Lib/tkinter/__init__.py": "", "Lib/lib2to3/__init__.py": "",
        "Lib/turtle.py": "", "Lib/json/__init__.py": "VALUE = 1\n", "Lib/ensurepip/__init__.py": "",
        "Lib/site-packages/pip/__init__.py": "", "Lib/site-packages/pip-25.0.dist-info/METADATA": "",
        "Lib/site-packages/numpy/__init__.py": "", "Lib/site-packages/numpy/testing/__init__.py": "",
        "Lib/site-packages/numpy/_core/tests/test_a.py": "", "Lib/site-packages/numpy/core.pyi": "",
        "Lib/site-packages/numpy/_core/include/x.h": "", "Lib/site-packages/numpy/README.md": "",
        "Lib/site-packages/flowly/__init__.py": "VALUE = 1\n", "Lib/site-packages/flowly/tests/__init__.py": "",
        "Lib/site-packages/flowly/stubs.pyi": "", "Lib/threading.py": "", "Lib/tomllib/__init__.py": "",
        f"Lib/site-packages/{FBS}/__init__.py": "import glob, os\n"
        "__all__ = [os.path.basename(f)[:-3] for f in glob.glob(os.path.join(os.path.dirname(__file__), '*.py'))]\n",
        f"Lib/site-packages/{FBS}/Tensor.py": "VALUE = 1\n",
    }
    for name, text in files.items():
        (prefix / name).parent.mkdir(parents=True, exist_ok=True)
        (prefix / name).write_text(text)
    return prefix, stdlib, site


def test_what_a_runtime_never_runs_is_left_out(tmp_path):
    builder = _builder()
    prefix, stdlib, site = _fake_python(tmp_path)

    builder.prune(prefix, stdlib, site)

    gone = ["include", "libs", "tcl", "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/_testcapi.pyd",
            "Lib/idlelib", "Lib/tkinter", "Lib/lib2to3", "Lib/turtle.py", "Lib/site-packages/pip",
            "Lib/site-packages/pip-25.0.dist-info", "Lib/site-packages/numpy/_core/tests",
            "Lib/site-packages/numpy/core.pyi", "Lib/site-packages/numpy/_core/include/x.h"]
    kept = ["DLLs/_ssl.pyd", "Lib/threading.py", "Lib/tomllib/__init__.py", "Lib/json/__init__.py", "Lib/ensurepip/__init__.py",
            "Lib/site-packages/numpy/testing/__init__.py", "Lib/site-packages/numpy/README.md",
            # Core is shipped as it is built.
            "Lib/site-packages/flowly/tests/__init__.py", "Lib/site-packages/flowly/stubs.pyi"]
    assert [name for name in gone if (prefix / name).exists()] == []
    assert [name for name in kept if not (prefix / name).exists()] == []


def test_windows_letter_case_never_takes_a_module_the_runtime_needs(monkeypatch, tmp_path):
    # Path.glob ignores letter case on Windows: there `lib/thread*` (Tcl's thread package on macOS and Linux)
    # matched Lib/threading.py, and `lib/tk*` Lib/tkinter. Here every glob behaves as on Windows.
    import fnmatch
    builder = _builder()
    prefix, stdlib, site = _fake_python(tmp_path)

    def windows_glob(self, pattern, **_):
        found = [self]
        for part in pattern.split("/"):
            found = [c for d in found if d.is_dir() for c in d.iterdir() if fnmatch.fnmatch(c.name.lower(), part.lower())]
        return iter(found)

    monkeypatch.setattr(Path, "glob", windows_glob)

    builder.prune(prefix, stdlib, site)

    assert (prefix / "Lib" / "threading.py").is_file()
    assert (prefix / "Lib" / "tomllib" / "__init__.py").is_file()


def test_a_compile_pass_that_stopped_early_fails_the_build(monkeypatch, tmp_path):
    # The pass that turns the standard library and packages into .pyc runs unchecked (a file meant for another
    # Python must not stop it). Had it died, their sources would stay, compiled again at every start: the build
    # must say so rather than ship it.
    import subprocess
    import sys
    import pytest
    builder = _builder()
    prefix, stdlib, site = _fake_python(tmp_path)
    real_run = subprocess.run

    def run(args, *rest, **kwargs):
        if "-b" in [str(a) for a in args]:
            return subprocess.CompletedProcess(args, 1)
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(builder.subprocess, "run", run)

    with pytest.raises(SystemExit, match="compile but got no .pyc"):
        builder.compile_runtime(Path(sys.executable), stdlib, site)


def test_a_source_that_does_not_compile_keeps_its_source(tmp_path):
    import sys
    builder = _builder()
    prefix, stdlib, site = _fake_python(tmp_path)
    (stdlib / "py2_only.py").write_text("print 'meant for another Python'\n")

    builder.compile_runtime(Path(sys.executable), stdlib, site)

    assert (stdlib / "py2_only.py").is_file()
    assert not (stdlib / "py2_only.pyc").exists()


def test_only_core_keeps_its_sources(tmp_path):
    import sys
    builder = _builder()
    prefix, stdlib, site = _fake_python(tmp_path)

    builder.compile_runtime(Path(sys.executable), stdlib, site)

    assert not (stdlib / "json" / "__init__.py").exists()
    assert (stdlib / "json" / "__init__.pyc").is_file()
    assert (site / "flowly" / "__init__.py").is_file()
    assert list((site / "flowly" / "__pycache__").glob("__init__.*.pyc"))
    modes = builder.pyc_modes(prefix)
    assert modes["timestamp"] == modes["checked-hash"] == 0 and modes["unchecked-hash"] > 0


def test_a_package_that_lists_its_own_sources_keeps_them(tmp_path):
    # onnxruntime's flatbuffers bindings find their modules by globbing *.py: as .pyc only they find none.
    import sys
    builder = _builder()
    prefix, stdlib, site = _fake_python(tmp_path)

    builder.compile_runtime(Path(sys.executable), stdlib, site)

    fbs = site / FBS
    assert (fbs / "Tensor.py").is_file() and (fbs / "__init__.py").is_file()
    assert not (fbs / "Tensor.pyc").exists()
    assert list((fbs / "__pycache__").glob("Tensor.*.pyc"))
    assert builder.pyc_modes(fbs)["unchecked-hash"] == 2


def test_a_runtime_whose_file_times_moved_loads_its_bytecode_not_its_sources(tmp_path):
    # A zip keeps file times to 2 s, in local time: unpacked elsewhere, every source looks newer than its
    # timestamp .pyc and Python compiles it again at each start (-B never writes the result). An unchecked-hash
    # .pyc is loaded whatever the source's time. Proof: the source changes after the build, with a new time,
    # and the runtime still runs what was built.
    import os
    import subprocess
    import sys
    builder = _builder()
    prefix, stdlib, site = _fake_python(tmp_path)
    builder.compile_runtime(Path(sys.executable), stdlib, site)
    core = site / "flowly" / "__init__.py"
    core.write_text("VALUE = 2\n")
    shifted = core.stat().st_mtime + 10_801
    os.utime(core, (shifted, shifted))

    value = subprocess.run([sys.executable, "-s", "-P", "-B", "-c", "import flowly; print(flowly.VALUE)"],
                           cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(site)},
                           capture_output=True, text=True, check=True).stdout.strip()

    assert value == "1"
