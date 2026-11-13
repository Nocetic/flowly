"""Flowly's skill library: skills that ship with Core but are not installed.

The core set in ``flowly/skills/`` is synced into every agent's
``~/.flowly/skills``. The library (``flowly/skills_library/``) holds the rest:
specialist skills an agent adds when its owner needs one (``flowly skills
install <name>``, or ``skill_manage`` action ``install``), with no network.
"""

from __future__ import annotations

import re
from pathlib import Path

LIBRARY_DIR = Path(__file__).resolve().parent.parent / "skills_library"


def library_skills() -> dict[str, Path]:
    """Every library skill by name (a folder with a SKILL.md)."""
    if not LIBRARY_DIR.is_dir():
        return {}
    return {
        item.name: item
        for item in sorted(LIBRARY_DIR.iterdir())
        if item.is_dir() and (item / "SKILL.md").is_file()
    }


def library_skill(name: str) -> Path | None:
    return library_skills().get(name.strip())


def describe(path: Path) -> str:
    """A library skill's one-line description, from its frontmatter."""
    try:
        text = (path / "SKILL.md").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    match = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else ""
