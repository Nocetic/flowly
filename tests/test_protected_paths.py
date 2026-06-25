"""Hardcoded-protected paths tests.

The protected list is the floor of agent permissions — these tests pin
behavior that *must not* drift: a future refactor accidentally letting
the agent read SSH keys would be a real security regression, so each
sensitive root gets explicit coverage.

Tests use ``monkeypatch`` to redirect ``Path.home()`` to a tmp dir so
they don't depend on the developer's actual ``~/.ssh`` etc. existing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from flowly.protected_paths import (
    find_protected_paths_in_command,
    is_protected_path,
)


# ── Test fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect Path.home() so we test against a sandboxed tree.

    Creates the protected layouts (``.ssh``, ``.aws``, etc.) inside the
    fake home so ``resolve()`` can find them.
    """
    fake = tmp_path / "home"
    fake.mkdir()
    # Pre-create the dirs we'll be checking against
    (fake / ".ssh").mkdir()
    (fake / ".aws").mkdir()
    (fake / ".kube").mkdir()
    (fake / ".gnupg").mkdir()
    (fake / ".docker").mkdir()
    (fake / ".flowly" / "credentials").mkdir(parents=True)
    (fake / ".flowly" / "sessions").mkdir(parents=True)
    (fake / "Library" / "Keychains").mkdir(parents=True)
    (fake / "Downloads").mkdir()
    (fake / "Desktop").mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    monkeypatch.setenv("HOME", str(fake))
    return fake


# ── is_protected_path ───────────────────────────────────────────────────


def test_ssh_directory_protected(fake_home: Path):
    assert is_protected_path(fake_home / ".ssh")
    assert is_protected_path(fake_home / ".ssh" / "id_rsa")
    assert is_protected_path(fake_home / ".ssh" / "config")


def test_aws_credentials_protected(fake_home: Path):
    assert is_protected_path(fake_home / ".aws")
    assert is_protected_path(fake_home / ".aws" / "credentials")
    assert is_protected_path(fake_home / ".aws" / "config")


def test_kube_protected(fake_home: Path):
    assert is_protected_path(fake_home / ".kube" / "config")


def test_gnupg_protected(fake_home: Path):
    assert is_protected_path(fake_home / ".gnupg" / "private-keys-v1.d")


def test_keychain_protected(fake_home: Path):
    assert is_protected_path(fake_home / "Library" / "Keychains" / "login.keychain-db")


def test_flowly_credentials_protected(fake_home: Path):
    assert is_protected_path(fake_home / ".flowly" / "credentials")
    assert is_protected_path(fake_home / ".flowly" / "credentials" / "api-keys.json")
    assert is_protected_path(fake_home / ".flowly" / "sessions")


def test_etc_shadow_protected():
    # System paths checked against absolute roots — fake_home doesn't
    # affect /etc/shadow comparison.
    assert is_protected_path(Path("/etc/shadow"))
    assert is_protected_path(Path("/etc/sudoers"))
    assert is_protected_path(Path("/etc/sudoers.d"))


def test_workspace_paths_NOT_protected(fake_home: Path):
    """Allowlisted user folders must never be flagged."""
    assert not is_protected_path(fake_home / "Downloads" / "report.md")
    assert not is_protected_path(fake_home / "Desktop" / "file.txt")
    assert not is_protected_path(fake_home / ".flowly" / "workspace" / "code.py")


def test_unrelated_paths_NOT_protected():
    assert not is_protected_path(Path("/tmp/foo"))
    assert not is_protected_path(Path("/usr/local/bin/something"))


def test_string_input_accepted(fake_home: Path):
    """Accepts a string for caller convenience."""
    assert is_protected_path(str(fake_home / ".ssh" / "id_rsa"))
    assert not is_protected_path(str(fake_home / "Downloads" / "ok.md"))


def test_tilde_expansion(fake_home: Path):
    """Untilde in ``~/.ssh`` → resolves to the protected root."""
    assert is_protected_path(Path("~/.ssh/id_rsa"))


def test_symlink_following(fake_home: Path, tmp_path: Path):
    """Symlink that points into a protected dir is caught.

    This is the TOCTOU-ish attack vector: write a file in Downloads,
    symlink it to ``~/.ssh/id_rsa``, ask the agent to read it. We
    resolve before checking, so the link target is what matters.
    """
    # Set up: a file inside Downloads that links to a protected file.
    target = fake_home / ".ssh" / "id_rsa"
    target.write_text("PRIVATE KEY", encoding="utf-8")
    link = fake_home / "Downloads" / "innocent.txt"
    os.symlink(target, link)

    # Linking via Downloads — but resolve() follows the symlink and
    # returns the protected path.
    assert is_protected_path(link)


def test_exact_match_against_root(fake_home: Path):
    """The root itself is also protected, not just descendants."""
    assert is_protected_path(fake_home / ".ssh")


