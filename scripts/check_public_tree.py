#!/usr/bin/env python3
"""What may reach the open repo (Nocetic/flowly), checked before it does.

The open repo gets code, tests and the user docs in ``content/docs``. Design
notes, research, marketing drafts and operations live in the maintainers'
private notes repo. This refuses a push to the open repo when
the commit pushed carries one of those paths or the upstream agent's name,
and when it pushes anything but ``main`` or a tag.

Install once per clone (writes ``.git/hooks/pre-push``)::

    python scripts/check_public_tree.py --install-hook

Check a commit by hand::

    python scripts/check_public_tree.py HEAD
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: Folders that never go to the open repo.
INTERNAL_DIRS = ("docs/", "output/", "ops/")
#: Single files and name patterns that never go to the open repo.
INTERNAL_FILES = ("FLOWLETS_ROADMAP.md", "STREAMING_ENGINEERING.md")
INTERNAL_PATTERNS = (re.compile(r"(^|/)[^/]*_PLAN\.md$"), re.compile(r"(^|/)[^/]*_AUDIT\.md$"))
#: The upstream agent's name, kept out of this file itself.
_FORBIDDEN = "".join(chr(code) for code in (104, 101, 114, 109, 101, 115))
#: The open repo, by remote name or URL.
PUBLIC_NAME = "public"
PUBLIC_URL = re.compile(r"github\.com[:/]Nocetic/flowly(\.git)?/?$", re.IGNORECASE)
ZERO = "0" * 40

HOOK = """#!/bin/sh
# Installed by scripts/check_public_tree.py: refuses internal paths on the open repo.
exec python3 "$(git rev-parse --show-toplevel)/scripts/check_public_tree.py" --pre-push "$1" "$2"
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def problems(repo: Path, commit: str) -> list[str]:
    """Why ``commit`` may not reach the open repo; empty when it may."""
    found: list[str] = []
    for path in _git(repo, "ls-tree", "-r", "--name-only", commit).splitlines():
        if path.startswith(INTERNAL_DIRS) or path in INTERNAL_FILES or any(p.search(path) for p in INTERNAL_PATTERNS):
            found.append(f"internal path: {path}")
    named = subprocess.run(["git", "-C", str(repo), "grep", "-I", "-i", "-l", "-F", _FORBIDDEN, commit],
                           capture_output=True, text=True)
    for line in named.stdout.splitlines():
        found.append(f"upstream agent's name in: {line.split(':', 1)[-1]}")
    return found


def is_public(remote: str, url: str) -> bool:
    return remote == PUBLIC_NAME or bool(PUBLIC_URL.search(url or "")) or bool(PUBLIC_URL.search(remote or ""))


def check_push(repo: Path, remote: str, url: str, updates: list[str]) -> list[str]:
    """Problems with a push (git's pre-push stdin lines); empty when it may go."""
    if not is_public(remote, url):
        return []
    found: list[str] = []
    for line in updates:
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, remote_ref, _remote_sha = parts
        if local_sha == ZERO:
            found.append(f"deleting {remote_ref} on the open repo: do it by hand if you mean it")
            continue
        if remote_ref != "refs/heads/main" and not remote_ref.startswith("refs/tags/"):
            found.append(f"only main and tags go to the open repo, not {remote_ref}")
            continue
        found.extend(problems(repo, local_sha))
    return found


def _report(found: list[str]) -> int:
    if not found:
        return 0
    print("Refused: this must not reach the open repo (Nocetic/flowly).", file=sys.stderr)
    for item in found[:40]:
        print(f"  - {item}", file=sys.stderr)
    if len(found) > 40:
        print(f"  … and {len(found) - 40} more", file=sys.stderr)
    print("Internal notes belong in the maintainers' private notes repo.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("commit", nargs="?", help="commit to check (default HEAD)")
    parser.add_argument("--pre-push", nargs=2, metavar=("REMOTE", "URL"), help="run as git's pre-push hook")
    parser.add_argument("--install-hook", action="store_true", help="install the pre-push hook in this clone")
    args = parser.parse_args(argv)
    repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    if args.install_hook:
        hooks = Path(_git(repo, "rev-parse", "--git-path", "hooks").strip())
        hooks = hooks if hooks.is_absolute() else repo / hooks
        hook = hooks / "pre-push"
        if hook.exists() and "check_public_tree.py" not in hook.read_text():
            print(f"{hook} exists and is not ours; add this line to it:\n{HOOK.splitlines()[-1]}", file=sys.stderr)
            return 1
        hooks.mkdir(parents=True, exist_ok=True)
        hook.write_text(HOOK)
        hook.chmod(0o755)
        print(f"Installed {hook}")
        return 0
    if args.pre_push:
        remote, url = args.pre_push
        return _report(check_push(repo, remote, url, sys.stdin.read().splitlines()))
    return _report(problems(repo, args.commit or "HEAD"))


if __name__ == "__main__":
    sys.exit(main())
