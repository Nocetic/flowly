"""Tests for the P3.1 / P3.2 prompt blocks.

The two modules we're verifying:

  * ``detect_platform`` → correct OsKind + live shell detection
  * ``build_platform_block`` → OS-specific cheatsheet (Windows, macOS,
    Linux, other) with no cross-OS leakage
  * ``build_discipline_block`` → four-block composition, semantic
    XML tags preserved

We deliberately avoid mocking ``platform.system()`` — instead we build
a ``PlatformInfo`` directly for render tests so the OS output is
deterministic on any dev machine. Detection-specific tests use a real
system call but only assert on the detected kind matching the local
OS, because that's the only invariant we can rely on.
"""

from __future__ import annotations

import platform
from unittest.mock import patch

import pytest

from flowly.agent.prompt_blocks import (
    ACT_DONT_ASK_BLOCK,
    CHINESE_OPENWEIGHT_LANGUAGE_BLOCK,
    GOOGLE_OPERATIONAL_BLOCK,
    MANDATORY_TOOL_USE_BLOCK,
    MISSING_CONTEXT_BLOCK,
    OPENAI_EXECUTION_EXTRAS_BLOCK,
    PLATFORM_HINTS,
    TOOL_USE_ENFORCEMENT_BLOCK,
    VOICE_MODE_BLOCK,
    PlatformInfo,
    build_discipline_block,
    build_model_family_block,
    build_platform_block,
    build_platform_hint,
    build_voice_mode_block,
    detect_model_families,
    detect_platform,
)


# ---------------------------------------------------------------------------
# detect_platform
# ---------------------------------------------------------------------------


class TestDetectPlatform:
    def test_matches_local_system(self) -> None:
        # The one invariant we can test cheaply: the detector agrees
        # with `platform.system()` on the three majors.
        info = detect_platform()
        sys_name = platform.system()
        if sys_name == "Darwin":
            assert info.kind == "macos"
        elif sys_name == "Windows":
            assert info.kind == "windows"
        elif sys_name == "Linux":
            assert info.kind == "linux"
        else:
            assert info.kind == "other"

    def test_label_non_empty(self) -> None:
        info = detect_platform()
        assert info.label.strip()

    def test_shell_non_empty(self) -> None:
        info = detect_platform()
        assert info.shell.strip()

    @patch("flowly.agent.prompt_blocks.platform.system", return_value="Windows")
    @patch("flowly.agent.prompt_blocks.platform.release", return_value="10.0.26100")
    @patch("flowly.agent.prompt_blocks.platform.machine", return_value="AMD64")
    def test_windows_detection(self, _m: object, _r: object, _s: object) -> None:
        info = detect_platform()
        assert info.kind == "windows"
        assert "Windows" in info.label

    @patch("flowly.agent.prompt_blocks.platform.system", return_value="Darwin")
    @patch("flowly.agent.prompt_blocks.platform.release", return_value="24.1.0")
    @patch("flowly.agent.prompt_blocks.platform.machine", return_value="arm64")
    def test_macos_detection(self, _m: object, _r: object, _s: object) -> None:
        info = detect_platform()
        assert info.kind == "macos"
        assert "macOS" in info.label or "Darwin" in info.label


# ---------------------------------------------------------------------------
# build_platform_block — per-OS render tests
# ---------------------------------------------------------------------------


def _info(kind: str, **kwargs: object) -> PlatformInfo:
    """Factory so each test doesn't repeat the keyword list."""
    defaults = dict(
        kind=kind,
        label=f"{kind}-label",
        shell=f"{kind}-shell",
        is_wsl=False,
        is_termux=False,
        is_docker=False,
    )
    defaults.update(kwargs)
    return PlatformInfo(**defaults)  # type: ignore[arg-type]


class TestPlatformBlockWindows:
    def setup_method(self) -> None:
        self.out = build_platform_block(_info("windows"))

    def test_runtime_header_present(self) -> None:
        assert "Runtime: windows-label" in self.out
        assert "`windows-shell`" in self.out

    def test_windows_specific_guidance(self) -> None:
        assert "dir /B" in self.out
        assert "start \"\"" in self.out
        assert "%USERPROFILE%" in self.out
        assert "explorer.exe" in self.out

    def test_rejects_unix_commands(self) -> None:
        # The block explicitly warns against Unix commands leaking
        # into Windows execution.
        assert "NOT `ls`" in self.out
        assert "open -a" in self.out  # mentioned as explicit negative
        assert "xdg-open" in self.out  # same
        # And the "NEVER use" line tells the model to stop.
        assert "NEVER use `open -a`" in self.out


