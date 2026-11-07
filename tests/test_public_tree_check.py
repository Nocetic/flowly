"""The open repo's gate: scripts/check_public_tree.py."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_public_tree", Path(__file__).resolve().parents[1] / "scripts" / "check_public_tree.py")
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)

NAME = "".join(chr(code) for code in (104, 101, 114, 109, 101, 115))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    return tmp_path


def commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "-m", "c")
    return _git(repo, "rev-parse", "HEAD")


def test_code_tests_and_user_docs_may_go(repo):
    sha = commit(repo, {"flowly/x.py": "x = 1\n", "tests/test_x.py": "", "content/docs/a.md": "# A\n",
                        "README.md": "# Flowly\n", "flowly/docs_helper.py": "", "notes.md": "notes\n"})
    assert gate.problems(repo, sha) == []


@pytest.mark.parametrize("path", ["docs/engineering/phone-control.md", "docs/chat-wire-protocol.md",
                                  "output/pdf/deck.pdf", "ops/video/README.md", "FLOWLETS_ROADMAP.md",
                                  "flowly/MEMORY_AUDIT.md", "VOICE_PLAN.md"])
def test_internal_paths_are_refused(repo, path):
    sha = commit(repo, {"flowly/x.py": "", path: "internal\n"})
    assert gate.problems(repo, sha) == [f"internal path: {path}"]


def test_the_upstream_agents_name_is_refused_in_any_file(repo):
    sha = commit(repo, {"flowly/x.py": f"# like {NAME.title()} does\n", "flowly/y.py": "fine\n"})
    assert gate.problems(repo, sha) == ["upstream agent's name in: flowly/x.py"]


def test_only_pushes_to_the_open_repo_are_checked(repo):
    bad = commit(repo, {"docs/notes.md": "internal\n"})
    line = [f"refs/heads/main {bad} refs/heads/main {gate.ZERO}"]
    assert gate.check_push(repo, "origin", "https://github.com/someone/flowly-fork.git", line) == []
    assert gate.check_push(repo, "private", "git@github.com:someone/notes.git", line) == []
    for remote, url in (("public", "DISABLED"), ("x", "https://github.com/Nocetic/flowly.git"),
                        ("https://github.com/nocetic/flowly", "https://github.com/nocetic/flowly")):
        assert gate.check_push(repo, remote, url, line) == ["internal path: docs/notes.md"]


def test_the_open_repo_gets_main_and_tags_only(repo):
    good = commit(repo, {"flowly/x.py": ""})
    pushes = {
        "refs/heads/main": [],
        "refs/tags/v3.4.0": [],
        "refs/heads/feat/phone-control": ["only main and tags go to the open repo, not refs/heads/feat/phone-control"],
    }
    for ref, expected in pushes.items():
        assert gate.check_push(repo, "public", "", [f"refs/heads/x {good} {ref} {gate.ZERO}"]) == expected
    deleting = gate.check_push(repo, "public", "", [f"(delete) {gate.ZERO} refs/heads/main {good}"])
    assert deleting and deleting[0].startswith("deleting refs/heads/main")


def test_the_hook_refuses_a_real_push(repo, tmp_path_factory):
    """Installed in a clone, git itself runs it and the push does not happen."""
    script = repo / "scripts" / "check_public_tree.py"
    script.parent.mkdir()
    script.write_text(Path(gate.__file__).read_text())
    commit(repo, {"flowly/x.py": ""})
    subprocess.run(["python3", str(script), "--install-hook"], cwd=repo, check=True, capture_output=True)
    remote = tmp_path_factory.mktemp("open")
    _git(remote, "init", "-q", "--bare")
    _git(repo, "remote", "add", "public", str(remote))
    assert subprocess.run(["git", "-C", str(repo), "push", "-q", "public", "main"], capture_output=True).returncode == 0
    commit(repo, {"docs/engineering/x.md": "internal\n"})
    refused = subprocess.run(["git", "-C", str(repo), "push", "-q", "public", "main"], capture_output=True, text=True)
    assert refused.returncode != 0 and "internal path: docs/engineering/x.md" in refused.stderr
    assert _git(remote, "log", "-1", "--format=%s", "main") == "c"
    assert len(_git(remote, "rev-list", "main").split()) == 1
