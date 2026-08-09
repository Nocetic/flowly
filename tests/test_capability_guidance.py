"""Behaviour and cache contracts for JIT capability guidance."""

from __future__ import annotations

import json
from textwrap import dedent

import pytest

from flowly.agent.capability_guidance import route_capability_guidance
from flowly.agent.context import ContextBuilder
from flowly.agent.skills import _SKILLS_PROMPT_CACHE, SkillsLoader
from flowly.agent.tools.skill_view import SkillViewTool
from flowly.providers.anthropic_provider import _messages_to_anthropic
from flowly.providers.codex_responses_provider import _messages_to_codex_input
from flowly.providers.prompt_caching import apply_cache_control
from flowly.providers.xai_responses_provider import _messages_to_responses_input

UI_TOOLS = {"computer", "browser_tab", "skill_view"}


def _builder(tmp_path, monkeypatch) -> ContextBuilder:
    builder = ContextBuilder(workspace=tmp_path)
    monkeypatch.setattr(
        builder.skills,
        "get_always_skills",
        lambda: ["flowly-browser"],
    )

    def load(names):
        return "\n".join(
            "### Skill: flowly-browser\n\n# Flowly Browser Playbook"
            if name == "flowly-browser"
            else f"### Skill: {name}\n\n# Requested {name}"
            for name in names
        )

    monkeypatch.setattr(builder.skills, "load_skills_for_context", load)
    monkeypatch.setattr(
        builder.skills,
        "build_skills_summary",
        lambda **kwargs: "<skills><skill_index>flowly-browser</skill_index></skills>",
    )
    return builder


def test_greeting_omits_large_ui_playbooks(tmp_path, monkeypatch) -> None:
    builder = _builder(tmp_path, monkeypatch)

    messages = builder.build_messages(
        history=[],
        current_message="selam",
        skip_memory=True,
        skip_context_files=True,
        available_tools=UI_TOOLS,
        reachable_tools=UI_TOOLS,
    )

    joined = "\n".join(str(message.get("content", "")) for message in messages)
    assert "## Computer Use (Desktop Automation)" not in joined
    assert "# Flowly Browser Playbook" not in joined
    assert "## Browser Tab Control (Web Pages)" not in joined
    assert "<skill_index>flowly-browser</skill_index>" in joined


def test_plain_turkish_response_request_does_not_activate_ui_guides(
    tmp_path, monkeypatch,
) -> None:
    """Chat phrasing such as ``OK yaz`` means answer, not desktop typing."""

    builder = _builder(tmp_path, monkeypatch)

    messages = builder.build_messages(
        history=[],
        current_message="Yalnızca OK yaz. Herhangi bir araç kullanma.",
        skip_memory=True,
        skip_context_files=True,
        available_tools=UI_TOOLS,
        reachable_tools=UI_TOOLS,
    )

    dynamic = next(
        (
            message
            for message in messages
            if "_capability_guidance" in message
        ),
        None,
    )
    assert dynamic is None or dynamic["_capability_guidance"] == []
    joined = "\n".join(str(message.get("content", "")) for message in messages)
    assert "## Computer Use (Desktop Automation)" not in joined
    assert "# Flowly Browser Playbook" not in joined


def test_long_task_with_repeat_word_is_not_treated_as_ui_continuation(
    tmp_path, monkeypatch,
) -> None:
    """``tekrar`` inside a full task is not a context-free continuation."""

    builder = _builder(tmp_path, monkeypatch)

    messages = builder.build_messages(
        history=[],
        current_message=(
            "Context7 aracını kullanarak React dokümanını bul. "
            "Gereksiz tekrar arama yapma ve sonucu Türkçe özetle."
        ),
        skip_memory=True,
        skip_context_files=True,
        available_tools=UI_TOOLS,
        reachable_tools=UI_TOOLS,
    )

    dynamic = next(
        (
            message
            for message in messages
            if "_capability_guidance" in message
        ),
        None,
    )
    assert dynamic is None or dynamic["_capability_guidance"] == []