class TestPlatformBlockMacos:
    def setup_method(self) -> None:
        self.out = build_platform_block(_info("macos"))

    def test_runtime_header_present(self) -> None:
        assert "Runtime: macos-label" in self.out

    def test_macos_specific_guidance(self) -> None:
        assert "open -a 'Google Chrome'" in self.out
        assert "~/Desktop" in self.out
        assert "$HOME" in self.out

    def test_does_not_teach_windows_commands(self) -> None:
        # macOS block must not tell the model about cmd.exe.
        assert "cmd.exe" not in self.out
        assert "%USERPROFILE%" not in self.out
        assert "start \"\"" not in self.out

    def test_does_not_teach_linux_xdg_as_primary(self) -> None:
        # macOS block may MENTION xdg-open in a "NOT xdg-open" warning,
        # but must not teach it as a usable command (no `xdg-open <file>`
        # or similar use-pattern).
        assert "xdg-open <" not in self.out
        assert "use xdg-open" not in self.out.lower()
        # The presence of `open -a` as the recommended command is the
        # positive signal that macOS gets macOS-native guidance.
        assert "open -a" in self.out


class TestPlatformBlockLinux:
    def setup_method(self) -> None:
        self.out = build_platform_block(_info("linux"))

    def test_runtime_header_present(self) -> None:
        assert "Runtime: linux-label" in self.out

    def test_linux_specific_guidance(self) -> None:
        assert "xdg-open" in self.out
        assert "$XDG_DESKTOP_DIR" in self.out
        assert "$HOME" in self.out

    def test_does_not_teach_macos_open(self) -> None:
        # The exact string `open -a` is macOS-only and must not appear
        # in the Linux block (Linux users would type `open` and get
        # nothing).
        assert "open -a" not in self.out

    def test_does_not_teach_windows_commands(self) -> None:
        assert "cmd.exe" not in self.out
        assert "%USERPROFILE%" not in self.out


class TestPlatformBlockExtras:
    def test_wsl_adds_mnt_c_hint(self) -> None:
        out = build_platform_block(_info("linux", is_wsl=True))
        assert "WSL detected" in out
        assert "/mnt/c/" in out

    def test_termux_hint(self) -> None:
        out = build_platform_block(_info("linux", is_termux=True))
        assert "Termux" in out

    def test_docker_hint(self) -> None:
        out = build_platform_block(_info("linux", is_docker=True))
        assert "Docker" in out

    def test_no_extras_on_vanilla_linux(self) -> None:
        out = build_platform_block(_info("linux"))
        assert "WSL detected" not in out
        assert "Termux" not in out
        assert "Docker container detected" not in out

    def test_other_os_falls_back_safely(self) -> None:
        out = build_platform_block(_info("other", label="Plan9 4th"))
        assert "Plan9 4th" in out
        assert "Unknown" in out


# ---------------------------------------------------------------------------
# Discipline block
# ---------------------------------------------------------------------------


class TestDisciplineBlock:
    def setup_method(self) -> None:
        self.out = build_discipline_block()

    def test_all_four_sub_blocks_present(self) -> None:
        # act_dont_ask joined the block set when we deduped the
        # enforcement rules — previously duplicated in context.py's
        # identity header (see build_discipline_block docstring for
        # the dedupe rationale).
        assert "Tool-use enforcement" in self.out
        assert "<mandatory_tool_use>" in self.out
        assert "</mandatory_tool_use>" in self.out
        assert "<missing_context>" in self.out
        assert "</missing_context>" in self.out
        assert "<act_dont_ask>" in self.out
        assert "</act_dont_ask>" in self.out

    def test_core_enforcement_wording(self) -> None:
        # Canonical must-use-tools directive. If someone weakens this
        # wording (e.g. "should" instead of "MUST"), the test catches it.
        assert "You MUST use your tools to take action" in self.out
        assert "Never end your turn with a promise of future action" in self.out

    def test_mandatory_tool_use_covers_key_categories(self) -> None:
        # The whole point of this block is to ground answers the model
        # likes to fabricate. Verify the high-frequency fabrication
        # targets are listed.
        assert "Current time" in self.out
        assert "System state" in self.out
        assert "File contents" in self.out
        assert "web_search" in self.out
        assert "list_dir" in self.out

    def test_profile_vs_system_distinction(self) -> None:
        # The single most important sentence against cross-environment
        # hallucination — profile ≠ live runtime.
        assert "describe the USER, not the system" in self.out

    def test_missing_context_forbids_guessing(self) -> None:
        assert "do NOT guess or hallucinate" in self.out
        # The "label assumptions" directive may wrap across lines;
        # just confirm both halves are present.
        assert "label assumptions" in self.out

    def test_blocks_are_ordered(self) -> None:
        # Order matters for model attention: enforcement first, then
        # the specific grounding list, then the "what to do when stuck",
        # then the "act instead of asking" disambiguator.
        enforcement_idx = self.out.find("Tool-use enforcement")
        mandatory_idx = self.out.find("<mandatory_tool_use>")
        missing_idx = self.out.find("<missing_context>")
        act_idx = self.out.find("<act_dont_ask>")
        assert 0 <= enforcement_idx < mandatory_idx < missing_idx < act_idx

    def test_act_dont_ask_covers_default_interpretation(self) -> None:
        # The whole point of this block is that trivial-ambiguity
        # questions shouldn't block on a clarification round-trip.
        # Phrase wraps across a line in the source — normalise before check.
        assert "act on it immediately" in " ".join(ACT_DONT_ASK_BLOCK.split())
        # Canonical examples — if a future edit drops them the block
        # loses most of its teaching value.
        assert "What time is it?" in ACT_DONT_ASK_BLOCK
        assert "port 443" in ACT_DONT_ASK_BLOCK

    def test_individual_constants_exposed(self) -> None:
        # The four constants are re-exported so future callers (e.g.
        # a subagent builder) can pick and choose instead of taking
        # the whole block set.
        assert "Tool-use enforcement" in TOOL_USE_ENFORCEMENT_BLOCK
        assert "<mandatory_tool_use>" in MANDATORY_TOOL_USE_BLOCK
        assert "<missing_context>" in MISSING_CONTEXT_BLOCK
        assert "<act_dont_ask>" in ACT_DONT_ASK_BLOCK


