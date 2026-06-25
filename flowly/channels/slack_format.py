"""Markdown → Slack mrkdwn conversion and message splitting.

Slack's ``chat.postMessage`` renders *mrkdwn*, not CommonMark: bold is
``*x*`` (not ``**x**``), italic is ``_x_``, strikethrough is ``~x~``, links
are ``<url|text>``, and there are no headings. Sending raw model markdown
therefore renders literal ``**`` / ``[text](url)`` / ``#`` in Slack. This
module converts the common cases and splits oversized messages.

Kept deliberately small and dependency-free; unit-tested in
``tests/test_slack_format.py``.
"""

from __future__ import annotations

import re

# Slack's hard cap for a message's text field is 40000 chars; stay just under.
MAX_MESSAGE_LENGTH = 39000

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\((https?://[^)\s]+)\)")
_BOLD_STAR_RE = re.compile(r"\*\*([^\n]+?)\*\*")
_BOLD_USCORE_RE = re.compile(r"__([^\n]+?)__")
_STRIKE_RE = re.compile(r"~~([^\n]+?)~~")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"(?m)^(\s*)[-*+]\s+")

# Sentinels keep bold/italic/strike apart while converting, so the single-* italic
# pass can't clobber the result of the ** → * bold pass.
_B, _I, _S = "\x01", "\x02", "\x03"


def to_mrkdwn(text: str) -> str:
    """Convert common markdown to Slack mrkdwn. Code spans are left untouched."""
    if not text:
        return text

    stash: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    # 1. Protect code (block then inline) from all other substitutions.
    text = _CODE_BLOCK_RE.sub(_stash, text)
    text = _INLINE_CODE_RE.sub(_stash, text)

    # 2. Links / images: [text](url) and ![alt](url) → <url|text>
    text = _LINK_RE.sub(r"<\2|\1>", text)

    # 3. Emphasis → sentinels (bold/strike before single-* italic).
    text = _BOLD_STAR_RE.sub(f"{_B}\\1{_B}", text)
    text = _BOLD_USCORE_RE.sub(f"{_B}\\1{_B}", text)
    text = _STRIKE_RE.sub(f"{_S}\\1{_S}", text)
    text = _ITALIC_STAR_RE.sub(f"{_I}\\1{_I}", text)

    # 4. Headings have no Slack equivalent → render bold.
    text = _HEADING_RE.sub(f"{_B}\\1{_B}", text)

    # 5. Bullets: -, *, + → • (Slack does not auto-bullet these).
    text = _BULLET_RE.sub(r"\1• ", text)

    # 6. Resolve sentinels to Slack markers, then restore code spans.
    text = text.replace(_B, "*").replace(_I, "_").replace(_S, "~")
    text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    return text


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split *text* into chunks no longer than *limit*, preferring line breaks.

    Only oversized content is split — a normal reply returns a single chunk.
    A single line longer than *limit* is hard-split as a last resort.
    """
    if text is None:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # A single line that exceeds the limit: flush, then hard-split it.
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
