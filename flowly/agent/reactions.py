"""The agent's reactions on the user's messages: one emoji, like a person in a chat.

The ``react`` tool hands a reaction here; the surfaces that draw chats
(the Flowly server and relay through the web channel, the local gateway's
clients) register to carry it as a ``chat.reaction`` event. The server puts
it on the user's newest message in that conversation.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger

#: ``(session_key, emoji)``: carry one reaction to the chat it belongs to.
ReactionCallback = Callable[[str, str], Awaitable[None]]

#: Generous for a joined emoji (family, flag, skin tone).
_MAX_EMOJI_LENGTH = 16
_ZWJ = "\u200d"
#: What dresses a picture without being one: presentation selector, keycap, skin tones.
_MODIFIERS = frozenset({"\ufe0f", "\u20e3", *(chr(c) for c in range(0x1F3FB, 0x1F400))})


def _regional(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def is_reaction_emoji(value: object) -> bool:
    """One emoji and nothing else: one picture, or pictures joined into one
    (family, profession), or a flag. Never words, spaces, or two emoji side by side.
    The server checks the same with Unicode's own grapheme rules."""
    if not isinstance(value, str) or not value or len(value) > _MAX_EMOJI_LENGTH:
        return False
    bases = [ch for ch in value if ch not in _MODIFIERS and ch != _ZWJ]
    if not bases or any(ord(ch) < 0x2000 or ch.isspace() for ch in bases):
        return False
    if all(_regional(ch) for ch in bases):
        return len(bases) == 2  # a flag
    if any(_regional(ch) for ch in bases) or not any(ord(ch) >= 0x2600 for ch in bases):
        return False
    return len(bases) == value.count(_ZWJ) + 1


class ReactionHub:
    """Where the ``react`` tool's reactions go: every surface that registered."""

    def __init__(self) -> None:
        self._callbacks: list[ReactionCallback] = []

    def add_callback(self, callback: ReactionCallback) -> None:
        self._callbacks.append(callback)

    @property
    def connected(self) -> bool:
        return bool(self._callbacks)

    async def emit(self, session_key: str, emoji: str) -> bool:
        """Carry the reaction; True when at least one surface took it."""
        carried = False
        for callback in list(self._callbacks):
            try:
                await callback(session_key, emoji)
                carried = True
            except Exception:  # noqa: BLE001 — one surface failing never stops another
                logger.exception("[Reactions] a surface failed to carry a reaction")
        return carried


_hub: ReactionHub | None = None


def get_reaction_hub() -> ReactionHub:
    global _hub
    if _hub is None:
        _hub = ReactionHub()
    return _hub
