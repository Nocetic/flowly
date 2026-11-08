"""The prompt names memory_append, so learning a fact never goes to the shell.

A fresh home, 2026-11-08: told to "append it to MEMORY.md" with the file's path
and an `exec date` hint, a model ran `mkdir -p ~/.flowly/workspace/memory`
through exec, and the owner had to approve a command to save their name.
"""

from flowly.agent.context import ContextBuilder


def _memory_section(tmp_path, monkeypatch, tools):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    prompt = ContextBuilder(tmp_path / "workspace").build_system_prompt(available_tools=set(tools))
    start = prompt.index("## Memory")
    end = prompt.find("\n## ", start + 1)
    return prompt[start:end if end != -1 else None]


def test_a_fact_is_saved_with_memory_append_not_a_command(tmp_path, monkeypatch):
    section = _memory_section(tmp_path, monkeypatch, {"exec", "write_file", "edit_file", "memory_append"})
    assert "call `memory_append`" in section
    assert "Never write memory files" in section
    assert "exec date" not in section
    assert "append it to MEMORY.md" not in section


def test_without_memory_append_the_file_route_stays(tmp_path, monkeypatch):
    section = _memory_section(tmp_path, monkeypatch, {"exec", "write_file"})
    assert "append it to MEMORY.md" in section
    assert "exec date +%F" in section
    assert "call `memory_append`" not in section  # (the tmp path carries the test name)
