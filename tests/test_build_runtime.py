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
