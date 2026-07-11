"""Tests for the oversized-tool-result spill mechanism.

Covers the full chain that keeps long tool output (codex_session, exec, …)
recoverable instead of silently truncated:

  1. spill_tool_result writes the full output to the temp spill dir.
  2. _sanitize_tool_result appends a pointer the model can follow.
  3. read_file can access the spill dir (path policy) and supports
     offset/limit so the model can read sections without re-overflowing.
  4. CompactionService.microcompact preserves the pointer when it
     truncates old tool results.
"""

import asyncio

import pytest

from flowly.agent.tool_result_spill import (
    SPILL_POINTER_MARKER,
    build_spill_pointer,
    extract_spill_pointer,
    get_spill_dir,
    spill_tool_result,
)


# ── Layer 1: spill write ──────────────────────────────────────────────────

def test_spill_writes_full_content():
    content = "line\n" * 5000  # 25K chars
    path = spill_tool_result(content, "exec")
    assert path is not None
    assert path.parent == get_spill_dir()
    assert path.read_text(encoding="utf-8") == content
    path.unlink()


def test_pointer_roundtrip():
    content = "x" * 9000
    path = spill_tool_result(content, "codex_session")
    pointer = build_spill_pointer(path, len(content), 1)
    truncated = content[:8000] + pointer
    assert extract_spill_pointer(truncated) == pointer
    assert str(path) in pointer
    path.unlink()


def test_extract_pointer_absent():
    assert extract_spill_pointer("plain result, no spill") is None
    assert extract_spill_pointer("x" * 8000 + "\n[... truncated from 9000 chars]") is None


# ── Layer 2: sanitize appends pointer ─────────────────────────────────────

def test_sanitize_spills_and_points():
    from flowly.agent.loop import _sanitize_tool_result

    raw = "important detail\n" * 2000  # ~34K chars, over the 8K exec cap
    sanitized = _sanitize_tool_result(raw, "exec")
    assert len(sanitized) < len(raw)
    assert SPILL_POINTER_MARKER in sanitized
    pointer = extract_spill_pointer(sanitized)
    assert pointer is not None
    # The referenced file holds the FULL original output.
    spill_path = pointer.split(SPILL_POINTER_MARKER)[1].split(" — ")[0]
    from pathlib import Path
    assert Path(spill_path).read_text(encoding="utf-8") == raw
    Path(spill_path).unlink()


def test_sanitize_read_file_does_not_spill():
    """read_file results point back at offset/limit — no new file."""
    from flowly.agent.loop import _sanitize_tool_result

    raw = "z" * 20000
    sanitized = _sanitize_tool_result(raw, "read_file")
    assert SPILL_POINTER_MARKER not in sanitized
    assert "offset/limit" in sanitized


def test_sanitize_small_result_untouched():
    from flowly.agent.loop import _sanitize_tool_result

    raw = "small output"
    assert _sanitize_tool_result(raw, "exec") == raw


def test_sanitize_skill_view_not_truncated():
    """A whole skill body (~25 KB) must land intact — at the 8000 default it
    was truncated to a third + spilled, so the model reassembled it over dozens
    of reads and acted on partial guidance."""
    from flowly.agent.loop import _sanitize_tool_result

    raw = "skill guidance line\n" * 1300  # ~26 KB, over the old 8000 cap
    sanitized = _sanitize_tool_result(raw, "skill_view")
    assert sanitized == raw                      # untouched (under the 40000 cap)
    assert SPILL_POINTER_MARKER not in sanitized


# ── Layer 3: read_file path policy + offset/limit ─────────────────────────

def test_spill_dir_is_readable_by_policy(tmp_path):
    from flowly.agent.tools.filesystem import _is_path_allowed

    spill_file = get_spill_dir() / "exec-20260610-120000-abcd1234.txt"
    assert _is_path_allowed(spill_file.resolve(), workspace=tmp_path)
    # The rest of the temp dir stays off-limits.
    other_tmp = get_spill_dir().parent / "unrelated.txt"
    assert not _is_path_allowed(other_tmp.resolve(), workspace=tmp_path)


def test_read_file_offset_limit(tmp_path):
    from flowly.agent.tools.filesystem import ReadFileTool

    f = tmp_path / "big.txt"
    f.write_text("".join(f"line {i}\n" for i in range(1, 101)), encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)

    section = asyncio.run(tool.execute(str(f), offset=10, limit=3))
    assert "line 10" in section and "line 12" in section
    assert "line 13" not in section.split("[")[0]
    assert "continue with offset=13" in section

    # Reading through the end omits the continuation footer.
    tail = asyncio.run(tool.execute(str(f), offset=99, limit=50))
    assert "line 100" in tail
    assert "continue with offset" not in tail

    # String-typed args (model emission quirk) are coerced.
    coerced = asyncio.run(tool.execute(str(f), offset="5", limit="2"))
    assert "line 5" in coerced and "line 6" in coerced

    past_end = asyncio.run(tool.execute(str(f), offset=500))
    assert past_end.startswith("Error: offset 500 is past the end")