def test_unresolvable_path_returns_false():
    """Garbage input doesn't claim protection — caller's other checks
    will reject it. We return False so we don't mask the real reason."""
    # Path with null byte — Python rejects on resolve()
    assert not is_protected_path(Path("\x00invalid"))


# ── find_protected_paths_in_command ─────────────────────────────────────


def test_command_reading_ssh_key_flagged(fake_home: Path):
    flagged = find_protected_paths_in_command("cat ~/.ssh/id_rsa")
    assert len(flagged) >= 1
    assert any(".ssh" in f for f in flagged)


def test_command_reading_aws_credentials_flagged(fake_home: Path):
    flagged = find_protected_paths_in_command("cat ~/.aws/credentials")
    assert len(flagged) >= 1


def test_command_redirect_to_etc_shadow_flagged():
    """``echo x > /etc/shadow`` — the redirect target gets flagged."""
    flagged = find_protected_paths_in_command("echo hacked > /etc/shadow")
    assert any("/etc/shadow" in f for f in flagged)


def test_command_with_2redirect_to_etc_flagged():
    """``cmd 2>/etc/sudoers`` — leading numeric redirect markers stripped."""
    flagged = find_protected_paths_in_command("touch foo 2>/etc/sudoers")
    assert any("sudoers" in f for f in flagged)


def test_safe_command_not_flagged(fake_home: Path):
    flagged = find_protected_paths_in_command("ls ~/Downloads")
    assert flagged == []


def test_command_without_path_args_not_flagged():
    flagged = find_protected_paths_in_command("ls -la")
    assert flagged == []


def test_command_with_relative_path_not_flagged():
    """``./foo/bar`` is fine — not in protected list."""
    flagged = find_protected_paths_in_command("cat ./project/notes.md")
    assert flagged == []


def test_unparseable_command_treated_suspiciously(fake_home: Path):
    """If shlex can't parse, fall back to whole-string match. We'd
    rather over-protect than under."""
    # Mismatched quotes — shlex raises. The fallback inspects the whole
    # string; if it contains a protected path substring (treated as a
    # path), it gets flagged.
    flagged = find_protected_paths_in_command("cat '~/.ssh/id_rsa")
    # Either it parsed somehow OR it fell back — both should flag.
    assert len(flagged) >= 1 or all(False for _ in [None])


def test_multiple_protected_paths_all_returned(fake_home: Path):
    flagged = find_protected_paths_in_command(
        "cp ~/.ssh/id_rsa ~/.aws/credentials"
    )
    assert len(flagged) >= 2


def test_protected_path_in_subshell_flagged(fake_home: Path):
    """Command substitution still has the path inside it as a token
    after shlex parsing."""
    flagged = find_protected_paths_in_command("echo $(cat ~/.ssh/id_rsa)")
    # shlex may or may not separate the inner token cleanly; we just
    # require *something* gets flagged in this clearly-malicious input.
    assert len(flagged) >= 1


def test_command_referring_to_keychain_flagged(fake_home: Path):
    flagged = find_protected_paths_in_command(
        f"cat '{fake_home}/Library/Keychains/login.keychain-db'"
    )
    assert any("Keychains" in f for f in flagged)


# ── Integration with exec safety ────────────────────────────────────────


def test_analyze_command_rejects_protected_path(fake_home: Path):
    """End-to-end: analyze_command returns ok=False when the cmd
    touches a protected path, regardless of any other policy."""
    from flowly.exec.safety import analyze_command

    result = analyze_command("cat ~/.ssh/id_rsa")
    assert result.ok is False
    assert "protected" in (result.reason or "").lower()


def test_analyze_command_allows_safe_command(fake_home: Path):
    from flowly.exec.safety import analyze_command

    result = analyze_command("ls ~/Downloads")
    assert result.ok is True


# ── Integration with filesystem tools ───────────────────────────────────


def test_filesystem_tool_blocks_protected_path(fake_home: Path):
    """end-to-end: WriteFileTool refuses to write inside ~/.ssh."""
    import asyncio
    from flowly.agent.tools.filesystem import WriteFileTool

    tool = WriteFileTool(workspace=fake_home / ".flowly" / "workspace")
    result = asyncio.run(tool.execute(
        path=str(fake_home / ".ssh" / "id_rsa_evil"),
        content="should not be written",
    ))
    assert "denied" in result.lower() or "outside workspace" in result.lower()
    # And the file does not exist.
    assert not (fake_home / ".ssh" / "id_rsa_evil").exists()


def test_filesystem_tool_allows_workspace(fake_home: Path):
    import asyncio
    from flowly.agent.tools.filesystem import WriteFileTool

    workspace = fake_home / ".flowly" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    tool = WriteFileTool(workspace=workspace)
    result = asyncio.run(tool.execute(
        path=str(workspace / "ok.txt"),
        content="hello",
    ))
    assert "Successfully wrote" in result
