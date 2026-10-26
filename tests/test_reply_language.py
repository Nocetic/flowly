"""The language an agent answers in, and the words it starts from.

A first "Selam" got "Selam! 👋 I'm Flowly, your personal AI assistant…" and an
English introduction (test account, 2026-10-27): the language rule sat
mid-prompt among English instructions, and the SOUL template scripted an
English getting-to-know-you in the brand's forbidden words.
"""

from pathlib import Path

from flowly.agent.context import REPLY_LANGUAGE_BLOCK, ContextBuilder

WORKSPACE = Path(__file__).resolve().parents[1] / "workspace"


def test_every_system_prompt_ends_with_the_reply_language_rule(tmp_path) -> None:
    (tmp_path / "memory").mkdir()
    builder = ContextBuilder(workspace=tmp_path, persona="default")
    for kwargs in ({}, {"voice_mode": True}, {"channel": "telegram"}):
        prompt = builder.build_system_prompt(skill_names=[], skip_memory=True, skip_context_files=True, **kwargs)
        assert prompt.endswith(REPLY_LANGUAGE_BLOCK), kwargs
    assert "Selam" in REPLY_LANGUAGE_BLOCK
    assert "never decides the language" in REPLY_LANGUAGE_BLOCK


def test_the_getting_to_know_you_offer_is_in_the_user_s_language(tmp_path) -> None:
    (tmp_path / "memory").mkdir()
    (tmp_path / "USER.md").write_text("<!-- ONBOARDING_PENDING -->\n", encoding="utf-8")
    prompt = ContextBuilder(workspace=tmp_path, persona="default").build_system_prompt(skill_names=[], skip_memory=True)
    assert "be more useful, in the language they write in." in " ".join(prompt.split())


def test_the_starting_soul_and_persona_speak_the_brand_and_script_no_introduction() -> None:
    soul = (WORKSPACE / "SOUL.md").read_text(encoding="utf-8")
    persona = (WORKSPACE / "personas" / "default.md").read_text(encoding="utf-8")
    for text in (soul, persona):
        # Brand voice: an agent you own, never "an AI assistant".
        assert "AI assistant" not in text
    # The system prompt owns the light, once-only offer; the SOUL no longer
    # scripts an English interview that overrides it.
    assert "First-Time Onboarding" not in soul
    assert "Ask the user's name" not in soul