def test_read_file_without_pagination_unchanged(tmp_path):
    from flowly.agent.tools.filesystem import ReadFileTool

    f = tmp_path / "plain.txt"
    f.write_text("hello\nworld\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)
    assert asyncio.run(tool.execute(str(f))) == "hello\nworld\n"


# ── Layer 4: microcompact preserves the pointer ───────────────────────────

def test_microcompact_preserves_spill_pointer():
    from flowly.compaction.service import CompactionService
    from flowly.compaction.types import CompactionConfig

    class _StubProvider:
        provider_name = "flowly"

    config = CompactionConfig()
    service = CompactionService(provider=_StubProvider(), model="m", config=config)

    pointer = build_spill_pointer(
        get_spill_dir() / "codex_session-20260610-120000-abcd1234.txt", 50000, 900
    )
    big_result = ("detail " * 2000) + pointer
    keep_full = config.microcompact.keep_recent_full

    messages = [{"role": "user", "content": "q"}]
    messages.append({"role": "tool", "content": big_result})
    # Enough newer tool results that the spilled one gets truncated.
    for _ in range(keep_full + 1):
        messages.append({"role": "tool", "content": "recent ok"})

    compacted = service.microcompact(messages)
    old = compacted[1]["content"]
    assert len(old) < len(big_result)
    assert "[old tool output truncated]" in old
    assert extract_spill_pointer(old) == pointer


def test_microcompact_no_pointer_unchanged_behavior():
    from flowly.compaction.service import CompactionService
    from flowly.compaction.types import CompactionConfig

    class _StubProvider:
        provider_name = "flowly"

    config = CompactionConfig()
    service = CompactionService(provider=_StubProvider(), model="m", config=config)
    keep_full = config.microcompact.keep_recent_full

    messages = [{"role": "tool", "content": "x" * 5000}]
    for _ in range(keep_full + 1):
        messages.append({"role": "tool", "content": "recent"})

    compacted = service.microcompact(messages)
    assert compacted[0]["content"].endswith("[old tool output truncated]")


# ── Layer 5: codex-projected results pass through the same choke point ────

def test_codex_projected_result_spills():
    from flowly.agent.loop import _sanitize_codex_projected_result
    from pathlib import Path

    assistant = {
        "role": "assistant",
        "tool_calls": [{"function": {"name": "exec", "arguments": "{}"}}],
    }
    big = "codex inner output line\n" * 1500  # ~36K chars
    tool_msg = {"role": "tool", "content": big}

    sanitized = _sanitize_codex_projected_result(assistant, tool_msg)
    assert sanitized is not tool_msg
    assert len(sanitized["content"]) < len(big)
    assert SPILL_POINTER_MARKER in sanitized["content"]
    pointer = extract_spill_pointer(sanitized["content"])
    spill_path = Path(pointer.split(SPILL_POINTER_MARKER)[1].split(" — ")[0])
    # Spill filename identifies the codex inner tool.
    assert spill_path.name.startswith("codex_exec-")
    assert spill_path.read_text(encoding="utf-8") == big
    spill_path.unlink()


def test_codex_projected_small_result_untouched():
    from flowly.agent.loop import _sanitize_codex_projected_result

    assistant = {"role": "assistant", "tool_calls": [{"function": {"name": "exec"}}]}
    tool_msg = {"role": "tool", "content": "short"}
    assert _sanitize_codex_projected_result(assistant, tool_msg) is tool_msg


def test_codex_projected_non_string_content_untouched():
    from flowly.agent.loop import _sanitize_codex_projected_result

    assistant = {"role": "assistant", "tool_calls": []}
    tool_msg = {"role": "tool", "content": [{"type": "text", "text": "block"}]}
    assert _sanitize_codex_projected_result(assistant, tool_msg) is tool_msg


# ── Layer 6: late-turn messages survive mid-turn list rebinding ───────────
#
# Field bug (2026-06-10): _strip_old_tool_results (iteration > 5) and the
# mid-turn microcompact guard REBIND the loop's local ``messages`` to a new
# list. Persistence sliced the CALLER's original list, so anything appended
# after the rebinding — a codex_session call landing on iteration 6+ and all
# its projected pairs — was silently dropped from the session. Next turn the
# model truthfully claimed it never ran codex. The fix returns the loop's
# final list and persists from THAT.

def test_strip_old_tool_results_rebinding_loses_late_appends():
    """Documents WHY the loop must return its final list: the transform
    copies, so late appends never reach the original."""
    from flowly.agent.loop import _strip_old_tool_results

    original = [{"role": "tool", "content": f"r{i}" * 200} for i in range(5)]
    rebound = _strip_old_tool_results(original, keep_last=3, max_old_chars=50)
    assert rebound is not original  # copy happened (>3 tool results)

    rebound.append({"role": "assistant", "content": "codex call"})
    assert original[-1]["role"] == "tool"  # original never saw the append


def test_turn_persistence_uses_loop_final_list():
    """End-to-end recipe: persisting from the loop's RETURNED list keeps
    messages appended after a mid-turn rebinding."""
    from flowly.agent.loop import _strip_old_tool_results
    from flowly.session.manager import Session

    caller_messages = [{"role": "user", "content": "incele codex ile"}]
    turn_start_idx = len(caller_messages)

    # Loop simulation: early exec pairs append in place…
    messages = caller_messages
    for i in range(4):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "exec", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}",
                         "name": "exec", "content": "x" * 400})
    # …iteration 6: strip rebinds, then codex lands on the NEW list.
    messages = _strip_old_tool_results(messages, keep_last=3, max_old_chars=50)
    messages.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "cx", "type": "function",
         "function": {"name": "codex_session", "arguments": "{}"}}]})
    messages.append({"role": "tool", "tool_call_id": "cx",
                     "name": "codex_session", "content": '{"status": "ok"}'})

    s = Session(key="t")
    s.extend_with_turn_messages(
        user_content="incele codex ile",
        new_messages=messages[turn_start_idx:],  # the loop's FINAL list
        final_content="done",
    )
    persisted_tools = [m.get("name") for m in s.messages if m.get("role") == "tool"]
    assert "codex_session" in persisted_tools  # the regression
    assert persisted_tools.count("exec") == 4


