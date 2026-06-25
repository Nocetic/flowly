#!/usr/bin/env python3
"""Create a reproducibility audit for a local research folder."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
}

IMPORTANT_NAMES = {
    "requirements.txt",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "environment.yml",
    "environment.yaml",
    "renv.lock",
    "package-lock.json",
    "package.json",
    "Dockerfile",
    "Makefile",
    "README.md",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_value(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=5,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def iter_files(root: Path, max_bytes: int) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_bytes:
            digest = ""
        else:
            digest = sha256(path)
        records.append({
            "path": str(path.relative_to(root)),
            "size": size,
            "sha256": digest,
        })
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".", help="Project or analysis directory")
    parser.add_argument("--out", default="repro-audit", help="Output directory")
    parser.add_argument("--max-bytes", type=int, default=25_000_000, help="Skip hashing files larger than this")
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        raise SystemExit(f"Path does not exist: {root}")

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    files = iter_files(root, args.max_bytes)
    important = [f for f in files if Path(str(f["path"])).name in IMPORTANT_NAMES]
    commit = git_value(root, "rev-parse", "HEAD")
    status = git_value(root, "status", "--short")

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "python": sys.version,
        "platform": platform.platform(),
        "git_commit": commit,
        "git_status_short": status,
        "file_count": len(files),
        "files": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    lines = [
        "# Reproducibility Audit",
        "",
        f"- Root: `{root}`",
        f"- Created UTC: {manifest['created_utc']}",
        f"- Python: `{sys.version.split()[0]}`",
        f"- Platform: `{platform.platform()}`",
        f"- Git commit: `{commit or 'not detected'}`",
        f"- Git dirty: {'yes' if status else 'no'}",
        f"- Files recorded: {len(files)}",
        "",
        "## Important Environment Files",
        "",
    ]
    if important:
        lines.extend(f"- `{f['path']}` ({f['size']} bytes)" for f in important)
    else:
        lines.append("- None detected.")
    lines.extend([
        "",
        "## Rerun Command",
        "",
        "```bash",
        "# Fill in the command that reproduces the target result.",
        "",
        "```",
        "",
        "## Expected Outputs",
        "",
        "| Output | Source script | Expected value/tolerance | Status |",
        "| --- | --- | --- | --- |",
        "",
        "## Missing Reproducibility Information",
        "",
        "- Data provenance:",
        "- Random seeds:",
        "- Package versions:",
        "- Hardware or instrument details:",
        "- External API/model versions:",
        "",
        "See `manifest.json` for file hashes.",
        "",
    ])
    (out / "repro-audit.md").write_text("\n".join(lines), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
