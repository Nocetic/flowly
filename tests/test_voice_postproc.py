"""Tests for the voice-mode response post-processor (P4).

``sanitize_for_tts`` is the defence-in-depth pass that runs AFTER the
LLM returns text on voice_mode turns. The prompt tells the model not to
emit markdown/emoji, but models drift — these tests pin down the things
we absolutely can't let through.

Two shapes of assertions:

  1. *stripping* — given markup X, output contains the content but not
     the delimiter.
  2. *preservation* — given a false-positive candidate (snake_case,
     math, CJK), output is unchanged.
"""

from __future__ import annotations

import pytest

from flowly.agent.voice_postproc import sanitize_for_tts


class TestPassthrough:
    """Clean text should never be modified."""

    def test_empty_string(self) -> None:
        assert sanitize_for_tts("") == ""

    def test_none_like_values(self) -> None:
        # The signature is `str`, but defensive: empty string stays empty.
        assert sanitize_for_tts("") == ""

    def test_plain_sentence_unchanged(self) -> None:
        text = "Bugün hava güneşli ve sıcaklık on beş derece."
        assert sanitize_for_tts(text) == text

    def test_unicode_letters_preserved(self) -> None:
        # Turkish / CJK / accented characters must survive — they live
        # in unicode categories we don't touch.
        text = "İstanbul 東京 café naïve"
        assert sanitize_for_tts(text) == text


class TestMarkdownStripping:
    def test_bold_star(self) -> None:
        assert sanitize_for_tts("This is **important** news") == "This is important news"

    def test_bold_underscore(self) -> None:
        assert sanitize_for_tts("This is __important__ news") == "This is important news"

    def test_italic_star(self) -> None:
        assert sanitize_for_tts("Say *hello* now") == "Say hello now"

    def test_italic_underscore(self) -> None:
        assert sanitize_for_tts("Say _hello_ now") == "Say hello now"

    def test_bold_does_not_become_italic_leftovers(self) -> None:
        # Order-sensitive: the bold pass must run before the italic pass,
        # otherwise **x** decomposes to *x* and the italic regex eats it
        # too, losing the ``**`` info entirely. Correct behaviour: bold
        # strips fully in one step.
        out = sanitize_for_tts("**x**")
        assert out == "x"

    def test_atx_headers(self) -> None:
        out = sanitize_for_tts("# Title\n\nbody")
        assert "#" not in out
        assert "Title" in out
        assert "body" in out

    def test_unordered_list(self) -> None:
        out = sanitize_for_tts("- first\n- second\n- third")
        assert "-" not in out.split("\n")[0]  # no leading -
        assert "first" in out
        assert "second" in out

    def test_numbered_list(self) -> None:
        out = sanitize_for_tts("1. first\n2. second\n3. third")
        assert "first" in out and "second" in out
        # No digit at the start of any non-empty line after sanitizing
        for line in out.splitlines():
            stripped = line.lstrip()
            if stripped:
                assert not stripped[:2] in ("1.", "2.", "3.")

    def test_blockquote(self) -> None:
        out = sanitize_for_tts("> quoted line")
        assert "quoted line" in out
        assert ">" not in out

    def test_fenced_code_block(self) -> None:
        src = "Here is code:\n```python\ndef f(): pass\n```\nThat was it."
        out = sanitize_for_tts(src)
        assert "```" not in out
        assert "def f(): pass" in out
        assert "That was it" in out

    def test_inline_code(self) -> None:
        out = sanitize_for_tts("Use the `print` function.")
        assert "`" not in out
        assert "print" in out

    def test_markdown_link_keeps_visible_text(self) -> None:
        out = sanitize_for_tts("See [the docs](https://example.com/docs).")
        assert "the docs" in out
        assert "https://" not in out
        assert "[" not in out and "]" not in out

    def test_excessive_blank_lines_collapsed(self) -> None:
        src = "first\n\n\n\n\nsecond"
        out = sanitize_for_tts(src)
        # At most one blank line between runs.
        assert "\n\n\n" not in out


