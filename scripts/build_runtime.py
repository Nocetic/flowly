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

What a runtime never runs is left out (Tcl/Tk, IDLE, lib2to3, pip, C headers,
type stubs, test suites), and the rest is byte-compiled ahead with
unchecked-hash .pyc files, which Python loads without looking at the source's
time (2026-11-15: a zip keeps file times to 2 seconds in local time, so every
timestamp .pyc in the Windows runtime was stale and Core compiled its whole
tree again at every start). Everything but Core ships as .pyc alone; Core
keeps its .py beside them. A Windows install writes half the files it did.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import platform
import re
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

# The runtime carries every optional extra in pyproject.toml, so nothing is
# downloaded after install (as the open agent's desktop bundle does), except:
#   dev:    test and lint tools.
#   screen: Linux only. Its own installer puts playwright and Chrome into a
#           virtualenv on the machine (the cloud image), never into a runtime.
LEFT_OUT_EXTRAS = frozenset({"dev", "screen"})

# Every launch runs with -s (no user site-packages), -P (the working folder is
# not on sys.path, so a checkout there can't shadow Core) and -B (Python never
# writes .pyc files: the folder may sit inside a signed app; the build
# compiles everything ahead instead).
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
exec "$here/../python/bin/python3" -s -P -B -m flowly "$@"
"""

WINDOWS_LAUNCHER = """@echo off
rem Flowly CLI: runs Core on the Python that ships beside it.
"%~dp0..\\python\\python.exe" -s -P -B -m flowly %*
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
    # uv also leaves a minor-version link (cpython-3.12-…) beside the install:
    # a symlink on macOS and Linux, a directory junction on Windows, which
    # is_symlink() does not report. Only the full-version folder is the install.
    found = [p for p in installs.iterdir()
             if p.is_dir() and not p.is_symlink() and not getattr(p, "is_junction", bool)()
             and re.match(r"cpython-\d+\.\d+\.\d+", p.name)]
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


# Never run by a runtime, so never shipped (paths under the Python's prefix):
# Tcl/Tk and what needs it, headers and import libraries for building C code,
# man pages.
PREFIX_LEFT_OUT = ("include", "libs", "share", "tcl", "lib/tcl*", "lib/tk*", "lib/itcl*", "lib/thread*",
                   "lib/libtcl*", "lib/libtk*", "lib/pkgconfig")
# Extension modules of the above and CPython's own test modules (DLLs\ on Windows, lib-dynload/ elsewhere).
EXTENSIONS_LEFT_OUT = ("_tkinter*", "tcl*.dll", "tk*.dll", "_test*", "_ctypes_test*", "xxlimited*", "_xxtestfuzz*",
                       "_xxsubinterpreters*", "_xxinterpchannels*")
# Standard library modules no agent imports. ensurepip stays: `python -m ensurepip` brings pip back.
STDLIB_LEFT_OUT = ("idlelib", "tkinter", "turtledemo", "lib2to3", "test", "turtle.py")
# Installed packages: pip (Core installs with uv) and the files only a build or a type checker reads.
PACKAGES_LEFT_OUT = ("pip", "pip-*.dist-info")
PACKAGE_FILES_LEFT_OUT = frozenset({".pyi", ".h", ".hpp", ".c", ".cpp", ".pxd", ".pyx", ".pxi", ".f", ".f90", ".lib", ".chm"})
# Test suites inside packages (numpy.testing is a module, not a suite, and stays).
PACKAGE_TEST_DIRS = frozenset({"tests", "test"})
# Core: its own package keeps its sources (tracebacks, skills' scripts); everything else ships as .pyc only.
CORE_PACKAGE = "flowly"
# Packages that find their own modules by listing *.py (site-packages paths): they keep their sources too, with
# their .pyc in __pycache__ as Core's. The build's import check finds the next one.
KEEPS_SOURCES = ("onnxruntime/tools/ort_format_model/ort_flatbuffers_py/fbs",)


def python_paths(python: Path) -> tuple[Path, Path]:
    """The interpreter's standard library and site-packages folders."""
    stdlib, purelib = output([python, "-I", "-c",
                              "import sysconfig; p = sysconfig.get_paths(); print(p['stdlib']); print(p['purelib'])"]).splitlines()
    return Path(stdlib), Path(purelib)