# ---------------------------------------------------------------------------
# Voice mode block (P4)
# ---------------------------------------------------------------------------


class TestVoiceModeBlock:
    """The voice-mode prompt is injected in place of the Tool Call Style
    block when a turn arrives with ``voiceMode: true``. These tests pin
    down the non-negotiable directives so a future wording tweak can't
    silently drop them."""

    def setup_method(self) -> None:
        self.out = build_voice_mode_block()

    def test_accessor_matches_constant(self) -> None:
        # The function is a thin accessor — sanity check.
        assert build_voice_mode_block() == VOICE_MODE_BLOCK

    def test_declares_tts_context(self) -> None:
        # The opening line must make it unambiguous: this reply is
        # going to speech. If that's unclear, the model falls back to
        # its markdown-heavy default.
        assert "TTS" in self.out or "text-to-speech" in self.out.lower()
        assert "speech" in self.out.lower() or "spoken" in self.out.lower()

    def test_forbids_markdown(self) -> None:
        # Must name the markdown offenders the model actually produces.
        assert "markdown" in self.out.lower()
        for anti in ("**bold**", "headers"):
            assert anti in self.out

    def test_forbids_emoji(self) -> None:
        assert "emoji" in self.out.lower()
        assert "NO emoji" in self.out

    def test_forbids_bare_urls(self) -> None:
        # The voice block must push "say link attached" not "read URL".
        assert "URL" in self.out or "url" in self.out.lower()
        assert "link" in self.out.lower()

    def test_mentions_length_target(self) -> None:
        # Hard cap so the model doesn't narrate for a minute+.
        # Tightened from 150 → 60 words after user reported voice replies
        # were still too long (flowly-main, 2026-04-23).
        assert "60 words" in self.out.lower() or "60 word" in self.out.lower()

    def test_tool_calls_silent(self) -> None:
        # Voice mode must enforce silent tool-call discipline: calls
        # go out WITHOUT a preamble in any language. Earlier revisions
        # hardcoded Turkish phrases as examples; we removed all language-
        # specific strings to make the rule i18n-safe.
        # Collapse whitespace so line wraps don't break substring checks.
        lower = " ".join(self.out.lower().split())
        # Must name preamble anti-patterns in English (language-agnostic
        # example) and explicitly generalise to other languages.
        assert "let me check" in lower
        assert "any language" in lower
        # Must state the silent-tool-call rule outright.
        assert "without any preamble" in lower or "no preamble" in lower

    def test_language_match_directive(self) -> None:
        # Must tell the model to reply in whatever language the user spoke,
        # without naming any specific language (no hardcoded "Turkish in,
        # Turkish out" style — that would privilege one locale).
        lower = self.out.lower()
        assert "language" in lower
        assert "match" in lower or "same language" in lower
        # Guard against re-introducing hardcoded locale tokens.
        for banned in ("turkish in, turkish out", "bir saniye bakıyorum"):
            assert banned not in lower, f"hardcoded locale token re-appeared: {banned}"


# ---------------------------------------------------------------------------
# ContextBuilder integration
# ---------------------------------------------------------------------------


