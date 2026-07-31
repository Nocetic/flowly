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


# ── optional asset descriptors ───────────────────────────────────────────────

def test_envelope_without_assets_is_byte_identical_to_before(tmp_path):
    """Tools that produce plain images must not change what they emit."""
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    assert media_envelope([str(p)], "hi") == json.dumps(
        {"_reply_media": [str(p)], "summary": "hi"}
    )


def test_assets_round_trip_through_the_envelope(tmp_path):
    from flowly.agent.reply_media import extract_reply_media_assets
    from flowly.media.assets import MediaAsset

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 8)
    asset = MediaAsset(
        path=str(clip),
        kind="video",
        file_name="clip.mp4",
        mime_type="video/mp4",
        duration_ms=6000,
        id="media_1",
    )
    env = media_envelope([str(clip)], "Generated a clip.", assets=[asset])

    paths, summary = extract_reply_media(env)
    assert paths == [str(clip)]
    assert summary == "Generated a clip."
    assert extract_reply_media_assets(env) == [asset]


def test_assets_for_vanished_files_are_dropped(tmp_path):
    from flowly.agent.reply_media import extract_reply_media_assets
    from flowly.media.assets import MediaAsset

    real = tmp_path / "real.mp4"
    real.write_bytes(b"\x00")
    env = media_envelope(
        [str(real)],
        "one real",
        assets=[
            MediaAsset(path=str(real), kind="video", file_name="real.mp4"),
            MediaAsset(path="/gone/ghost.mp4", kind="video", file_name="ghost.mp4"),
        ],
    )
    assert [a.file_name for a in extract_reply_media_assets(env)] == ["real.mp4"]


def test_asset_extraction_on_a_plain_envelope_is_empty(tmp_path):
    from flowly.agent.reply_media import extract_reply_media_assets

    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    assert extract_reply_media_assets(media_envelope([str(p)], "hi")) == []
    assert extract_reply_media_assets("not an envelope") == []
    assert extract_reply_media_assets('{"_reply_media_assets": [ broken') == []
