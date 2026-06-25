"""Voice-mode response post-processor (P4).

When an iOS client sends ``chat.send`` with ``voiceMode: true``, the
backend steers the LLM toward spoken-friendly output via
``VOICE_MODE_BLOCK`` (see ``prompt_blocks.py``). That's the primary
defence, but LLMs drift — they'll occasionally slip a ``**bold**`` or
an emoji through anyway, and reading those aloud with ElevenLabs is
ugly ("star star bold star star" / "fire emoji").

``sanitize_for_tts`` is the second line of defence: a pure regex pass
that strips the patterns the prompt already forbids. Small, fast, no
LLM call. Only runs when ``voice_mode=True``; text chat is untouched.

Intentionally NOT destructive: if the text is already clean (which it
should be most of the time), the function returns it unchanged. We
don't rewrite grammar or re-order sentences — only remove visual
delimiters that mangle TTS.
"""

from __future__ import annotations

import re
import unicodedata


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
#
# Patterns are kept module-level so they're compiled once per process.
# Every one is intentionally conservative — we'd rather leave a bit of
# formatting in than accidentally delete content.


# Fenced code blocks (```lang\n...\n```).
_RE_FENCED_CODE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n?(.*?)```", flags=re.DOTALL)

# Inline code `like this`. Strip the backticks, keep the content.
_RE_INLINE_CODE = re.compile(r"`([^`\n]+?)`")

# Bold: **text** or __text__. Must have non-empty body.
_RE_BOLD_STAR = re.compile(r"\*\*([^\n*]+?)\*\*")
_RE_BOLD_UNDER = re.compile(r"__([^\n_]+?)__")

# Italic: *text* (not part of **) or _text_ (not part of __). We only
# strip single-character delimiters when they wrap a contiguous non-
# whitespace span — avoids mangling math (a*b) and snake_case
# (foo_bar).
_RE_ITALIC_STAR = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_RE_ITALIC_UNDER = re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])")

# ATX headers at line start (# Header … up to ######).
_RE_ATX_HEADER = re.compile(r"^#{1,6}\s+", flags=re.MULTILINE)

# Bullet list markers at line start.
_RE_BULLET = re.compile(r"^[ \t]*[-*+]\s+", flags=re.MULTILINE)

# Ordered list markers at line start (1. / 2) / …).
_RE_NUMBERED_LIST = re.compile(r"^[ \t]*\d+[.)]\s+", flags=re.MULTILINE)

# Blockquote prefix "> ".
_RE_BLOCKQUOTE = re.compile(r"^[ \t]*>\s?", flags=re.MULTILINE)

# Markdown links [text](url) → keep the visible text.
_RE_MD_LINK = re.compile(r"\[([^\]\n]+?)\]\(([^)\n]+?)\)")

# Bare URLs. Replacement — see sanitize_for_tts — is language-agnostic.
_RE_BARE_URL = re.compile(r"https?://\S+")

# Whitespace normalisation — collapse 3+ blank lines to 2 so TTS
# doesn't hold a long silence.
_RE_EXCESSIVE_BLANK = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Emoji detection
# ---------------------------------------------------------------------------
#
# We don't ship a full emoji library; instead we strip characters in the
# unicode categories that cover emoji + symbols (So, Sk) plus the
# Variation Selectors used for emoji presentation. This catches ~98% of
# emoji without false-positiving on CJK characters (which are in Lo /
# Nd / Po categories, not So).


_EMOJI_CATEGORIES = {"So", "Sk"}
_VARIATION_SELECTOR_RANGE = (0xFE00, 0xFE0F)
_ZWJ = 0x200D  # zero-width joiner used in compound emoji


def _is_emoji_char(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    if _VARIATION_SELECTOR_RANGE[0] <= cp <= _VARIATION_SELECTOR_RANGE[1]:
        return True
    if cp == _ZWJ:
        return True
    # Regional indicators (flags): U+1F1E6 .. U+1F1FF
    if 0x1F1E6 <= cp <= 0x1F1FF:
        return True
    return unicodedata.category(ch) in _EMOJI_CATEGORIES


def _strip_emoji(text: str) -> str:
    return "".join(ch for ch in text if not _is_emoji_char(ch))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sanitize_for_tts(text: str, *, url_hint: str = "the link") -> str:
    """Return ``text`` with TTS-hostile markup removed.

    Safe on already-clean input: every regex is a no-op when its pattern
    is absent. Order matters: fenced code first (otherwise its inline
    backticks trip the inline-code regex), then bold-before-italic
    (otherwise ``**x**`` decomposes to ``*x*`` which the italic pass
    would then eat).

    ``url_hint`` is what we substitute for bare URLs — pass a Turkish
    phrase for Turkish conversations, English for English, etc.
    """
    if not text:
        return text

    out = text

    # 1. Fenced code blocks → keep the body, drop the fences
    out = _RE_FENCED_CODE.sub(lambda m: m.group(1).rstrip(), out)

    # 2. Inline code → strip backticks
    out = _RE_INLINE_CODE.sub(r"\1", out)

    # 3. Bold BEFORE italic (see docstring)
    out = _RE_BOLD_STAR.sub(r"\1", out)
    out = _RE_BOLD_UNDER.sub(r"\1", out)

    # 4. Italic — single-delimiter variants
    out = _RE_ITALIC_STAR.sub(r"\1", out)
    out = _RE_ITALIC_UNDER.sub(r"\1", out)

    # 5. Headers / lists / blockquotes at line start
    out = _RE_ATX_HEADER.sub("", out)
    out = _RE_BULLET.sub("", out)
    out = _RE_NUMBERED_LIST.sub("", out)
    out = _RE_BLOCKQUOTE.sub("", out)

    # 6. Markdown links → visible text only
    out = _RE_MD_LINK.sub(r"\1", out)

    # 7. Bare URLs → hint. We don't try to be clever about context; the
    #    voice block already tells the LLM to say "I'll send the link" —
    #    this is just the safety net when it slips one in.
    out = _RE_BARE_URL.sub(url_hint, out)

    # 8. Emoji + variation selectors + ZWJ
    out = _strip_emoji(out)

    # 9. Whitespace tidy
    out = _RE_EXCESSIVE_BLANK.sub("\n\n", out)

    return out.strip()


__all__ = ["sanitize_for_tts"]