def _remove(path: Path) -> int:
    """Removes a file or a folder; returns how many files went."""
    if path.is_symlink() or path.is_file():
        path.unlink()
        return 1
    if path.is_dir():
        count = sum(1 for p in path.rglob("*") if not p.is_dir())
        shutil.rmtree(path)
        return count
    return 0


def matching(folder: Path, pattern: str) -> list[Path]:
    """
    The paths under `folder` that `pattern` names, letter case and all on every system. Path.glob ignores case
    on Windows, where `lib/thread*` (Tcl's thread package elsewhere) would take Lib/threading.py.
    """
    found = [folder]
    for part in pattern.split("/"):
        found = [child for parent in found if parent.is_dir() for child in sorted(parent.iterdir())
                 if fnmatch.fnmatchcase(child.name, part)]
    return found


def prune(prefix: Path, stdlib: Path, site_packages: Path) -> int:
    """Removes what a runtime never runs (the *_LEFT_OUT lists). Returns how many files went."""
    removed = 0
    for pattern in PREFIX_LEFT_OUT:
        for path in matching(prefix, pattern):
            removed += _remove(path)
    for folder in (prefix / "DLLs", stdlib / "lib-dynload"):
        for pattern in EXTENSIONS_LEFT_OUT:
            for path in matching(folder, pattern):
                removed += _remove(path)
    for name in STDLIB_LEFT_OUT:
        for path in matching(stdlib, name):
            removed += _remove(path)
    for pattern in PACKAGES_LEFT_OUT:
        for path in matching(site_packages, pattern):
            removed += _remove(path)
    for top in sorted(site_packages.iterdir()):
        if top.name == CORE_PACKAGE or not top.is_dir():
            continue
        for path in sorted(top.rglob("*"), reverse=True):
            if path.is_dir() and path.name in PACKAGE_TEST_DIRS:
                removed += _remove(path)
            elif path.is_file() and path.suffix.lower() in PACKAGE_FILES_LEFT_OUT:
                removed += _remove(path)
    return removed


def compile_runtime(python: Path, stdlib: Path, site_packages: Path) -> int:
    """
    Byte-compiles everything ahead, as unchecked-hash .pyc (loaded without a look at the source's time), and
    leaves the standard library and every package but Core as .pyc only. The runtime runs with -B, so nothing
    compiles later. Returns how many sources went.
    """
    for cache in [*stdlib.rglob("__pycache__")]:
        shutil.rmtree(cache, ignore_errors=True)
    # -B: the compiler's own imports (and its worker processes') write no timestamp .pyc of their own.
    compile_all = [python, "-I", "-B", "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash"]
    core = site_packages / CORE_PACKAGE
    kept = [core, *(site_packages / name for name in KEEPS_SOURCES)]
    # Core and the packages that list their own sources: .pyc in __pycache__ beside them.
    run([*compile_all, *(folder for folder in kept if folder.is_dir())])
    # The rest: legacy .pyc in place of each source (-b). A file that does not compile (a template, a file meant
    # for another Python) keeps its source.
    sep = r"[/\\]"
    excluded = "|".join(re.escape(name).replace("/", sep) for name in (CORE_PACKAGE, *KEEPS_SOURCES))
    subprocess.run([str(a) for a in [*compile_all, "-b", "-x", f"{sep}site-packages{sep}({excluded}){sep}", stdlib]],
                   check=False)
    dropped = 0
    left: list[str] = []
    for source in stdlib.rglob("*.py"):
        if any(source.is_relative_to(folder) for folder in kept):
            continue
        if not source.with_suffix(".pyc").is_file():
            left.append(str(source))
            continue
        source.unlink()
        dropped += 1
    # A source left without its .pyc must be one that does not compile. If any other is left, the pass above
    # stopped early, and the runtime would compile it again at every start (what this build exists to end).
    compiles = subprocess.run([str(python), "-I", "-B", "-c", COMPILES_PROBE], input="\n".join(left).encode(),
                              capture_output=True, check=True).stdout.decode().splitlines()
    if compiles:
        raise SystemExit(f"{len(compiles)} source(s) compile but got no .pyc, e.g. {compiles[:5]}")
    return dropped