class TestContextBuilderIntegration:
    """Verify the new blocks actually land in the final system prompt.

    ``ContextBuilder.build_system_prompt`` is a large method with many
    branches (memory, skills, personas, bootstrap files). Rather than
    mock every branch, we give it a minimal real workspace and assert
    that the three high-value strings make it through.
    """

    @pytest.fixture
    def builder(self, tmp_path) -> "ContextBuilder":  # noqa: F821
        from flowly.agent.context import ContextBuilder
        (tmp_path / "memory").mkdir()
        return ContextBuilder(workspace=tmp_path, persona="default")

    def test_discipline_block_present_in_final_prompt(self, builder) -> None:
        prompt = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
        )
        # One landmark per sub-block — if any of them is missing we've
        # broken the wiring in context.py.
        assert "Tool-use enforcement" in prompt
        assert "<mandatory_tool_use>" in prompt
        assert "<missing_context>" in prompt

    def test_platform_block_present_in_final_prompt(self, builder) -> None:
        prompt = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
        )
        # Runtime header is emitted regardless of OS — easy sentinel.
        assert "Runtime:" in prompt
        # And the per-OS section header appears.
        assert "## Platform —" in prompt

    def test_system_prompt_does_not_auto_create_skills(self, builder) -> None:
        prompt = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
        )
        assert "## Skills (skill_manage tool)" not in prompt
        assert "skill_manage(action='create')" not in prompt
        assert "reusable skills" not in prompt.lower()
        assert "complex task (5+ tool calls)" not in prompt

    def test_skill_manage_tool_description_is_explicit_only(self) -> None:
        from flowly.agent.tools.skill_manage import SkillManageTool

        description = SkillManageTool().description.lower()
        assert "explicitly asks" in description
        assert "create when" not in description
        assert "5+ tool" not in description
        assert "patch when" not in description

    def test_platform_block_matches_live_os(self, builder) -> None:
        # Sanity: the section header matches the actual OS so we know
        # the detector result actually feeds the prompt.
        prompt = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
        )
        sys_name = platform.system()
        if sys_name == "Darwin":
            assert "## Platform — macOS" in prompt
        elif sys_name == "Windows":
            assert "## Platform — Windows" in prompt
        elif sys_name == "Linux":
            assert "## Platform — Linux" in prompt

    def test_voice_mode_false_emits_agency_block(self, builder) -> None:
        # With voice_mode=False the universal agency block carries the
        # "act, then report" discipline in principle form (it replaced the
        # old prohibition-heavy "## Tool Usage Style" section). It ships to
        # every model; the strict enforcement is layered on only for weaker
        # families.
        prompt = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            voice_mode=False,
        )
        assert "# How you work" in prompt
        assert "Act, then report" in prompt
        assert "## Tool Usage Style" not in prompt  # old block is gone
        assert "# Voice Mode" not in prompt

    def test_voice_mode_true_swaps_in_voice_block(self, builder) -> None:
        # With voice_mode=True VOICE_MODE_BLOCK is appended. The
        # identity-level "## Tool Usage Style" still ships (it's part
        # of the always-on identity header) but VOICE_MODE_BLOCK sits
        # below it and carries the TTS-specific rules.
        prompt = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            voice_mode=True,
        )
        assert "# Voice Mode" in prompt
        # And the core TTS-forbidden patterns are named in the prompt.
        assert "no markdown" in prompt.lower() or "NO markdown" in prompt
        assert "NO emoji" in prompt

    def test_voice_mode_default_is_false(self, builder) -> None:
        # Every existing caller that doesn't pass voice_mode gets the
        # text-mode prompt. This is the backward-compat guarantee.
        prompt_default = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
        )
        prompt_explicit = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            voice_mode=False,
        )
        # Same shape — "voice_mode=False" is indistinguishable from
        # "didn't pass voice_mode at all".
        assert prompt_default == prompt_explicit

    def test_prompt_is_cache_stable_across_turns(self, builder) -> None:
        # Regression test: the prompt used to embed a live
        # ``datetime.now()`` timestamp in the session-metadata footer
        # and in the memory-section "today:" line, which made the
        # system prompt byte-different on every build — the Anthropic
        # prompt cache could never reuse the prefix. We removed
        # the dynamic clock from the prompt entirely and rely on
        # ``exec date`` when the model needs a real timestamp.
        # This test pins that choice so no future edit
        # quietly reintroduces a live clock.
        import time
        p1 = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
        )
        time.sleep(1.1)  # cross a clock second
        p2 = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
        )
        assert p1 == p2, (
            "system prompt drifted across turns — something is rendering "
            "datetime.now() live into the prompt again"
        )
        # Defensive: the old live-timestamp footer should not reappear.
        assert "Conversation time:" not in p1


# ---------------------------------------------------------------------------
# Model-family aware guidance
# ---------------------------------------------------------------------------
#
# These tests pin two things:
#
#   1. Every model id in the user-facing OpenRouter allowlist (mirrored
#      from ``flowly-app/lib/plans/allowlist.ts``) maps to a known
#      family. A new model added on the web side that we never sync
#      here shows up as an orphan and fails the test — that's the
#      catch we want: silent fallback to "no family block" can ship a
#      Chinese open-weight model with no language-discipline block.
#
#   2. The dispatcher returns the right block per family, and returns
#      an empty string for Claude and xAI (their baseline is enough).
#
# We avoid a goldmaster snapshot of the full guidance text — the wording
# is tuned per failure mode and will keep evolving. Instead we assert
# on the XML tags / heading anchors that the dispatcher contract
# guarantees.


