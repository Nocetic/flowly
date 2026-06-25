"""Tests for the reply-media envelope contract (flowly/agent/reply_media.py)."""

from __future__ import annotations

import json

from flowly.agent.reply_media import extract_reply_media, media_envelope


def test_round_trip(tmp_path):
    p = tmp_path / "img.png"
    p.write_bytes(b"x")
    env = media_envelope([str(p)], "Generated 1 image, attached.")
    paths, summary = extract_reply_media(env)
    assert paths == [str(p)]
    assert summary == "Generated 1 image, attached."


def test_envelope_is_valid_json_with_summary(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    parsed = json.loads(media_envelope([str(p)], "hi"))
    assert parsed["summary"] == "hi"
    assert parsed["_reply_media"] == [str(p)]


def test_non_envelope_returns_empty():
    assert extract_reply_media("just some text") == ([], None)
    assert extract_reply_media(json.dumps({"foo": "bar"})) == ([], None)
    assert extract_reply_media("") == ([], None)
    assert extract_reply_media(None) == ([], None)  # type: ignore[arg-type]


def test_malformed_json_returns_empty():
    # contains the key (so it passes the cheap guard) but isn't valid JSON
    assert extract_reply_media('{"_reply_media": [ broken') == ([], None)


def test_missing_files_dropped_but_summary_kept(tmp_path):
    real = tmp_path / "real.png"
    real.write_bytes(b"x")
    env = media_envelope([str(real), "/no/such/file.png"], "two asked, one real")
    paths, summary = extract_reply_media(env)
    assert paths == [str(real)]  # the fabricated path is dropped
    assert summary == "two asked, one real"


def test_all_missing_returns_empty_paths_with_summary():
    env = media_envelope(["/gone/1.png", "/gone/2.png"], "vanished")
    paths, summary = extract_reply_media(env)
    assert paths == []
    assert summary == "vanished"  # still an envelope → summary signals it


def test_empty_media_list_is_not_an_envelope():
    assert extract_reply_media(json.dumps({"_reply_media": [], "summary": "x"})) == ([], None)