# The files (one a line on stdin) that compile: their source has no .pyc only if compileall never reached them.
COMPILES_PROBE = r"""
import sys
for name in sys.stdin.buffer.read().decode().splitlines():
    try:
        with open(name, "rb") as f:
            compile(f.read(), name, "exec", dont_inherit=True)
    except Exception:
        continue
    sys.stdout.buffer.write(name.encode() + b"\n")
"""


IMPORT_PROBE = r"""
import fnmatch, importlib, json, pkgutil, sys, sysconfig, warnings
warnings.simplefilter("ignore")
left_out, skipped = json.loads(sys.argv[1])
result = sys.argv[2]

class LeftOut:
    # What the build leaves out is missing in both runs: the check compares only what leaving files out and
    # shipping .pyc did, not modules gone on purpose (tkinter, and PIL.ImageTk with it).
    def find_spec(self, name, path=None, target=None):
        if any(fnmatch.fnmatchcase(name.partition(".")[0], pattern) for pattern in left_out):
            raise ModuleNotFoundError(f"left out: {name}", name=name)
        return None

sys.meta_path.insert(0, LeftOut())
ok = []

def attempt(name):
    try:
        module = importlib.import_module(name)
    except BaseException:
        return None
    ok.append(name)
    return module

def walk(name, path):
    for found in pkgutil.iter_modules(path, name + "."):
        if found.name.rpartition(".")[2] in skipped:
            continue
        module = attempt(found.name)
        if module is not None and found.ispkg:
            walk(found.name, list(getattr(module, "__path__", [])))

site = sysconfig.get_paths()["purelib"]
for top in pkgutil.iter_modules([site]):
    module = attempt(top.name)
    if module is not None and top.ispkg:
        walk(top.name, list(getattr(module, "__path__", [])))
# To a file, not stdout: a module may print, or swap sys.stdout, while it imports or as the interpreter exits.
with open(result, "w", encoding="utf-8") as out:
    json.dump(sorted(set(ok)), out)
"""
# Never imported by the check: test suites (left out) and __main__ (runs a program).
PROBE_SKIPS = sorted({*PACKAGE_TEST_DIRS, "__main__"})


def probe_left_out() -> list[str]:
    """Top-level module names the build leaves out, as patterns."""
    names = [name.removesuffix(".py") for name in STDLIB_LEFT_OUT]
    names += [pattern for pattern in EXTENSIONS_LEFT_OUT if not pattern.endswith(".dll")]
    names += [pattern for pattern in PACKAGES_LEFT_OUT if not pattern.endswith(".dist-info")]
    return names