# Mirror of the OpenRouter ids in flowly-app/lib/plans/allowlist.ts.
# Keep this list in sync when the web-side allowlist gains a new model
# — the assertions below catch any model that lands in production
# without a corresponding family entry in _MODEL_FAMILY_PATTERNS.
_PRODUCTION_ALLOWLIST_MODELS: tuple[str, ...] = (
    # STARTER (free)
    "z-ai/glm-5",
    "moonshotai/kimi-k2.6",
    "qwen/qwen3.5-397b-a17b",
    "qwen/qwen3.5-plus-20260420",
    "anthropic/claude-haiku-4.5",
    "minimax/minimax-m2.7",
    "minimax/minimax-m2.5",
    "x-ai/grok-4.3",
    "deepseek/deepseek-v4-pro",
    "xiaomi/mimo-v2.5",
    # STARTER_PLUS adds
    "google/gemini-3.1-flash-lite",
    # PRO adds
    "openai/gpt-5.5",
    "anthropic/claude-sonnet-4.6",
)

# Expected family for each model in the production allowlist. The
# dispatcher returns frozenset so a model could match multiple
# families in principle — but the current catalogue has 1:1 mapping.
_EXPECTED_FAMILY: dict[str, str] = {
    "z-ai/glm-5":                    "chinese",
    "moonshotai/kimi-k2.6":          "chinese",
    "qwen/qwen3.5-397b-a17b":        "chinese",
    "qwen/qwen3.5-plus-20260420":    "chinese",
    "anthropic/claude-haiku-4.5":    "claude",
    "minimax/minimax-m2.7":          "chinese",
    "minimax/minimax-m2.5":          "chinese",
    "x-ai/grok-4.3":                 "xai",
    "deepseek/deepseek-v4-pro":      "chinese",
    "xiaomi/mimo-v2.5":              "chinese",
    "google/gemini-3.1-flash-lite":  "google",
    "openai/gpt-5.5":                "openai",
    "anthropic/claude-sonnet-4.6":   "claude",
}


class TestDetectModelFamily:
    def test_every_allowlist_model_has_a_family(self) -> None:
        """No production model should fall through to the empty set.

        If this fails, ``flowly-app/lib/plans/allowlist.ts`` has a new
        model that ``_MODEL_FAMILY_PATTERNS`` doesn't know about —
        the agent will ship that model with no family-specific
        guidance, which silently regresses behaviour (most common
        failure mode: new Chinese open-weight model loses the
        language-discipline block).
        """
        orphans = [
            m for m in _PRODUCTION_ALLOWLIST_MODELS
            if not detect_model_families(m)
        ]
        assert not orphans, (
            "models with no family — extend _MODEL_FAMILY_PATTERNS: "
            f"{orphans}"
        )

    @pytest.mark.parametrize(
        "model,expected", list(_EXPECTED_FAMILY.items())
    )
    def test_each_model_maps_to_expected_family(
        self, model: str, expected: str,
    ) -> None:
        families = detect_model_families(model)
        assert expected in families, (
            f"{model}: expected family {expected!r}, got {sorted(families)}"
        )

    def test_unknown_model_returns_empty(self) -> None:
        # A provider we don't know about should return empty rather
        # than guess — guessing wrong would inject a stale block.
        assert detect_model_families("acme/some-model-1.0") == frozenset()

    def test_none_returns_empty(self) -> None:
        # Callers pass model=None for cron builds where the model is
        # resolved later; detection must tolerate this without raising.
        assert detect_model_families(None) == frozenset()

    def test_empty_string_returns_empty(self) -> None:
        assert detect_model_families("") == frozenset()

    def test_case_insensitive(self) -> None:
        # Some clients upper-case model ids before sending; the
        # detector lowercases internally.
        assert "openai" in detect_model_families("OpenAI/GPT-5.5")
        assert "claude" in detect_model_families("Anthropic/Claude-Sonnet-4.6")

    def test_short_alias_matches(self) -> None:
        # ``self.model`` may be a bare short name without the provider
        # prefix when set via the CLI setup wizard's "custom" option.
        assert "claude" in detect_model_families("claude-haiku-4.5")
        assert "openai" in detect_model_families("gpt-5.5")
        assert "google" in detect_model_families("gemini-3.1-pro")
        assert "chinese" in detect_model_families("kimi-k2.6")


