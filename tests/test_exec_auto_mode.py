"""Auto runs read-only commands without asking, and asks for everything else.

Auto judged a command by its first word: `echo hi && rm -r ./build` and
`echo x | bash` ran unasked while `ls -la` and `git status` asked. Every part
of a chain is judged now, and a read reaches what `read_file` reaches: home,
the workspace and the folder the command starts in, never a protected path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from flowly.exec import ExecConfig, ExecRequest, execute_command
from flowly.exec.approvals import ExecApprovalStore
from flowly.exec.auto_rules import programs_to_remember, runs_without_asking

RUNS = [
    "ls",
    "ls -la",
    "ls | grep notes",
    "git status",
    "git log --oneline -5",
    "git diff",
    "git branch -a",
    "grep -rn TODO .",
    'find . -name "*.md"',
    "cat notes.md",
    "wc -l notes.md",
    "sort notes.md | uniq -c",
    "head -n 3 notes.md && tail -n 3 notes.md",
    "pwd",
    "echo hello",
    "date +%Y",
    "cd sub && ls",
    "cd sub; pwd",
    "sleep 1",
    # The shell writing nowhere, $HOME, home itself: read as read_file reads.
    "ls 2>/dev/null",
    "ls >/dev/null 2>&1",
    "cat notes.md || true",
    "wc -l < notes.md",
    "echo $HOME",
    "ls ~",
    "cd ~ && ls",
    "cd",
    "sed -n '1,2p' notes.md",
]

ASKS = [
    # The holes: the first word was harmless, the rest was not looked at.
    "echo hi && rm -r ./build",
    "echo hi; touch marker",
    "echo x | bash",
    "cat notes.md | sh",
    # Outside the folder, or a file the folder holds no claim to.
    "cat /etc/passwd",
    "cat ../outside.txt",
    # Options that write or run something.
    "sort -o out.txt notes.md",
    "sort -ro out.txt notes.md",
    "sort -oout.txt notes.md",
    "find . -delete",
    "find . -exec rm {} ;",
    "rg --pre=sh TODO",
    "tree -o out.txt",
    "uniq notes.md out.txt",
    "git -c core.pager=sh log",
    "git log --output=out.txt",
    "git branch new-branch",
    "git tag v1",
    "git remote add origin x",
    "git stash",
    "git commit -m x",
    "git push",
    # The shell itself acting.
    "echo hi > notes.md",
    "ls $(pwd)",
    "ls `pwd`",
    "ls &",
    "(ls)",
    "FOO=1 ls",
    "./script.sh",
    # Programs that write or run code.
    "rm notes.md",
    "python3 -c 'print(1)'",
    "npm install",
    "curl https://example.com",
    "cd .. && ls",
    "cd /tmp && ls",
    "cd sub && cat ../../outside.txt",
    "echo $USER",
    "echo $HOMEx",
    "sed -i '' 's/a/b/' notes.md",
    "sed -n '1w out.txt' notes.md",
    "sed 's/a/b/' notes.md",
    "grep -Rn TODO .",
    "rg -L TODO",
    "cat < ../outside.txt",
    "cat {notes,other}.md",
]


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    work = tmp_path / "project"
    work.mkdir()
    (work / "sub").mkdir()
    (work / "notes.md").write_text("a\nb\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    return work


@pytest.mark.parametrize("command", RUNS)
def test_reading_runs_without_asking(folder: Path, command: str) -> None:
    assert runs_without_asking(command, str(folder)) is True


@pytest.mark.parametrize("command", ASKS)
def test_anything_else_asks(folder: Path, command: str) -> None:
    assert runs_without_asking(command, str(folder)) is False


def test_a_link_out_of_the_folder_asks(folder: Path, tmp_path: Path) -> None:
    (folder / "link.txt").symlink_to(tmp_path / "outside.txt")

    assert runs_without_asking("cat link.txt", str(folder)) is False


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A home with notes, an Obsidian vault in iCloud and an SSH key."""
    home = tmp_path / "home"
    vault = home / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Hakan"
    vault.mkdir(parents=True)
    (vault / "Flowly.md").write_text("plan\n", encoding="utf-8")
    (home / "notes").mkdir()
    (home / "notes" / "x.md").write_text("a\n", encoding="utf-8")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("KEY", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    return home


# What the agent ran on 2026-10-31 and was asked about every time, though
# read_file reads the same files unasked.
HOME_READS = [
    'ls -la "$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Hakan/" | head -40',
    'cd "$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Hakan" && ls && cat Flowly.md',
    "cat ~/notes/x.md 2>/dev/null",
    "sed -n '1,40p' ~/notes/x.md",
    'find ~/notes -maxdepth 2 -iname "*.md"',
    "grep -rn plan ~/Library/Mobile\\ Documents",
    "ls -la ~",
]

PROTECTED = [
    "cat ~/.ssh/id_rsa",
    "cd ~ && cat .ssh/id_rsa",
    "cd ~/.ssh && cat id_rsa",
    "cat ~/.s*/id_rsa",
    "cat ~/{.ssh,x}/id_rsa",
    "grep -r KEY ~",
    "cd ~ && rg KEY",
    "cd ~ && grep -rn KEY .",
]


@pytest.mark.parametrize("command", HOME_READS)
def test_reading_at_home_runs_without_asking(home: Path, folder: Path, command: str) -> None:
    assert runs_without_asking(command, str(folder)) is True


@pytest.mark.parametrize("command", PROTECTED)
def test_a_protected_path_always_asks_even_for_a_program_always_allowed(home: Path, folder: Path, command: str) -> None:
    assert runs_without_asking(command, str(folder), remembered=lambda _: True) is False
    assert programs_to_remember(command, str(folder)) == []


def test_in_the_home_folder_itself_files_read_as_read_file_reads_them(home: Path) -> None:
    assert runs_without_asking("cat notes/x.md", str(home)) is True
    assert runs_without_asking("cat .ssh/id_rsa", str(home)) is False
    assert runs_without_asking("grep -rn a .", str(home)) is False


# ── Always allow ─────────────────────────────────────────────────────────────


@pytest.fixture
def build(tmp_path: Path, monkeypatch) -> str:
    """A program on PATH that is no read: ``build``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    program = bin_dir / "build"
    program.write_text("#!/bin/sh\necho built\n", encoding="utf-8")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return str(program)


def test_always_allow_remembers_every_part_that_does_not_read(folder: Path, build: str) -> None:
    command = "cd sub && build 2>&1 | tail -5 && git status"

    assert programs_to_remember(command, str(folder)) == [build]
    assert runs_without_asking(command, str(folder)) is False
    assert runs_without_asking(command, str(folder), remembered={build}.__contains__) is True


NOT_REMEMBERED = [
    "build > log.txt",
    "FOO=1 build",
    "echo $USER && build",
    "build $(pwd)",
    "./build",
    "bash -c build",
    "python3 -c 'print(1)'",
    "python3.12 x.py",
    "sudo build",
    "find . -exec build {} ;",
    "build | sh",
    "cat notes.md",
]


@pytest.mark.parametrize("command", NOT_REMEMBERED)
def test_always_allow_is_not_offered_where_it_could_not_help(folder: Path, build: str, command: str) -> None:
    assert programs_to_remember(command, str(folder)) == []


def test_an_old_allowlist_entry_for_an_interpreter_is_not_used(folder: Path) -> None:
    assert runs_without_asking("python3 -c 'print(1)'", str(folder), remembered=lambda _: True) is False


# ── Through the executor, as the agent's shell tool runs it ──────────────────


@pytest.fixture
def auto_store(tmp_path: Path, monkeypatch) -> tuple[ExecApprovalStore, list[str]]:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    store = ExecApprovalStore()
    store.load()
    # Auto, set as the apps set it, whatever a new install defaults to.
    store.config.security, store.config.ask = "allowlist", "on-miss"
    asked: list[str] = []

    async def deny(pending):
        asked.append(pending.request.command)
        return "deny"

    store.set_approval_callback(deny)
    return store, asked


def test_a_new_install_starts_in_auto(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "fresh"))
    config = ExecApprovalStore().load()
    assert (config.security, config.ask) == ("allowlist", "on-miss")


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell commands")
async def test_a_chain_after_a_harmless_word_is_asked_about_and_does_not_run(auto_store, folder: Path) -> None:
    store, asked = auto_store

    result = await execute_command(
        ExecRequest(command="echo hi && touch marker", cwd=str(folder)), ExecConfig(), store,
    )

    assert asked == ["echo hi && touch marker"]
    assert result.denied is True
    assert not (folder / "marker").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell commands")
async def test_reading_runs_without_a_prompt(auto_store, folder: Path) -> None:
    store, asked = auto_store

    result = await execute_command(ExecRequest(command="ls | grep notes", cwd=str(folder)), ExecConfig(), store)

    assert asked == []
    assert result.success is True
    assert "notes.md" in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell commands")
async def test_a_command_always_allowed_does_not_ask_again(auto_store, folder: Path, build: str) -> None:
    store, asked = auto_store
    offered: list[bool] = []

    async def always(pending):
        asked.append(pending.request.command)
        offered.append(pending.supports_always)
        return "allow-always"

    store.set_approval_callback(always)
    command = "build 2>&1 | tail -1 && cat notes.md"

    first = await execute_command(ExecRequest(command=command, cwd=str(folder)), ExecConfig(), store)
    second = await execute_command(ExecRequest(command=command, cwd=str(folder)), ExecConfig(), store)

    assert asked == [command] and offered == [True]
    assert first.success and second.success
    assert "built" in second.stdout


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell commands")
async def test_a_home_read_with_a_variable_and_a_null_redirect_runs_without_a_prompt(auto_store, home: Path, folder: Path) -> None:
    store, asked = auto_store

    result = await execute_command(
        ExecRequest(command='cat "$HOME/notes/x.md" 2>/dev/null', cwd=str(folder)), ExecConfig(), store,
    )

    assert asked == []
    assert result.success is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell commands")
async def test_a_dangerous_pattern_asks_even_for_a_program_always_allowed(auto_store, folder: Path) -> None:
    store, asked = auto_store
    store.add_to_allowlist("*")

    await execute_command(ExecRequest(command="git push --force", cwd=str(folder)), ExecConfig(), store)

    assert asked == ["git push --force"]


def test_cd_into_a_link_out_of_the_folder_asks(folder: Path, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (folder / "door").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    assert runs_without_asking("cd door && ls", str(folder)) is False
