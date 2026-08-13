"""MediaAsset + Attachment V2 contract (flowly/media/assets.py).

Attachment V2 is what makes a video renderable on a client: kind, duration and
dimensions travel with the file instead of being guessed. It must stay ADDITIVE
over V1 — an old client reads fileName/mimeType/thumbnail/mediaId/cdnUrl and
ignores the rest — and it must never leak a local path or a provider URL.
"""

from __future__ import annotations

from pathlib import Path

from flowly.media.assets import (
    ATTACHMENT_VERSION,
    KIND_IMAGE,
    KIND_VIDEO,
    STATUS_EXPIRED,
    STATUS_READY,
    MediaAsset,
    assets_from_meta,
    assets_to_meta,
    attachment_v2,
    describe,
    index_by_path,
    kind_for_mime,
)


def test_kind_for_mime():
    assert kind_for_mime("image/png") == KIND_IMAGE
    assert kind_for_mime("video/mp4") == KIND_VIDEO
    assert kind_for_mime("audio/mpeg") == "audio"
    assert kind_for_mime("application/pdf") == "file"
    assert kind_for_mime("") == "file"


def test_describe_image_probes_dimensions(tmp_path):
    from PIL import Image

    p = tmp_path / "shot.png"
    Image.new("RGB", (64, 48), (1, 2, 3)).save(p)

    art = describe(p, provider="fal", model="fal-ai/flux/dev")
    assert art.kind == KIND_IMAGE
    assert art.file_name == "shot.png"
    assert art.mime_type == "image/png"
    assert (art.width, art.height) == (64, 48)
    assert art.size == p.stat().st_size
    assert art.status == STATUS_READY
    assert art.id.startswith("media_")


def test_describe_missing_file_is_expired_not_an_error(tmp_path):
    """History can reference media retention already reclaimed."""
    art = describe(tmp_path / "gone.mp4")
    assert art.status == STATUS_EXPIRED
    assert art.kind == KIND_VIDEO
    assert art.size == 0


def test_attachment_v2_for_video_carries_playback_metadata():
    art = MediaAsset(
        path="/home/u/.flowly/media/clip.mp4",
        kind=KIND_VIDEO,
        file_name="clip.mp4",
        mime_type="video/mp4",
        size=1234,
        width=1080,
        height=1920,
        duration_ms=6000,
        id="media_abc",
    )
    att = attachment_v2(art, thumbnail="BASE64", media_id="clip.mp4")

    assert att["version"] == ATTACHMENT_VERSION
    assert att["kind"] == KIND_VIDEO
    assert att["mimeType"] == "video/mp4"  # NOT overwritten by the poster's mime
    assert att["durationMs"] == 6000
    assert (att["width"], att["height"]) == (1080, 1920)
    assert att["thumbnail"] == "BASE64"
    assert att["mediaId"] == "clip.mp4"
    assert att["status"] == STATUS_READY


def test_attachment_v2_never_publishes_local_or_provider_paths():
    art = MediaAsset(
        path="/home/u/.flowly/media/clip.mp4",
        kind=KIND_VIDEO,
        file_name="clip.mp4",
        mime_type="video/mp4",
        poster_path="/home/u/.flowly/media/clip.jpg",
        source_url="https://fal.media/signed?token=SECRET",
        request_id="req-42",
        id="media_abc",
    )
    att = attachment_v2(art, media_id="clip.mp4")
    blob = repr(att)

    assert "/home/u" not in blob
    assert "SECRET" not in blob
    assert "req-42" not in blob
    assert "poster_path" not in att and "path" not in att


def test_attachment_v2_omits_absent_fields():
    """Old clients keyed off presence — absent must mean absent, not null."""
    art = MediaAsset(path="/m/a.png", kind=KIND_IMAGE, file_name="a.png", mime_type="image/png")
    att = attachment_v2(art)
    for key in ("thumbnail", "cdnUrl", "mediaId", "width", "height", "durationMs", "size"):
        assert key not in att


def test_attachment_v2_keeps_v1_keys_for_old_clients():
    art = MediaAsset(path="/m/a.png", kind=KIND_IMAGE, file_name="a.png", mime_type="image/png")
    att = attachment_v2(art, thumbnail="T", cdn_url="https://cdn/a.png")
    assert att["fileName"] == "a.png"
    assert att["mimeType"] == "image/png"
    assert att["thumbnail"] == "T"
    assert att["cdnUrl"] == "https://cdn/a.png"


def test_round_trip_through_persistence():
    art = MediaAsset(
        path="/m/clip.mp4",
        kind=KIND_VIDEO,
        file_name="clip.mp4",
        mime_type="video/mp4",
        size=99,
        width=1080,
        height=1920,
        duration_ms=6000,
        poster_path="/m/clip.jpg",
        provider="fal",
        model="fal-ai/some/model",
        id="media_abc",
    )
    restored = assets_from_meta(assets_to_meta([art]))
    assert restored == [art]


def test_from_dict_tolerates_partial_legacy_entries():
    """A session written before this module must still render."""
    restored = assets_from_meta([{"path": "/m/x.mp4"}, {"no": "path"}, "junk"])
    assert len(restored) == 1
    art = restored[0]
    assert art.kind == KIND_VIDEO
    assert art.file_name == "x.mp4"
    assert art.mime_type == "video/mp4"
    assert art.id.startswith("media_")


def test_index_by_path():
    a = MediaAsset(path="/m/a.png")
    b = MediaAsset(path="/m/b.mp4")
    assert index_by_path([a, b]) == {"/m/a.png": a, "/m/b.mp4": b}


def test_describe_uses_given_poster_and_source(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 16)
    art = describe(
        p,
        poster_path=str(tmp_path / "clip.jpg"),
        source_url="https://fal.media/x.mp4",
        provider="fal",
        model="m",
        request_id="r",
        probe_media=False,
    )
    assert art.poster_path == str(tmp_path / "clip.jpg")
    assert art.source_url == "https://fal.media/x.mp4"
    assert art.kind == KIND_VIDEO
    assert Path(art.path) == p


# ── prompt: indexed, never published ──────────────────────────────────────────


def test_prompt_survives_a_persistence_round_trip():
    a = MediaAsset(path="/m/a.png", kind=KIND_IMAGE, prompt="a red car")

    restored = assets_from_meta(assets_to_meta([a]))[0]

    assert restored.prompt == "a red car"


def test_prompt_is_absent_when_empty():
    """Empty strings stay out of the persisted dict, as every other field does."""
    assert "prompt" not in MediaAsset(path="/m/a.png").to_dict()


def test_a_transcript_written_before_prompts_existed_still_loads():
    restored = assets_from_meta([{"path": "/m/a.png", "kind": KIND_IMAGE}])

    assert restored[0].prompt == ""


def test_attachment_v2_never_publishes_the_prompt():
    """The chat wire is the publication boundary; the library is not.

    A bubble sits directly under the message that requested it, so the prompt
    adds nothing there — and keeping it off the wire means history payloads
    don't grow by a paragraph per image.
    """
    a = MediaAsset(path="/m/a.png", kind=KIND_IMAGE, prompt="a red car")

    assert "prompt" not in attachment_v2(a)


def test_describe_accepts_a_prompt(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"\x00" * 8)

    assert describe(p, prompt="a red car", probe_media=False).prompt == "a red car"