class TestBuildModelFamilyBlock:
    """Dispatcher contract: empty for Claude/xAI/None, the right block for others."""

    def test_claude_returns_empty(self) -> None:
        # Claude follows the discipline baseline; extra rules cause
        # oscillation in earlier tuning runs. Empty by design.
        assert build_model_family_block("anthropic/claude-haiku-4.5") == ""
        assert build_model_family_block("anthropic/claude-sonnet-4.6") == ""

    def test_xai_returns_empty(self) -> None:
        # Grok has no failure mode that the baseline doesn't catch.
        # Recorded as a deliberate decision in build_model_family_block's
        # docstring; pinned here so a future "let's add a Grok block"
        # discussion has to re-justify it.
        assert build_model_family_block("x-ai/grok-4.3") == ""

    def test_openai_returns_extras(self) -> None:
        block = build_model_family_block("openai/gpt-5.5")
        assert block, "GPT-5.5 must receive the execution extras block"
        # Three XML tags the OpenAI block always carries.
        assert "<tool_persistence>" in block
        assert "<prerequisite_checks>" in block
        assert "<verification>" in block
        # Anti-duplication check: the discipline block already carries
        # these — they must NOT be re-injected by the family block.
        assert "<mandatory_tool_use>" not in block
        assert "<missing_context>" not in block
        assert "<act_dont_ask>" not in block

    def test_google_returns_operational(self) -> None:
        block = build_model_family_block("google/gemini-3.1-flash-lite")
        assert block, "Gemini must receive the operational block"
        # Anchor headings/bullets that the contract guarantees.
        assert "Operational directives" in block
        assert "Parallel tool calls" in block
        assert "Non-interactive commands" in block
        assert "Keep going" in block
        # Gemini block addresses the relative-path bug — pin that.
        assert "relative" in block.lower() or "absolute path" in block.lower()

    def test_chinese_returns_language_discipline(self) -> None:
        block = build_model_family_block("moonshotai/kimi-k2.6")
        assert block, "Chinese open-weight must receive language block"
        # Anchor bullets the contract guarantees.
        assert "Match the user's language" in block
        assert "No content / safety disclaimers" in block
        assert "Tool names are literals" in block
        # Concrete examples of disallowed Chinese filler — the
        # presence of these literal characters is the whole point
        # of having the block, so pin them.
        assert "您好" in block
        assert "本人作为AI助手" in block

    def test_gemma_routes_to_google(self) -> None:
        # gemma-* models (smaller open-weight Google line) share
        # enough failure modes with Gemini that they map to the same
        # block; future split if behaviours diverge.
        block = build_model_family_block("google/gemma-3-7b")
        assert "Operational directives" in block

    def test_glm_routes_to_chinese(self) -> None:
        # GLM is open-weight from Z.AI / Zhipu; same Chinese block.
        block = build_model_family_block("z-ai/glm-5")
        assert "Match the user's language" in block

    def test_none_returns_empty(self) -> None:
        assert build_model_family_block(None) == ""

    def test_unknown_returns_empty(self) -> None:
        assert build_model_family_block("acme/foo-1") == ""

    def test_block_constants_match_dispatcher_output(self) -> None:
        # Sanity: the dispatcher emits the constants verbatim. If
        # someone mutates the constant they need a snapshot update;
        # if someone mutates the dispatcher to wrap/append they
        # break the prompt-cache fingerprint contract.
        assert build_model_family_block("openai/gpt-5.5") == OPENAI_EXECUTION_EXTRAS_BLOCK
        assert build_model_family_block("google/gemini-3.1-flash-lite") == GOOGLE_OPERATIONAL_BLOCK
        assert build_model_family_block("moonshotai/kimi-k2.6") == CHINESE_OPENWEIGHT_LANGUAGE_BLOCK


class TestModelAwareSystemPrompt:
    """End-to-end: build_system_prompt(model=...) injects the right block."""

    def _builder(self, tmp_path):
        # Local import to avoid the top-of-file circular ContextBuilder
        # imports that the earlier tests in this file already navigate.
        from flowly.agent.context import ContextBuilder
        return ContextBuilder(workspace=tmp_path, persona="default")

    def test_claude_prompt_runs_agentic_no_discipline_no_family(self, tmp_path) -> None:
        prompt = self._builder(tmp_path).build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            model="anthropic/claude-sonnet-4.6",
        )
        # Claude runs on the agentic baseline alone — the strict discipline
        # block (and its hammer) is gated OUT for action-oriented families.
        assert "# How you work" in prompt          # agency block present
        assert "<mandatory_tool_use>" not in prompt  # strict block gated out
        # No family-specific tags either — the three OpenAI extras tags are
        # absent, and the Chinese block's signature line is absent.
        assert "<tool_persistence>" not in prompt
        assert "<prerequisite_checks>" not in prompt
        assert "<verification>" not in prompt
        assert "No emoji decoration" not in prompt

    def test_openai_prompt_carries_extras(self, tmp_path) -> None:
        prompt = self._builder(tmp_path).build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            model="openai/gpt-5.5",
        )
        assert "<tool_persistence>" in prompt
        assert "<prerequisite_checks>" in prompt
        assert "<verification>" in prompt
        # Chinese block markers must not leak in.
        assert "您好" not in prompt

    def test_gemini_prompt_carries_operational(self, tmp_path) -> None:
        prompt = self._builder(tmp_path).build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            model="google/gemini-3.1-flash-lite",
        )
        assert "Operational directives" in prompt
        assert "Parallel tool calls" in prompt
        assert "<tool_persistence>" not in prompt

    def test_chinese_prompt_carries_language_discipline(self, tmp_path) -> None:
        prompt = self._builder(tmp_path).build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            model="moonshotai/kimi-k2.6",
        )
        assert "Match the user's language" in prompt
        assert "Tool names are literals" in prompt
        # OpenAI extras must not leak in.
        assert "<tool_persistence>" not in prompt

    def test_no_model_falls_back_to_discipline_only(self, tmp_path) -> None:
        # Cron preflight builds the prompt before resolving the model;
        # the result must still be a valid, useful prompt.
        prompt = self._builder(tmp_path).build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            model=None,
        )
        assert "<mandatory_tool_use>" in prompt  # discipline still there
        assert "<tool_persistence>" not in prompt
        assert "Operational directives" not in prompt
        assert "Match the user's language" not in prompt

    def test_family_block_position_is_stable_for_cache(self, tmp_path) -> None:
        # Cache-stability invariant: two consecutive builds for the
        # same model must produce byte-identical prompts (so the
        # Anthropic prompt cache prefix matches). We already pin
        # timestamp-stability elsewhere; this adds the model-family
        # block to the contract.
        builder = self._builder(tmp_path)
        kwargs = dict(
            skill_names=[], skip_memory=True, skip_context_files=True,
            model="openai/gpt-5.5",
        )
        p1 = builder.build_system_prompt(**kwargs)
        import time
        time.sleep(0.05)
        p2 = builder.build_system_prompt(**kwargs)
        assert p1 == p2

    def test_strong_vs_weak_model_differ_by_discipline_and_family(
        self, tmp_path,
    ) -> None:
        # Across a strong→weak model switch, TWO things move now: the
        # strict discipline block (gated to weaker families) and the
        # family-specific add-on. Claude runs agentic-only; GPT gets both
        # the discipline hammer and its OpenAI extras.
        builder = self._builder(tmp_path)
        base_kwargs = dict(
            skill_names=[], skip_memory=True, skip_context_files=True,
        )
        claude = builder.build_system_prompt(
            **base_kwargs, model="anthropic/claude-haiku-4.5",
        )
        gpt = builder.build_system_prompt(
            **base_kwargs, model="openai/gpt-5.5",
        )
        # Both share the universal agentic baseline.
        assert "# How you work" in claude
        assert "# How you work" in gpt
        # The strict discipline block is gated to the weak model only.
        assert "<mandatory_tool_use>" not in claude
        assert "<mandatory_tool_use>" in gpt
        # The OpenAI family extras likewise land only on GPT.
        assert "<tool_persistence>" not in claude
        assert "<tool_persistence>" in gpt
        # GPT is therefore strictly longer (discipline + family added).
        assert len(gpt) > len(claude)


