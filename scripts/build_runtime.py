"""Build the Flowly runtime: Core, its locked dependencies and a Python, in one folder.

Flowly Desktop ships this folder inside the app and runs Core from it; a
Desktop update is a Core update. The cloud computer's image installs the
same folder. Nothing is compiled: it is a portable CPython (the build uv
installs), the dependencies `uv.lock` pins, and Core built from one git
revision, with a `flowly` launcher beside them.

    python3 scripts/build_runtime.py --ref v3.4.0 --out dist/runtime

The archive is built for the machine it runs on (native wheels), so each
target builds on its own runner. Layout of the folder:

    flowly-runtime/
    ├── flowly-runtime.json    version, commit, target, Python
    ├── bin/flowly             the CLI (flowly.cmd on Windows)
    └── python/                the interpreter, with Core in its site-packages

The source comes from `git archive <ref>`, never from the working tree, so
uncommitted edits can't ship.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = "flowly-runtime"
MANIFEST = "flowly-runtime.json"
DEFAULT_PYTHON = "3.12"

UNIX_LAUNCHER = """#!/bin/sh
# Flowly CLI: runs Core on the Python that ships beside it.
# Follows symlinks, so a link on PATH (~/.local/bin/flowly) works.
self=$0
while [ -L "$self" ]; do
  link=$(readlink "$self")
  case $link in
    /*) self=$link ;;
    *) self=$(dirname "$self")/$link ;;
  esac
done
here=$(cd "$(dirname "$self")" && pwd -P)
exec "$here/../python/bin/python3" -s -P -m flowly "$@"
"""

WINDOWS_LAUNCHER = """@echo off
rem Flowly CLI: runs Core on the Python that ships beside it.
"%~dp0..\\python\\python.exe" -s -P -m flowly %*
"""


def target_name() -> str:
    system = {"darwin": "darwin", "linux": "linux", "win32": "win"}.get(sys.platform)
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine)
    if system is None or arch is None:
        raise SystemExit(f"build_runtime: unsupported machine {sys.platform}/{machine}")
    return f"{system}-{arch}"


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+ " + subprocess.list2cmdline([str(a) for a in argv]), flush=True)
    return subprocess.run([str(a) for a in argv], check=True, **kwargs)


def output(argv: list[str], **kwargs) -> str:
    done = subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kwargs)
    if done.returncode != 0:
        sys.stderr.write(done.stdout + done.stderr)
        raise SystemExit(f"build_runtime: {Path(str(argv[0])).name} exited {done.returncode}")
    return done.stdout.strip()


def snapshot(ref: str, destination: Path) -> str:
    """Extract one revision's tracked files. Returns its full commit id."""
    commit = output(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=ROOT)
    archive = destination.parent / "source.tar"
    run(["git", "archive", "--format=tar", "--output", archive, commit], cwd=ROOT)
    destination.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(destination, filter="data")
    archive.unlink()
    return commit


def install_python(version: str, work: Path, destination: Path) -> Path:
    """Copy a uv-managed (portable) CPython into the runtime. Returns its executable."""
    installs = work / "pythons"
    run(["uv", "python", "install", version, "--install-dir", installs, "--no-bin"],
        env={**os.environ, "UV_PYTHON_DOWNLOADS": "automatic"})
    # uv also leaves a minor-version link (cpython-3.12-…) beside the install.
    found = [p for p in installs.iterdir()
             if p.is_dir() and not p.is_symlink() and p.name.startswith("cpython-")]
    if len(found) != 1:
        raise SystemExit(f"build_runtime: expected one Python under {installs}, found {found}")
    shutil.copytree(found[0], destination, symlinks=True)
    exe = destination / ("python.exe" if sys.platform == "win32" else "bin/python3")
    if not exe.exists():
        raise SystemExit(f"build_runtime: no interpreter at {exe}")
    # A managed interpreter refuses package installs; this one is ours to fill.
    for marker in destination.rglob("EXTERNALLY-MANAGED"):
        marker.unlink()
    return exe


def install_core(source: Path, python: Path, work: Path) -> None:
    """Install the locked dependencies, then Core itself, into the runtime's Python."""
    requirements = work / "requirements.txt"
    exported = output(["uv", "export", "--frozen", "--no-dev", "--no-default-groups",
                       "--no-emit-project", "--no-hashes", "--no-header",
                       "--format", "requirements-txt"], cwd=source)
    requirements.write_text(exported + "\n")
    wheels = work / "wheel"
    run(["uv", "build", "--wheel", "--out-dir", wheels, source])
    [wheel] = wheels.glob("flowly_ai-*.whl")
    common = ["uv", "pip", "install", "--python", python, "--no-cache", "--compile-bytecode",
              "--link-mode", "copy"]
    run([*common, "--requirement", requirements])
    run([*common, "--no-deps", wheel])


def write_launchers(runtime: Path) -> None:
    bin_dir = runtime / "bin"
    bin_dir.mkdir()
    if sys.platform == "win32":
        (bin_dir / "flowly.cmd").write_text(WINDOWS_LAUNCHER, newline="\r\n")
    else:
        launcher = bin_dir / "flowly"
        launcher.write_text(UNIX_LAUNCHER)
        launcher.chmod(0o755)


def smoke_test(runtime: Path, version: str) -> None:
    """The runtime starts from its own folder and reports the version it was built from."""
    launcher = runtime / "bin" / ("flowly.cmd" if sys.platform == "win32" else "flowly")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV", "UV_"))}
    python = runtime / "python" / ("python.exe" if sys.platform == "win32" else "bin/python3")
    # From a neutral folder: run from the checkout, `-m flowly` would quietly
    # import the checkout's code instead of the runtime's.
    with tempfile.TemporaryDirectory(prefix="flowly-runtime-smoke-") as neutral:
        reported = output([launcher, "--version"], env=env, cwd=neutral)
        if version not in reported:
            raise SystemExit(f"build_runtime: launcher reports {reported!r}, expected {version}")
        located = output([python, "-s", "-P", "-c",
                          "import flowly, flowly.cli.commands, flowly.gateway.server, "
                          "fitz, PIL, cryptography, pydantic_core; print(flowly.__file__)"],
                         env=env, cwd=neutral)
    if not Path(located).resolve().is_relative_to(runtime.resolve()):
        raise SystemExit(f"build_runtime: Core imported from {located}, outside the runtime")


def archive(runtime: Path, out: Path, name: str) -> Path:
    if sys.platform == "win32":
        path = out / f"{name}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in sorted(runtime.rglob("*")):
                zf.write(file, file.relative_to(runtime.parent))
    else:
        path = out / f"{name}.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            tar.add(runtime, arcname=runtime.name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (out / f"{path.name}.sha256").write_text(f"{digest}  {path.name}\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default="HEAD", help="git tag or commit to build (default HEAD)")
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "runtime")
    parser.add_argument("--python", default=DEFAULT_PYTHON, help=f"Python version (default {DEFAULT_PYTHON})")
    parser.add_argument("--no-archive", action="store_true", help="leave the folder, skip the archive")
    args = parser.parse_args()

    target = target_name()
    out = args.out.resolve()
    runtime = out / RUNTIME_DIR
    if runtime.exists():
        shutil.rmtree(runtime)
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="flowly-runtime-") as temp:
        work = Path(temp)
        source = work / "source"
        commit = snapshot(args.ref, source)
        version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
        runtime.mkdir()
        python = install_python(args.python, work, runtime / "python")
        install_core(source, python, work)

    write_launchers(runtime)
    python_version = output([python, "-c", "import platform; print(platform.python_version())"])
    manifest = {"name": "flowly-runtime", "version": version, "commit": commit,
                "target": target, "python": python_version}
    (runtime / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    smoke_test(runtime, version)
    print(f"runtime: {runtime} ({version}, {commit[:8]}, {target}, Python {python_version})")

    if not args.no_archive:
        path = archive(runtime, out, f"flowly-runtime-{version}-{target}")
        print(f"archive: {path} ({path.stat().st_size / 1_048_576:.0f} MB)")


if __name__ == "__main__":
    main()