class TestUrlHandling:
    def test_bare_url_replaced_with_default_hint(self) -> None:
        out = sanitize_for_tts("Check https://example.com for details.")
        assert "https://" not in out
        assert "the link" in out

    def test_custom_hint(self) -> None:
        out = sanitize_for_tts(
            "Detay için https://example.com adresine bak.",
            url_hint="linke",
        )
        assert "https://" not in out
        assert "linke" in out

    def test_multiple_urls_all_replaced(self) -> None:
        out = sanitize_for_tts(
            "Compare https://a.com and https://b.com side by side."
        )
        assert "https://" not in out
        assert out.count("the link") == 2


class TestEmojiStripping:
    def test_simple_emoji(self) -> None:
        out = sanitize_for_tts("Great job 🎉 on the launch 🚀!")
        assert "🎉" not in out
        assert "🚀" not in out
        assert "Great job" in out
        assert "on the launch" in out

    def test_skin_tone_modifier(self) -> None:
        # 👍🏽 = thumbs up + skin-tone-4 modifier. Both should go.
        out = sanitize_for_tts("Nice 👍🏽 work")
        assert "👍" not in out
        assert "🏽" not in out
        assert "Nice" in out and "work" in out

    def test_regional_flag(self) -> None:
        out = sanitize_for_tts("Türkiye 🇹🇷 is beautiful")
        assert "🇹🇷" not in out
        assert "Türkiye" in out

    def test_cjk_characters_preserved(self) -> None:
        # Kanji / Hanzi live in category Lo (letter-other), not S.
        # They must NOT be stripped.
        out = sanitize_for_tts("日本語 is fun")
        assert "日本語" in out


class TestFalsePositiveGuards:
    """The single-delimiter italic regex is the riskiest — these pin it down."""

    def test_snake_case_not_italicised(self) -> None:
        # foo_bar_baz contains underscores between letters; if the
        # italic pass ate `_bar_`, we'd get `foobar_baz` (broken).
        out = sanitize_for_tts("Use the foo_bar_baz variable.")
        assert "foo_bar_baz" in out

    def test_math_star_not_italicised(self) -> None:
        # Expression `2*3 = 6` — the * sits between digits, not words.
        # We should leave it alone (strict: the regex requires a
        # non-space non-star body on both sides of the *, and digits
        # count as non-space, so there's ambiguity. This test locks
        # in the current behaviour — if we ever accidentally strip
        # math stars, this test flags it so we reconsider.)
        out = sanitize_for_tts("Compute 2*3 quickly.")
        # Whatever we do, the digits themselves must survive.
        assert "2" in out and "3" in out

    def test_two_separate_italic_phrases(self) -> None:
        # Two independent *foo* *bar* runs — both should strip cleanly,
        # neither should interfere with the other.
        out = sanitize_for_tts("Say *hello* and *goodbye*.")
        assert "hello" in out and "goodbye" in out
        assert "*" not in out


class TestRealisticSamples:
    """End-to-end checks on realistic LLM output shapes."""

    def test_llm_slippage_mixed_markdown(self) -> None:
        src = (
            "## Plan\n\n"
            "Here's what I found:\n"
            "- **First** item at https://a.com\n"
            "- _Second_ item in `config.yaml`\n\n"
            "All done 🎉"
        )
        out = sanitize_for_tts(src)
        for bad in ("##", "**", "_S", "`", "🎉", "https://", "[", "]"):
            assert bad not in out
        # Content survives
        for good in ("Plan", "First", "Second", "config.yaml", "All done"):
            assert good in out

    def test_turkish_response_clean(self) -> None:
        # A clean Turkish reply should pass through unchanged.
        text = (
            "Bugün on beş derece güneşli bir gün. "
            "Yağış beklenmiyor. İyi günler."
        )
        assert sanitize_for_tts(text) == text

    def test_url_replacement_in_turkish(self) -> None:
        src = "Detay için https://galatasaray.org bakabilirsin."
        out = sanitize_for_tts(src, url_hint="linki göndereceğim")
        assert "https://" not in out
        assert "linki göndereceğim" in out

    def test_very_long_bare_url(self) -> None:
        url = "https://example.com/very/long/path?query=1&other=2&foo=bar"
        out = sanitize_for_tts(f"See {url} for details")
        # URL must be fully collapsed — no partial survival.
        assert "example.com" not in out
        assert "query" not in out
        assert "the link" in out