def test_explicit_computer_intent_auto_loads_guide_without_skill_call(
    tmp_path, monkeypatch,
) -> None:
    builder = _builder(tmp_path, monkeypatch)

    messages = builder.build_messages(
        history=[],
        current_message="Computer use ile masaüstündeki uygulamayı aç",
        skip_memory=True,
        skip_context_files=True,
        available_tools=UI_TOOLS,
        reachable_tools=UI_TOOLS,
    )

    dynamic = messages[1]
    assert dynamic["_capability_guidance"] == ["computer"]
    assert "## Computer Use (Desktop Automation)" in dynamic["content"]
    assert "# Flowly Browser Playbook" not in dynamic["content"]
    assert not any(
        message.get("role") == "assistant" and message.get("tool_calls")
        for message in messages
    )


def test_explicit_browser_intent_auto_loads_full_browser_playbook(
    tmp_path, monkeypatch,
) -> None:
    builder = _builder(tmp_path, monkeypatch)

    messages = builder.build_messages(
        history=[],
        current_message="Chrome'da yeni sekme aç ve Notion sayfasını düzenle",
        skip_memory=True,
        skip_context_files=True,
        available_tools=UI_TOOLS,
        reachable_tools=UI_TOOLS,
    )

    dynamic = messages[1]
    assert dynamic["_capability_guidance"] == ["browser"]
    assert "## Browser Tab Control (Web Pages)" in dynamic["content"]
    assert "# Flowly Browser Playbook" in dynamic["content"]
    assert "## Computer Use (Desktop Automation)" not in dynamic["content"]
    assert "## Computer Fallback Floor" in dynamic["content"]


def test_continuation_is_sticky_from_recent_tool_call(tmp_path, monkeypatch) -> None:
    builder = _builder(tmp_path, monkeypatch)
    history = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "browser_tab", "arguments": "{}"},
        }],
    }]

    messages = builder.build_messages(
        history=history,
        current_message="devam et",
        skip_memory=True,
        skip_context_files=True,
        available_tools=UI_TOOLS,
        reachable_tools=UI_TOOLS,
    )

    assert messages[1]["_capability_guidance"] == ["browser"]
    assert "# Flowly Browser Playbook" in messages[1]["content"]


def test_ambiguous_ui_action_fails_open_to_legacy_guides(
    tmp_path, monkeypatch,
) -> None:
    builder = _builder(tmp_path, monkeypatch)

    messages = builder.build_messages(
        history=[],
        current_message="bunu aç",
        skip_memory=True,
        skip_context_files=True,
        available_tools=UI_TOOLS,
        reachable_tools=UI_TOOLS,
    )

    dynamic = messages[1]
    assert dynamic["_capability_guidance"] == ["browser", "computer"]
    assert dynamic["_capability_guidance_fail_open"] is True
    assert "## Computer Use (Desktop Automation)" in dynamic["content"]
    assert "# Flowly Browser Playbook" in dynamic["content"]


def test_main_cacheable_prefix_is_identical_across_intents(
    tmp_path, monkeypatch,
) -> None:
    builder = _builder(tmp_path, monkeypatch)
    common = dict(
        history=[],
        skip_memory=True,
        skip_context_files=True,
        available_tools=UI_TOOLS,
        reachable_tools=UI_TOOLS,
        session_key="web:test",
    )

    greeting = builder.build_messages(current_message="selam", **common)
    browser = builder.build_messages(
        current_message="Chrome'da sekmeyi aç", **common,
    )

    assert greeting[0] == browser[0]
    cached = apply_cache_control(browser)
    assert isinstance(cached[0]["content"], list)
    assert cached[0]["content"][-1]["cache_control"]["type"] == "ephemeral"
    assert "cache_control" not in cached[1]