# ── Layer 7: codex projected pairs are attributed via codex_ prefix ───────

def test_relabel_codex_pair_prefixes_tool_names():
    from flowly.agent.loop import _relabel_codex_projected_pair

    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "exec", "arguments": '{"command":"ls"}'},
        }],
    }
    tool_msg = {"role": "tool", "tool_call_id": "c1", "name": "exec", "content": "out"}

    new_call, new_result = _relabel_codex_projected_pair(assistant, tool_msg)
    assert new_call["tool_calls"][0]["function"]["name"] == "codex_exec"
    assert new_call["tool_calls"][0]["id"] == "c1"  # linkage intact
    assert new_call["tool_calls"][0]["function"]["arguments"] == '{"command":"ls"}'
    assert new_result["name"] == "codex_exec"
    assert new_result["tool_call_id"] == "c1"
    # Originals untouched (shallow-copy semantics).
    assert assistant["tool_calls"][0]["function"]["name"] == "exec"
    assert tool_msg["name"] == "exec"


def test_relabel_codex_pair_idempotent_and_defensive():
    from flowly.agent.loop import _relabel_codex_projected_pair

    already = {
        "role": "assistant",
        "tool_calls": [{"id": "c2", "function": {"name": "codex_exec"}}],
    }
    result = {"role": "tool", "name": "codex_exec", "content": "x"}
    new_call, new_result = _relabel_codex_projected_pair(already, result)
    assert new_call["tool_calls"][0]["function"]["name"] == "codex_exec"
    assert new_result["name"] == "codex_exec"

    # Malformed shapes pass through without raising.
    weird_call = {"role": "assistant", "tool_calls": "not-a-list"}
    weird_result = {"role": "tool", "content": "x"}  # no name field
    out_call, out_result = _relabel_codex_projected_pair(weird_call, weird_result)
    assert out_call["tool_calls"] == "not-a-list"
    assert "name" not in out_result


def test_sanitize_after_relabel_no_double_prefix():
    """Spill filename derives from the relabeled name without codex_codex_."""
    from flowly.agent.loop import (
        _relabel_codex_projected_pair,
        _sanitize_codex_projected_result,
    )
    from pathlib import Path

    assistant = {
        "role": "assistant",
        "tool_calls": [{"id": "c3", "type": "function",
                        "function": {"name": "exec", "arguments": "{}"}}],
    }
    big = "line\n" * 3000  # 15K chars, over the 8K default cap
    tool_msg = {"role": "tool", "tool_call_id": "c3", "name": "exec", "content": big}

    call2, result2 = _relabel_codex_projected_pair(assistant, tool_msg)
    sanitized = _sanitize_codex_projected_result(call2, result2)
    pointer = extract_spill_pointer(sanitized["content"])
    assert pointer is not None
    spill_path = Path(pointer.split(SPILL_POINTER_MARKER)[1].split(" — ")[0])
    assert spill_path.name.startswith("codex_exec-")
    assert "codex_codex" not in spill_path.name
    spill_path.unlink()


# ── Layer 8: completion-claim guard is action-turn only ───────────────────

def test_completion_claim_guard_gated_on_action_turn():
    """The forced-tool retry block must require action_turn — reporting
    answers like "şunları yaptım" on Q&A turns must not be discarded."""
    import inspect
    import re as _re
    import flowly.agent.loop as loop_mod

    src = inspect.getsource(loop_mod.AgentLoop._run_llm_tool_loop)
    guard = _re.search(
        r"if \(\s*action_turn\s*and not successful_tools_were_used"
        r"\s*and response\.content"
        r"\s*and self\._contains_unverified_completion_claim",
        src,
    )
    assert guard, "completion-claim guard must be gated on action_turn"
