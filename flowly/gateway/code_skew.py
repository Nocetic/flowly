"""Detect when the gateway is running stale code after a hot ``git pull``.

The gateway is a single long-lived process; its ``sys.modules`` is frozen at
boot. If the source checkout is updated underneath it — a manual ``git pull``,
or the brief window before ``flowly update``'s restart fires — a first-time
lazy import on a new code path can resolve a freshly-pulled module against a
stale cached dependency and raise a cryptic ImportError.

We snapshot the checkout revision at gateway startup and compare on demand, so
risky callers (e.g. provider/model hot-reload) can refuse with a clear "restart
the gateway" message instead of crashing.

For non-git installs (PyPI / uv-tool / managed binary) there's no revision to
read; the snapshot stays ``None`` and skew detection no-ops — never a false
positive.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_boot_revision: str | None = None

SKEW_MESSAGE = (
    "Flowly's code was updated under the running gateway. "
    "Restart it to load the new version:  flowly restart"
)


def _repo_root() -> Path | None:
    """The git checkout root when Flowly runs from a source/editable install."""
    try:
        import flowly

        root = Path(flowly.__file__).resolve().parent.parent  # parent of flowly/
        return root if (root / ".git").exists() else None
    except Exception:
        return None


def _read_revision() -> str | None:
    """Current ``HEAD`` revision of the checkout, or None for non-git installs."""
    root = _repo_root()
    if root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def snapshot_boot_revision() -> None:
    """Record the checkout revision at gateway startup. Call once, early."""
    global _boot_revision
    _boot_revision = _read_revision()


def is_skewed() -> bool:
    """True when the checkout moved since boot (a hot ``git pull``).

    Returns False for non-git installs (no boot revision) — never a false
    positive.
    """
    if _boot_revision is None:
        return False
    current = _read_revision()
    return current is not None and current != _boot_revision


def _reset_for_tests() -> None:
    """Clear the boot snapshot. **Test-only.**"""
    global _boot_revision
    _boot_revision = None