def test_dynamic_system_tail_survives_native_provider_conversions() -> None:
    messages = [
        {"role": "system", "content": "stable base"},
        {
            "role": "system",
            "content": "dynamic capability",
            "_capability_guidance": ["browser"],
        },
        {"role": "user", "content": "do it"},
    ]
    cached = apply_cache_control(messages)

    anthropic_system, anthropic_items = _messages_to_anthropic(cached)
    assert [block["text"] for block in anthropic_system] == [
        "stable base", "dynamic capability",
    ]
    assert "cache_control" in anthropic_system[0]
    assert "cache_control" not in anthropic_system[1]
    assert anthropic_items[0]["role"] == "user"

    codex_instructions, codex_items = _messages_to_codex_input(messages)
    assert codex_instructions == "stable base\n\ndynamic capability"
    assert codex_items[0]["role"] == "user"

    xai_instructions, xai_items = _messages_to_responses_input(messages)
    assert xai_instructions == "stable base\n\ndynamic capability"
    assert xai_items[0]["role"] == "user"


def test_disable_flag_restores_legacy_eager_prompt(tmp_path, monkeypatch) -> None:
    builder = _builder(tmp_path, monkeypatch)
    builder.set_capability_guidance(enabled=False)

    messages = builder.build_messages(
        history=[],
        current_message="selam",
        skip_memory=True,
        skip_context_files=True,
        available_tools=UI_TOOLS,
        reachable_tools=UI_TOOLS,
    )

    assert len(messages) == 2
    assert "## Computer Use (Desktop Automation)" in messages[0]["content"]
    assert "## Browser Tab Control (Web Pages)" in messages[0]["content"]
    assert "# Flowly Browser Playbook" in messages[0]["content"]


def test_router_does_not_confuse_web_research_with_visible_browser() -> None:
    decision = route_capability_guidance(
        "Webde araştır ve kaynakları özetle",
        history=[],
        available_tools=UI_TOOLS,
    )
    assert decision.active == frozenset()


@pytest.fixture
def many_skills(tmp_path, monkeypatch):
    flowly_home = tmp_path / "flowly-home"
    flowly_home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(flowly_home))
    workspace = tmp_path / "workspace"
    for index in range(90):
        name = f"skill-{index:03d}"
        description = f"Routine capability number {index} for ordinary work"
        if index == 89:
            name = "browser-automation"
            description = "Automate browser tabs, Sheets, Notion, and websites"
        skill_dir = workspace / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(dedent(f"""\
            ---
            name: {name}
            description: {description}
            ---
            # {name}
        """), encoding="utf-8")
    _SKILLS_PROMPT_CACHE.clear()
    yield workspace
    _SKILLS_PROMPT_CACHE.clear()


def test_budgeted_skill_catalog_keeps_all_names_and_prioritizes_intent(
    many_skills,
) -> None:
    loader = SkillsLoader(
        many_skills,
        builtin_skills_dir=many_skills / "no-builtins",
    )

    catalog = loader.build_skills_summary(
        available_tools={"skill_view"},
        max_chars=4_000,
        intent_text="edit a Notion page in the browser",
    )

    assert len(catalog) <= 4_000
    assert "truncated=\"true\"" not in catalog
    assert all(f"skill-{index:03d}" in catalog for index in range(89))
    assert "browser-automation" in catalog
    # A <name> node means this entry received scarce descriptive detail; names
    # in the compact index alone do not have one.
    assert "<name>browser-automation</name>" in catalog
    assert "Automate browser tabs" in catalog
    assert "<location>" not in catalog


@pytest.mark.asyncio
async def test_skill_view_search_is_lossless_catalog_fallback(many_skills) -> None:
    tool = SkillViewTool(many_skills)

    payload = json.loads(await tool.execute(
        action="search",
        query="browser automation",
    ))

    assert payload["total"] >= 1
    assert any(item["name"] == "browser-automation" for item in payload["skills"])