# ---------------------------------------------------------------------------
# Channel-specific platform hints
# ---------------------------------------------------------------------------
#
# Each entry in ``PLATFORM_HINTS`` is a small, self-contained block
# telling the agent how to render its reply on a given channel
# (markdown yes/no, native media syntax, length expectations). The
# tests below pin three things:
#
#   1. Every supported channel resolves to a hint and the hint has
#      the expected anchor headings / phrasing.
#   2. Unknown / None / empty input returns "" so callers can drop
#      the append without leaving a stray separator behind.
#   3. The channel hint is suppressed in voice mode and the prompt
#      switches to ``VOICE_MODE_BLOCK`` instead — the two overlap
#      on rendering rules and shipping both at once caused
#      oscillation in earlier hand tests.


# Every channel literal that any of the existing channel adapters
# pass into ``InboundMessage.channel``. Update this when a new
# channel ships — the parametric test below catches drift between
# adapter code and ``PLATFORM_HINTS`` automatically.
_EXPECTED_CHANNELS: tuple[str, ...] = (
    "telegram",
    "whatsapp",
    "discord",
    "slack",
    "email",
    "web",
    "cli",
    "cron",
)


class TestBuildPlatformHint:
    @pytest.mark.parametrize("channel", _EXPECTED_CHANNELS)
    def test_every_supported_channel_has_a_hint(self, channel: str) -> None:
        hint = build_platform_hint(channel)
        assert hint, f"channel {channel!r} has no hint"
        # Each hint starts with a `# Channel — <Name>` heading so a
        # consumer scanning the prompt can find it cheaply.
        assert hint.startswith("# Channel — ")

    def test_none_returns_empty(self) -> None:
        assert build_platform_hint(None) == ""

    def test_empty_returns_empty(self) -> None:
        assert build_platform_hint("") == ""

    def test_unknown_returns_empty(self) -> None:
        # Unknown channel (e.g. a future adapter not yet wired into
        # ``PLATFORM_HINTS``) must NOT raise — it falls back to no
        # hint and the rest of the prompt covers it.
        assert build_platform_hint("acme-messenger") == ""

    def test_case_insensitive(self) -> None:
        # Adapters that uppercase channel names in tests / older logs
        # must still match.
        assert build_platform_hint("TELEGRAM") == build_platform_hint("telegram")

    def test_whitespace_tolerated(self) -> None:
        assert build_platform_hint(" telegram ") == build_platform_hint("telegram")


