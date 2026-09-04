"""What a reader is told when a group member fails.

A member that could not start once put this on screen:

    jarvis: [Errno 1] Operation not permitted: '/Users/…/.flowly/config.json'

An errno and an absolute path, at the moment somebody most needed a sentence
they could act on. The code had a curated-message path and a raw-text
fallback, and the fallback is what runs when something unexpected happens —
which is exactly when the message matters.
"""
from __future__ import annotations

from flowly.profile_host_contract import ProfileHostError
from flowly.profile_rooms import _member_failure_text

FALLBACK = "This group member could not answer."


def test_a_named_failure_keeps_the_sentence_written_for_the_reader():
    error = ProfileHostError("ROOM_START_FAILED", "A group member did not accept the response.")
    assert _member_failure_text(error, FALLBACK) == "A group member did not accept the response."


def test_an_unexpected_failure_never_reaches_the_reader_verbatim():
    # The real one. Neither the errno nor the path may survive.
    error = PermissionError(1, "Operation not permitted", "/Users/someone/.flowly/config.json")
    text = _member_failure_text(error, FALLBACK)
    assert text == FALLBACK
    assert "Errno" not in text
    assert ".flowly" not in text


def test_no_exception_type_leaks_its_own_words():
    for error in (
        RuntimeError("connection reset by peer at 10.0.0.1:8443"),
        KeyError("account_key"),
        OSError(28, "No space left on device", "/var/folders/tmp"),
        ValueError("invalid literal for int() with base 10: 'x'"),
    ):
        assert _member_failure_text(error, FALLBACK) == FALLBACK


def test_a_named_failure_with_nothing_to_say_falls_back():
    assert _member_failure_text(ProfileHostError("ROOM_STOPPED", ""), FALLBACK) == FALLBACK


def test_a_long_named_message_is_bounded():
    error = ProfileHostError("ROOM_INVALID", "x" * 900)
    assert len(_member_failure_text(error, FALLBACK)) == 500
