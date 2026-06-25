"""Pre-run skill-tree snapshots for whole-pass rollback.

Auto-applied skill ops are safe because every pass tars the skills tree first;
``rollback(id)`` restores it. Snapshots live OUTSIDE the skills dir
(``~/.flowly/skills_backups/``) so the skills scanner never sees them.
"""

from __future__ import annotations

import shutil
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


def _default_skills_dir() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "skills"


def _default_backups_dir() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "skills_backups"


class SkillSnapshots:
    def __init__(self, skills_dir: Path | None = None, backups_dir: Path | None = None, keep: int = 10):
        self.skills_dir = Path(skills_dir) if skills_dir else _default_skills_dir()
        self.backups_dir = Path(backups_dir) if backups_dir else _default_backups_dir()
        self.keep = keep

    def snapshot(self, reason: str = "") -> Optional[str]:
        """Tar the skills tree; return a snapshot id (or None if nothing to back up)."""
        if not self.skills_dir.exists():
            return None
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        snap_id = f"{ts}-{uuid.uuid4().hex[:6]}"
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        tar_path = self.backups_dir / f"{snap_id}.tar.gz"
        try:
            with tarfile.open(tar_path, "w:gz") as tar:
                # store under a top-level "skills/" so restore is unambiguous
                tar.add(self.skills_dir, arcname="skills")
            if reason:
                (self.backups_dir / f"{snap_id}.reason.txt").write_text(reason, encoding="utf-8")
            self._prune()
            return snap_id
        except Exception as exc:
            logger.warning(f"[skill-snapshot] snapshot failed: {exc}")
            return None

    def restore(self, snap_id: str) -> bool:
        """Replace the skills tree with the snapshot's contents. Returns success."""
        tar_path = self.backups_dir / f"{snap_id}.tar.gz"
        if not tar_path.exists():
            return False
        try:
            # snapshot the current tree first so restore itself is undoable
            self.snapshot(reason=f"pre-restore-of-{snap_id}")
            if self.skills_dir.exists():
                shutil.rmtree(self.skills_dir)
            with tarfile.open(tar_path, "r:gz") as tar:
                tar.extractall(self.skills_dir.parent)  # extracts the "skills/" arcname
            return True
        except Exception as exc:
            logger.warning(f"[skill-snapshot] restore({snap_id}) failed: {exc}")
            return False

    def list_snapshots(self) -> list[str]:
        if not self.backups_dir.exists():
            return []
        ids = [p.name[: -len(".tar.gz")] for p in self.backups_dir.glob("*.tar.gz")]
        return sorted(ids, reverse=True)

    def _prune(self) -> None:
        snaps = sorted(self.backups_dir.glob("*.tar.gz"))
        excess = len(snaps) - self.keep
        for p in snaps[:max(0, excess)]:
            try:
                p.unlink()
                (self.backups_dir / f"{p.name[:-len('.tar.gz')]}.reason.txt").unlink(missing_ok=True)
            except OSError:
                pass