class TestPlatformHintContent:
    """Anchor checks on per-channel hint content.

    We pin the contract-level phrasing each hint must carry, not
    the exact wording — wording will keep being tuned. The pinned
    anchors are the ones a reader / log scraper / log dashboard
    relies on to know the hint is doing its job.
    """

    def test_whatsapp_says_no_markdown(self) -> None:
        hint = build_platform_hint("whatsapp")
        # WhatsApp's whole purpose for being separate from telegram /
        # discord / slack is "no markdown rendering". If this phrase
        # drops out, models will render asterisks-and-hashes into the
        # user's reply.
        assert "does NOT render markdown" in hint

    def test_telegram_supports_markdown(self) -> None:
        hint = build_platform_hint("telegram")
        assert "Markdown formatting renders" in hint

    def test_email_forbids_greeting(self) -> None:
        # Email always adds the user's own sig in their client.
        # Double sign-offs read as spam.
        hint = build_platform_hint("email")
        assert "Do NOT add greetings" in hint or "no sign-off" in hint.lower()
        assert "no Markdown" in hint or "no markdown" in hint.lower()

    def test_cli_forbids_media_token(self) -> None:
        # MEDIA:/path tags render as literal text in the terminal —
        # only messaging adapters intercept them. Pin the warning.
        hint = build_platform_hint("cli")
        assert "Do NOT emit" in hint
        assert "MEDIA:" in hint

    def test_cron_announces_no_user_present(self) -> None:
        # Scheduled jobs run without a human in the loop; the agent
        # must NOT ask follow-up questions or wait. Pin this contract.
        hint = build_platform_hint("cron")
        assert "no user present" in hint.lower() or "no user is present" in hint.lower()
        assert "autonomously" in hint.lower() or "autonomous" in hint.lower()

    def test_web_supports_full_markdown_and_media(self) -> None:
        hint = build_platform_hint("web")
        assert "full Markdown" in hint or "full markdown" in hint.lower()
        assert "MEDIA:" in hint

    def test_slack_mentions_slack_mrkdwn_quirks(self) -> None:
        # Slack uses ``*single asterisks*`` for bold, not the
        # canonical Markdown ``**double asterisks**``. The hint must
        # call this out specifically.
        hint = build_platform_hint("slack")
        assert "single asterisks" in hint.lower()


class TestChannelAwareSystemPrompt:
    """End-to-end: build_system_prompt(channel=...) injects the right hint."""

    def _builder(self, tmp_path):
        from flowly.agent.context import ContextBuilder
        return ContextBuilder(workspace=tmp_path, persona="default")

    def test_no_channel_means_no_hint(self, tmp_path) -> None:
        prompt = self._builder(tmp_path).build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            channel=None,
        )
        assert "# Channel — " not in prompt

    def test_each_channel_injects_its_hint(self, tmp_path) -> None:
        builder = self._builder(tmp_path)
        for ch in _EXPECTED_CHANNELS:
            prompt = builder.build_system_prompt(
                skill_names=[], skip_memory=True, skip_context_files=True,
                channel=ch,
            )
            assert "# Channel — " in prompt, f"channel hint missing for {ch}"

    def test_voice_mode_suppresses_channel_hint(self, tmp_path) -> None:
        # Voice mode and channel hint overlap on rendering rules
        # (markdown, emoji, URLs). VOICE_MODE_BLOCK wins; the channel
        # hint must NOT also appear or the model oscillates.
        prompt = self._builder(tmp_path).build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            voice_mode=True, channel="telegram",
        )
        assert "# Channel — Telegram" not in prompt
        assert "# Voice Mode" in prompt

    def test_unknown_channel_does_not_crash(self, tmp_path) -> None:
        # Forward-compat: an adapter that ships with a new channel
        # name before PLATFORM_HINTS is updated should still produce
        # a valid prompt.
        prompt = self._builder(tmp_path).build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            channel="brand-new-channel",
        )
        assert prompt  # non-empty
        assert "# Channel — " not in prompt

    def test_prompt_is_cache_stable_for_same_channel(self, tmp_path) -> None:
        # Two consecutive builds with the same (model, channel) pair
        # must produce byte-identical prompts; the channel hint must
        # not interpolate any runtime value.
        builder = self._builder(tmp_path)
        kwargs = dict(
            skill_names=[], skip_memory=True, skip_context_files=True,
            channel="discord",
        )
        import time
        p1 = builder.build_system_prompt(**kwargs)
        time.sleep(0.05)
        p2 = builder.build_system_prompt(**kwargs)
        assert p1 == p2

    def test_channel_hint_size_within_expected_bounds(self, tmp_path) -> None:
        # Sanity: switching from no-channel to a known channel adds
        # roughly the hint's length to the prompt (plus the joining
        # separator). Catches future regressions where someone
        # accidentally double-injects.
        builder = self._builder(tmp_path)
        base = builder.build_system_prompt(
            skill_names=[], skip_memory=True, skip_context_files=True,
            channel=None,
        )
        for ch in ("telegram", "whatsapp", "email"):
            with_hint = builder.build_system_prompt(
                skill_names=[], skip_memory=True, skip_context_files=True,
                channel=ch,
            )
            delta = len(with_hint) - len(base)
            hint_len = len(PLATFORM_HINTS[ch])
            assert (hint_len - 20) <= delta <= (hint_len + 20), (
                f"channel={ch}: prompt grew by {delta} chars, "
                f"expected ~{hint_len}"
            )
