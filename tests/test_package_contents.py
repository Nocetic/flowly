"""Every tracked Core file reaches the built wheel.

The wheel builder leaves out files that `.gitignore` matches, even tracked
ones. An unanchored `artifacts/` rule once dropped the `flowly/artifacts`
package from the runtime Desktop ships, and Core failed to start there.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(),
                    reason="needs a git checkout")
def test_no_tracked_core_file_is_gitignored():
    ignored = subprocess.run(
        ["git", "ls-files", "--cached", "--ignored", "--exclude-standard", "--", "flowly"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()

    assert ignored == []
