"""Whether Core runs from a Flowly runtime: the folder Desktop ships and updates.

`scripts/build_runtime.py` builds that folder: a portable Python with Core in
its site-packages, a `flowly-runtime.json` manifest and a `bin/flowly`
launcher. The interpreter's prefix is `<runtime>/python`, so the manifest
sits beside it.

A program that carries the runtime may name its own launcher in the manifest
(`"launcher"`, a path relative to the runtime folder). Flowly Desktop on macOS
does: the runtime sits inside `FlowlyAgent.app`, whose `Contents/MacOS/flowly-agent`
is the program the background service has always run, so the service keeps
that path across updates. It also names itself (`"host": "flowly-desktop"`):
Core then runs as it did when Desktop carried a compiled build, without
wrapping itself in the OS sandbox (`flowly/sandbox/cli_wrap.py`).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MANIFEST = "flowly-runtime.json"


def _manifest(prefix: str | None) -> tuple[Path, dict] | None:
    root = Path(prefix if prefix is not None else sys.prefix).parent
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or manifest.get("name") != "flowly-runtime":
        return None
    return root, manifest


def runtime_root(prefix: str | None = None) -> Path | None:
    """The runtime folder this interpreter belongs to, or None outside one."""
    found = _manifest(prefix)
    return found[0] if found else None


def host_launcher(prefix: str | None = None) -> Path | None:
    """The launcher the program carrying this runtime named, or None.

    Only an existing file counts: a manifest copied elsewhere, or a launcher
    left out of a build, falls back to running the interpreter itself.
    """
    found = _manifest(prefix)
    if found is None:
        return None
    root, manifest = found
    named = manifest.get("launcher")
    if not isinstance(named, str) or not named.strip():
        return None
    launcher = Path(os.path.normpath(root / named))
    return launcher if launcher.is_file() else None


def runtime_host(prefix: str | None = None) -> str | None:
    """The program carrying this runtime, as its manifest names it (`"host"`), or None."""
    found = _manifest(prefix)
    if found is None:
        return None
    host = found[1].get("host")
    return host.strip() if isinstance(host, str) and host.strip() else None


def python_command() -> list[str]:
    """This interpreter, with the flags a runtime always runs under (see the launchers)."""
    if runtime_root() is not None:
        return [sys.executable, "-s", "-P", "-B"]
    return [sys.executable]


def runtime_launcher(prefix: str | None = None) -> Path | None:
    """The command that starts Core from this runtime, or None outside a runtime."""
    found = _manifest(prefix)
    if found is None:
        return None
    named = host_launcher(prefix)
    if named is not None:
        return named
    launcher = found[0] / "bin" / ("flowly.cmd" if sys.platform == "win32" else "flowly")
    return launcher if launcher.is_file() else None