def importable(python: Path, env: dict[str, str] | None = None) -> set[str]:
    """Every module in site-packages (Core's included, test suites not) that imports, in a fresh interpreter."""
    with tempfile.TemporaryDirectory(prefix="flowly-runtime-probe-") as neutral:
        result = Path(neutral) / "imported.json"
        done = subprocess.run([str(python), "-s", "-P", "-B", "-c", IMPORT_PROBE,
                               json.dumps([probe_left_out(), PROBE_SKIPS]), str(result)],
                              cwd=neutral, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if not result.is_file():
            # A module ended the interpreter while it imported: say which, from what it printed last.
            sys.stderr.write((done.stdout + done.stderr)[-4000:])
            raise SystemExit(f"build_runtime: the import check stopped (exit {done.returncode}) before it finished")
        return set(json.loads(result.read_text(encoding="utf-8")))


def pyc_modes(root: Path) -> dict[str, int]:
    """How many .pyc files carry each invalidation mode: timestamp, checked-hash, unchecked-hash."""
    modes = {"timestamp": 0, "checked-hash": 0, "unchecked-hash": 0}
    for pyc in root.rglob("*.pyc"):
        with pyc.open("rb") as f:
            flags = int.from_bytes(f.read(8)[4:8], "little")
        modes["timestamp" if not flags & 1 else "checked-hash" if flags & 2 else "unchecked-hash"] += 1
    return modes


def runtime_extras(source: Path) -> list[str]:
    """The optional extras the runtime installs: all of them but LEFT_OUT_EXTRAS."""
    with (source / "pyproject.toml").open("rb") as f:
        extras = tomllib.load(f)["project"].get("optional-dependencies", {})
    return sorted(set(extras) - LEFT_OUT_EXTRAS)


def install_core(source: Path, python: Path, work: Path) -> None:
    """Install the locked dependencies, then Core itself, into the runtime's Python."""
    requirements = work / "requirements.txt"
    extras = [arg for extra in runtime_extras(source) for arg in ("--extra", extra)]
    exported = output(["uv", "export", "--frozen", "--no-dev", "--no-default-groups",
                       "--no-emit-project", "--no-hashes", "--no-header", *extras,
                       "--format", "requirements-txt"], cwd=source)
    requirements.write_text(exported + "\n")
    wheels = work / "wheel"
    run(["uv", "build", "--wheel", "--out-dir", wheels, source])
    [wheel] = wheels.glob("flowly_ai-*.whl")
    # Not compiled here: compile_runtime does it once the runtime is complete.
    common = ["uv", "pip", "install", "--python", python, "--no-cache", "--link-mode", "copy"]
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


def smoke_test(runtime: Path, version: str, imported: set[str] | None = None) -> None:
    """
    The runtime starts from its own folder and reports the version it was built from, its .pyc files are all
    unchecked-hash, and everything that imported before the build left files out still imports.
    """
    launcher = runtime / "bin" / ("flowly.cmd" if sys.platform == "win32" else "flowly")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV", "UV_"))}
    python = runtime / "python" / ("python.exe" if sys.platform == "win32" else "bin/python3")
    # From a neutral folder: run from the checkout, `-m flowly` would quietly
    # import the checkout's code instead of the runtime's.
    before = _files(runtime)
    with tempfile.TemporaryDirectory(prefix="flowly-runtime-smoke-") as neutral:
        reported = output([launcher, "--version"], env=env, cwd=neutral)
        if version not in reported:
            raise SystemExit(f"build_runtime: launcher reports {reported!r}, expected {version}")
        located = output([python, "-s", "-P", "-B", "-c",
                          "import flowly, flowly.cli.commands, flowly.gateway.server, "
                          "fitz, PIL, cryptography, pydantic_core; print(flowly.__file__)"],
                         env=env, cwd=neutral)
    if not Path(located).resolve().is_relative_to(runtime.resolve()):
        raise SystemExit(f"build_runtime: Core imported from {located}, outside the runtime")
    modes = pyc_modes(runtime)
    if modes["timestamp"] or modes["checked-hash"] or not modes["unchecked-hash"]:
        raise SystemExit(f"build_runtime: the runtime's .pyc files are {modes}, expected unchecked-hash only")
    if imported is not None:
        missing = sorted(imported - importable(python, env))
        if missing:
            raise SystemExit(f"build_runtime: left-out files broke these imports: {missing[:20]}")
    # Inside a signed app a written file breaks the signature's seal.
    written = sorted(_files(runtime) - before)
    if written:
        raise SystemExit(f"build_runtime: running Core wrote into the runtime: {written[:5]}")


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


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
        stdlib, site_packages = python_paths(python)
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV", "UV_"))}
        imported = importable(python, clean_env)
        files = sum(1 for p in runtime.rglob("*") if not p.is_dir())
        removed = prune(runtime / "python", stdlib, site_packages)
        sources = compile_runtime(python, stdlib, site_packages)
        print(f"runtime: {files} files installed, {removed} left out, {sources} sources as .pyc only", flush=True)

    write_launchers(runtime)
    python_version = output([python, "-c", "import platform; print(platform.python_version())"])
    manifest = {"name": "flowly-runtime", "version": version, "commit": commit,
                "target": target, "python": python_version}
    (runtime / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    smoke_test(runtime, version, imported)
    files = sum(1 for p in runtime.rglob("*") if not p.is_dir())
    print(f"runtime: {files} files")
    print(f"runtime: {runtime} ({version}, {commit[:8]}, {target}, Python {python_version})")

    if not args.no_archive:
        path = archive(runtime, out, f"flowly-runtime-{version}-{target}")
        print(f"archive: {path} ({path.stat().st_size / 1_048_576:.0f} MB)")


if __name__ == "__main__":
    main()
