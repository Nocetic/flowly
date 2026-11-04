"""Whether Core runs from a Flowly runtime: the folder Desktop ships and updates.

`scripts/build_runtime.py` builds that folder: a portable Python with Core in
its site-packages, a `flowly-runtime.json` manifest and a `bin/flowly`
launcher. The interpreter's prefix is `<runtime>/python`, so the manifest
sits beside it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MANIFEST = "flowly-runtime.json"


def runtime_root(prefix: str | None = None) -> Path | None:
    """The runtime folder this interpreter belongs to, or None outside one."""
    root = Path(prefix if prefix is not None else sys.prefix).parent
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or manifest.get("name") != "flowly-runtime":
        return None
    return root


def runtime_launcher(prefix: str | None = None) -> Path | None:
    """The runtime's own `flowly` launcher, or None outside a runtime."""
    root = runtime_root(prefix)
    if root is None:
        return None
    launcher = root / "bin" / ("flowly.cmd" if sys.platform == "win32" else "flowly")
    return launcher if launcher.is_file() else None
