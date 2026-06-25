from __future__ import annotations

from flowly.agent.learn_prompt import build_learn_prompt, parse_learn_args


def test_learn_prompt_uses_user_source() -> None:
    prompt = build_learn_prompt("local source: ~/work/sdk; URL: https://example.com/api")

    assert "The user invoked `/learn`" in prompt
    assert "local source: ~/work/sdk" in prompt
    assert "https://example.com/api" in prompt
    assert "skill_manage" in prompt
    assert "web_fetch" in prompt
    assert "Do not create a plugin" in prompt


def test_learn_prompt_empty_defaults_to_current_conversation() -> None:
    prompt = build_learn_prompt("")

    assert "this conversation" in prompt
    assert "recent steps" in prompt
    assert "SOURCE TO LEARN FROM" in prompt


def test_parse_learn_args_extracts_dry_run_flag() -> None:
    dry_run, source = parse_learn_args("--dry-run local source: ~/work/sdk")

    assert dry_run is True
    assert source == "local source: ~/work/sdk"


def test_parse_learn_args_preserves_source_without_flag() -> None:
    dry_run, source = parse_learn_args("notes from the current chat")

    assert dry_run is False
    assert source == "notes from the current chat"


def test_learn_prompt_dry_run_forbids_persistent_skill_changes() -> None:
    prompt = build_learn_prompt("local source: ~/work/sdk", dry_run=True)

    assert "The user invoked `/learn --dry-run`" in prompt
    assert "DRY RUN MODE" in prompt
    assert "MUST NOT call" in prompt
    assert 'skill_manage(action="create")' in prompt
    assert 'skill_manage(action="write_file")' in prompt
    assert "full SKILL.md draft" in prompt
