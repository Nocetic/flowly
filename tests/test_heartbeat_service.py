from __future__ import annotations

from pathlib import Path

import pytest

from flowly.heartbeat.service import (
    HEARTBEAT_OK_TOKEN,
    HeartbeatService,
    _build_heartbeat_prompt,
    _extract_heartbeat_tasks,
    _is_heartbeat_empty,
)


def test_bundled_heartbeat_template_is_empty() -> None:
    template = Path(__file__).resolve().parents[1] / "workspace" / "HEARTBEAT.md"
    content = template.read_text(encoding="utf-8")

    assert _extract_heartbeat_tasks(content) == ""
    assert _is_heartbeat_empty(content)


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "# Heartbeat Tasks\n",
        "<!-- comment only -->",
        "<!-- unterminated comment",
        "# Heartbeat\n\n- [ ]\n* [ ]\n",
        (
            "# Heartbeat\n\n"
            "Introductory template prose.\n\n"
            "## Active Tasks\n\n"
            "- [x] Already done\n\n"
            "## Completed\n\n"
            "- [x] Older task\n"
        ),
        (
            "# Heartbeat\n\n"
            "## Active Tasks\n\n"
            "### Priority\n\n"
            "## Completed\n\n"
            "- [x] Older task\n"
        ),
    ],
)
def test_non_actionable_heartbeat_content_is_empty(content: str | None) -> None:
    assert _is_heartbeat_empty(content)


def test_active_section_extracts_tasks_and_excludes_completed_items() -> None:
    content = """\
# Heartbeat Tasks

This prose describes the template and is not a task.

## Active Tasks

### Operations

- [ ] Check the nightly backup
- Review alerts
- [x] Already handled

## Completed

- [ ] This text must not run
"""

    tasks = _extract_heartbeat_tasks(content)

    assert "### Operations" in tasks
    assert "Check the nightly backup" in tasks
    assert "Review alerts" in tasks
    assert "Already handled" not in tasks
    assert "This text must not run" not in tasks
    assert not _is_heartbeat_empty(content)


def test_legacy_free_form_file_remains_actionable() -> None:
    content = """\
# Periodic instructions

Review the support inbox and report urgent messages.

## Completed

- [x] Historical task
"""

    tasks = _extract_heartbeat_tasks(content)

    assert tasks == "# Periodic instructions\n\nReview the support inbox and report urgent messages."
    assert not _is_heartbeat_empty(content)


def test_html_comments_are_removed_without_hiding_inline_tasks() -> None:
    content = """\
## Active Tasks

<!-- This explanation spans
multiple lines and is not a task. -->
- [ ] Check billing <!-- internal note -->
"""

    assert _extract_heartbeat_tasks(content) == "- [ ] Check billing"


def test_prompt_contains_tasks_without_asking_for_a_second_file_read() -> None:
    prompt = _build_heartbeat_prompt(
        deliver=True,
        tasks="- [ ] Check the nightly backup",
    )

    assert "Check the nightly backup" in prompt
    assert "Read HEARTBEAT.md" not in prompt
    assert "message tool" in prompt


async def test_tick_skips_llm_for_bundled_template(tmp_path: Path) -> None:
    template = Path(__file__).resolve().parents[1] / "workspace" / "HEARTBEAT.md"
    (tmp_path / "HEARTBEAT.md").write_bytes(template.read_bytes())
    prompts: list[str] = []

    async def on_heartbeat(prompt: str) -> str:
        prompts.append(prompt)
        return HEARTBEAT_OK_TOKEN

    service = HeartbeatService(workspace=tmp_path, on_heartbeat=on_heartbeat)

    await service._tick()

    assert prompts == []


async def test_tick_passes_active_tasks_directly_to_agent(tmp_path: Path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text(
        """\
# Heartbeat Tasks

## Active Tasks

- [ ] Verify the production backup

## Completed

- [x] Ignore this old task
""",
        encoding="utf-8",
    )
    prompts: list[str] = []

    async def on_heartbeat(prompt: str) -> str:
        prompts.append(prompt)
        return HEARTBEAT_OK_TOKEN

    service = HeartbeatService(workspace=tmp_path, on_heartbeat=on_heartbeat)

    await service._tick()

    assert len(prompts) == 1
    assert "Verify the production backup" in prompts[0]
    assert "Ignore this old task" not in prompts[0]
    assert "Read HEARTBEAT.md" not in prompts[0]


async def test_trigger_now_uses_same_empty_and_prompt_contract(tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "HEARTBEAT.md"
    heartbeat_file.write_text("# Heartbeat Tasks\n", encoding="utf-8")
    prompts: list[str] = []

    async def on_heartbeat(prompt: str) -> str:
        prompts.append(prompt)
        return "done"

    service = HeartbeatService(workspace=tmp_path, on_heartbeat=on_heartbeat)

    assert await service.trigger_now() == HEARTBEAT_OK_TOKEN
    assert prompts == []

    heartbeat_file.write_text(
        "## Active Tasks\n\nRun the health check.\n",
        encoding="utf-8",
    )

    assert await service.trigger_now() == "done"
    assert len(prompts) == 1
    assert "Run the health check." in prompts[0]
    assert "Read HEARTBEAT.md" not in prompts[0]
