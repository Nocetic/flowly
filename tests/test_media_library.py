"""Media library — indexing, reconciliation, search, thumbnails, backfill.

The library is a cache of the media directory, so most of these tests are about
the two staying honest with each other: what a scan discovers, what a generator
records, what retention removes, and what happens when they disagree.
"""

from __future__ import annotations

import base64
import json
import struct
import time
import zlib
from pathlib import Path

import pytest

from flowly.media.assets import MediaAsset, describe
from flowly.media.library import (
    INDEXED_KINDS,
    SOURCE_GENERATED,
    SOURCE_RECEIVED,
    THUMB_NONE,
    MediaLibrary,
    scan_media_dir,
    thumbs_dir,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _png(path: Path, width: int = 8, height: int = 6) -> Path:
    """A real, decodable PNG — Pillow has to be able to open it for thumbnails."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x7f\x9a\xc0" * width for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


def _blob(path: Path, size: int = 64) -> Path:
    path.write_bytes(b"\0" * size)
    return path


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    (tmp_path / "media").mkdir()
    return tmp_path


@pytest.fixture()
def library(home: Path) -> MediaLibrary:
    lib = MediaLibrary(home / "media.sqlite", home / "media")
    yield lib
    lib.close()


# ── Scanning ──────────────────────────────────────────────────────────────────


def test_scan_indexes_only_servable_kinds(home: Path):
    media = home / "media"
    _png(media / "img-a.png")
    _blob(media / "vid-b.mp4")
    _blob(media / "speech-c.mp3")
    # Not media: the catalog cache lives in this directory too.
    (media / "model-catalog.json").write_text("{}")
    _blob(media / "notes.pdf")

    found = {e.path.name for e in scan_media_dir(media)}
    assert found == {"img-a.png", "vid-b.mp4", "speech-c.mp3"}


def test_scan_treats_a_same_stem_image_as_a_poster(home: Path):
    media = home / "media"
    _blob(media / "vid-x.mp4")
    _png(media / "vid-x.jpg")

    entries = {e.path.name: e for e in scan_media_dir(media)}
    # The poster is folded into the clip, not offered as its own picture.
    assert set(entries) == {"vid-x.mp4"}
    assert entries["vid-x.mp4"].poster is not None
    assert entries["vid-x.mp4"].poster.name == "vid-x.jpg"


def test_scan_keeps_unrelated_same_stem_images_separate(home: Path):
    media = home / "media"
    _png(media / "cat.png")
    _png(media / "cat.jpg")

    assert {e.path.name for e in scan_media_dir(media)} == {"cat.png", "cat.jpg"}


def test_scan_skips_dotfiles_and_subdirectories(home: Path):
    media = home / "media"
    _png(media / ".hidden.png")
    (media / "thumbs").mkdir()
    _png(media / "thumbs" / "img-a.png.jpg")

    assert scan_media_dir(media) == []


def test_indexed_kinds_match_what_can_be_served():
    from flowly.media.serving import SERVABLE_MIME_PREFIXES

    assert INDEXED_KINDS == {p.rstrip("/") for p in SERVABLE_MIME_PREFIXES}


# ── Recording ─────────────────────────────────────────────────────────────────


def test_record_stores_provenance(home: Path, library: MediaLibrary):
    path = _png(home / "media" / "img-a.png")
    asset = describe(path, provider="fal", model="flux/dev", prompt="a red car")

    assert library.record([asset], source=SOURCE_GENERATED, session_key="cli:main") == 1

    item = library.get("img-a.png")
    assert item["source"] == SOURCE_GENERATED
    assert item["provider"] == "fal"
    assert item["model"] == "flux/dev"
    assert item["prompt"] == "a red car"
    assert item["sessionKey"] == "cli:main"
    assert item["kind"] == "image"
    assert item["width"] == 8 and item["height"] == 6


def test_record_skips_files_outside_the_media_directory(home: Path, library: MediaLibrary):
    """The desktop hands the gateway a path straight out of the user's Finder.

    Flowly never copied those bytes and cannot serve them by ``mediaId``, so
    indexing one would produce a library tile that 404s on click.
    """
    outside = _png(home / "on-the-desktop.png")

    assert library.record([describe(outside)], source=SOURCE_RECEIVED) == 0
    assert library.list()[1] == 0


def test_record_skips_non_servable_kinds(home: Path, library: MediaLibrary):
    doc = _blob(home / "media" / "report.pdf")

    assert library.record([describe(doc)], source=SOURCE_GENERATED) == 0


def test_record_is_idempotent_and_enriches(home: Path, library: MediaLibrary):
    path = _png(home / "media" / "img-a.png")

    library.record([describe(path)], source=SOURCE_RECEIVED)
    library.record(
        [describe(path, provider="fal", model="flux/dev", prompt="a red car")],
        source=SOURCE_GENERATED,
        session_key="cli:main",
    )

    items, total = library.list()
    assert total == 1
    assert items[0]["provider"] == "fal"
    assert items[0]["source"] == SOURCE_GENERATED


def test_a_later_poorer_write_cannot_blank_provenance(home: Path, library: MediaLibrary):
    """A reconcile scan runs after a real record and knows strictly less."""
    path = _png(home / "media" / "img-a.png")
    library.record(
        [describe(path, provider="fal", model="flux/dev", prompt="a red car")],
        source=SOURCE_GENERATED,
        session_key="cli:main",
    )

    library.reconcile(probe_new=False)

    item = library.get("img-a.png")
    assert item["prompt"] == "a red car"
    assert item["model"] == "flux/dev"
    assert item["sessionKey"] == "cli:main"


def test_record_never_raises_on_a_broken_asset(library: MediaLibrary):
    ghost = MediaAsset(path="/nowhere/at/all.png", kind="image")

    assert library.record([ghost], source=SOURCE_GENERATED) == 0


def test_record_captures_the_poster_sidecar(home: Path, library: MediaLibrary):
    media = home / "media"
    clip = _blob(media / "vid-x.mp4")
    poster = _png(media / "vid-x.jpg")
    asset = describe(clip, probe_media=False, poster_path=str(poster))

    library.record([asset], source=SOURCE_GENERATED)

    assert library.get("vid-x.mp4")["posterId"] == "vid-x.jpg"


# ── Listing, filtering, search ────────────────────────────────────────────────


def _seed(home: Path, library: MediaLibrary) -> None:
    media = home / "media"
    library.record(
        [describe(_png(media / "img-a.png"), provider="fal", prompt="a red car")],
        source=SOURCE_GENERATED,
        session_key="cli:main",
    )
    library.record(
        [describe(_png(media / "img-b.png"), provider="fal", prompt="a blue boat")],
        source=SOURCE_GENERATED,
        session_key="cli:other",
    )
    library.record(
        [describe(_blob(media / "speech-c.mp3"), probe_media=False, prompt="hello there")],
        source=SOURCE_GENERATED,
    )
    library.record(
        [describe(_png(media / "upload-d.png"))],
        source=SOURCE_RECEIVED,
    )


def test_list_filters_by_kind(home: Path, library: MediaLibrary):
    _seed(home, library)

    items, total = library.list(kind="image")
    assert total == 3
    assert {i["mediaId"] for i in items} == {"img-a.png", "img-b.png", "upload-d.png"}


def test_list_filters_by_source(home: Path, library: MediaLibrary):
    _seed(home, library)

    _items, total = library.list(source=SOURCE_RECEIVED)
    assert total == 1


def test_list_filters_by_session(home: Path, library: MediaLibrary):
    _seed(home, library)

    items, _total = library.list(session_key="cli:main")
    assert [i["mediaId"] for i in items] == ["img-a.png"]


def test_search_matches_the_prompt(home: Path, library: MediaLibrary):
    _seed(home, library)

    items, total = library.list(search="red car")
    assert total == 1
    assert items[0]["mediaId"] == "img-a.png"


def test_search_is_prefix_matched(home: Path, library: MediaLibrary):
    _seed(home, library)

    _items, total = library.list(search="blu")
    assert total == 1


def test_search_survives_punctuation(home: Path, library: MediaLibrary):
    """A quote in the box must narrow the results, not raise an FTS error."""
    _seed(home, library)

    _items, total = library.list(search='a "red" car')
    assert total == 1


def test_starred_float_to_the_top(home: Path, library: MediaLibrary):
    _seed(home, library)
    library.star("img-a.png", True)

    items, _total = library.list()
    assert items[0]["mediaId"] == "img-a.png"
    assert items[0]["starred"] is True


def test_list_pages(home: Path, library: MediaLibrary):
    _seed(home, library)

    first, total = library.list(limit=2, offset=0)
    second, _ = library.list(limit=2, offset=2)
    assert total == 4
    assert len(first) == 2 and len(second) == 2
    assert {i["mediaId"] for i in first} & {i["mediaId"] for i in second} == set()


def test_list_hides_expired_by_default(home: Path, library: MediaLibrary):
    _seed(home, library)
    library.expire(["img-a.png"])

    _items, visible = library.list()
    _items, everything = library.list(include_expired=True)
    assert visible == 3
    assert everything == 4


def test_item_carries_the_attachment_v2_shape(home: Path, library: MediaLibrary):
    """Clients hand this straight to the component that renders chat bubbles."""
    _seed(home, library)

    item = library.get("img-a.png")
    for key in ("version", "id", "mediaId", "kind", "fileName", "mimeType", "status"):
        assert key in item
    assert item["version"] == 2
    assert item["id"] == item["mediaId"]


# ── Stats ─────────────────────────────────────────────────────────────────────


def test_stats_counts_by_kind_and_source(home: Path, library: MediaLibrary):
    _seed(home, library)

    stats = library.stats()
    assert stats["totalItems"] == 4
    assert stats["byKind"]["image"]["count"] == 3
    assert stats["byKind"]["audio"]["count"] == 1
    assert stats["bySource"][SOURCE_GENERATED] == 3
    assert stats["bySource"][SOURCE_RECEIVED] == 1
    assert stats["totalBytes"] > 0


def test_stats_excludes_expired(home: Path, library: MediaLibrary):
    _seed(home, library)
    library.expire(["img-a.png"])

    stats = library.stats()
    assert stats["totalItems"] == 3
    assert stats["expiredItems"] == 1


# ── Reconcile ─────────────────────────────────────────────────────────────────


def test_reconcile_adopts_files_nobody_recorded(home: Path, library: MediaLibrary):
    _png(home / "media" / "img-a.png")

    summary = library.reconcile(probe_new=False)

    assert summary["added"] == 1
    # Unclaimed by any generator, so by elimination it arrived.
    assert library.get("img-a.png")["source"] == SOURCE_RECEIVED


def test_reconcile_expires_rows_whose_file_vanished(home: Path, library: MediaLibrary):
    path = _png(home / "media" / "img-a.png")
    library.record([describe(path)], source=SOURCE_GENERATED)
    path.unlink()

    summary = library.reconcile(probe_new=False)

    assert summary["expired"] == 1
    # The row survives so a chat bubble referencing it can still explain itself.
    assert library.get("img-a.png")["status"] == "expired"


def test_reconcile_restores_a_file_that_came_back(home: Path, library: MediaLibrary):
    path = _png(home / "media" / "img-a.png")
    library.record([describe(path)], source=SOURCE_GENERATED)
    path.unlink()
    library.reconcile(probe_new=False)

    _png(home / "media" / "img-a.png")
    summary = library.reconcile(probe_new=False)

    assert summary["restored"] == 1
    assert library.get("img-a.png")["status"] == "ready"


def test_reconcile_prunes_orphan_thumbnails(home: Path, library: MediaLibrary):
    orphan = thumbs_dir(home / "media") / "gone.png.jpg"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x")

    summary = library.reconcile(probe_new=False)

    assert summary["thumbs_pruned"] == 1
    assert not orphan.exists()


def test_reconcile_survives_a_missing_media_directory(tmp_path: Path):
    lib = MediaLibrary(tmp_path / "media.sqlite", tmp_path / "nope")
    try:
        assert lib.reconcile(probe_new=False)["added"] == 0
    finally:
        lib.close()


# ── Thumbnails ────────────────────────────────────────────────────────────────


def test_thumbnails_are_generated_and_cached(home: Path, library: MediaLibrary):
    library.record([describe(_png(home / "media" / "img-a.png"))], source=SOURCE_GENERATED)

    items, _total = library.list(with_thumbs=True)
    # Raw base64, exactly as a chat attachment carries it — clients wrap it.
    assert items[0]["thumbnail"]
    assert not items[0]["thumbnail"].startswith("data:")
    assert base64.b64decode(items[0]["thumbnail"]).startswith(b"\xff\xd8")
    assert (thumbs_dir(home / "media") / "img-a.png.jpg").is_file()


def test_audio_never_gets_a_thumbnail(home: Path, library: MediaLibrary):
    library.record(
        [describe(_blob(home / "media" / "speech-c.mp3"), probe_media=False)],
        source=SOURCE_GENERATED,
    )

    items, _total = library.list(with_thumbs=True)
    assert "thumbnail" not in items[0]
    # Recorded as hopeless so the next page render doesn't try again.
    assert library.get("speech-c.mp3")["thumbState"] == THUMB_NONE


def test_a_hopeless_thumbnail_is_attempted_once(home: Path, library: MediaLibrary, monkeypatch):
    broken = home / "media" / "img-broken.png"
    broken.write_bytes(b"not really a png")
    library.record([describe(broken, probe_media=False)], source=SOURCE_GENERATED)

    calls = {"n": 0}
    real = library._build_thumb

    def counting(item):
        calls["n"] += 1
        return real(item)

    monkeypatch.setattr(library, "_build_thumb", counting)
    library.list(with_thumbs=True)
    library.list(with_thumbs=True)

    assert calls["n"] == 1


def test_deleting_removes_bytes_poster_and_thumbnail(home: Path, library: MediaLibrary):
    media = home / "media"
    clip = _blob(media / "vid-x.mp4")
    poster = _png(media / "vid-x.jpg")
    library.record(
        [describe(clip, probe_media=False, poster_path=str(poster))],
        source=SOURCE_GENERATED,
    )
    library.list(with_thumbs=True)

    assert library.delete("vid-x.mp4") is True
    assert not clip.exists()
    assert not poster.exists()
    assert not (thumbs_dir(media) / "vid-x.mp4.jpg").exists()
    assert library.get("vid-x.mp4") is None


def test_deleting_an_unknown_id_is_a_no_op(library: MediaLibrary):
    assert library.delete("nope.png") is False


# ── Backfill ──────────────────────────────────────────────────────────────────


def _transcript(sessions: Path, name: str, messages: list[dict]) -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{name}.jsonl").write_text("")
    (sessions / f"{name}.full.jsonl").write_text(
        "\n".join(json.dumps(m) for m in messages)
    )


def test_backfill_recovers_provenance_from_transcripts(home: Path, library: MediaLibrary):
    path = _png(home / "media" / "img-a.png")
    asset = describe(path, provider="fal", model="flux/dev", prompt="a red car")
    _transcript(
        home / "sessions",
        "telegram_12345",
        [
            {"role": "user", "content": "draw a red car", "timestamp": "2026-01-01T00:00:00"},
            {
                "role": "assistant",
                "content": "here you go",
                "timestamp": "2026-01-01T00:00:09",
                "media_assets": [asset.to_dict()],
            },
        ],
    )

    summary = library.backfill_from_sessions(home / "sessions")

    assert summary["enriched"] == 1
    item = library.get("img-a.png")
    assert item["source"] == SOURCE_GENERATED
    assert item["prompt"] == "a red car"
    assert item["sessionKey"] == "telegram:12345"
    assert item["messageTs"] == "2026-01-01T00:00:09"


def test_backfill_reads_the_display_transcript_not_the_compacted_one(
    home: Path, library: MediaLibrary
):
    """Compaction rewrites the canonical file; early media lives only in .full."""
    path = _png(home / "media" / "img-a.png")
    sessions = home / "sessions"
    sessions.mkdir()
    (sessions / "cli_main.jsonl").write_text(
        json.dumps({"role": "user", "content": "[summary]"})
    )
    (sessions / "cli_main.full.jsonl").write_text(
        json.dumps(
            {
                "role": "assistant",
                "content": "here",
                "media_assets": [describe(path, prompt="a red car").to_dict()],
            }
        )
    )

    assert library.backfill_from_sessions(sessions)["enriched"] == 1
    assert library.get("img-a.png")["prompt"] == "a red car"


def test_backfill_marks_user_media_as_received(home: Path, library: MediaLibrary):
    path = _png(home / "media" / "upload-d.png")
    _transcript(
        home / "sessions",
        "cli_main",
        [
            {
                "role": "user",
                "content": "what is this",
                "media_assets": [describe(path).to_dict()],
            }
        ],
    )

    library.backfill_from_sessions(home / "sessions")

    assert library.get("upload-d.png")["source"] == SOURCE_RECEIVED


def test_backfill_runs_only_once(home: Path, library: MediaLibrary):
    _transcript(home / "sessions", "cli_main", [])

    assert library.backfill_from_sessions(home / "sessions")["skipped"] is False
    assert library.backfill_from_sessions(home / "sessions")["skipped"] is True
    assert library.backfill_from_sessions(home / "sessions", force=True)["skipped"] is False


def test_backfill_tolerates_a_corrupt_transcript(home: Path, library: MediaLibrary):
    sessions = home / "sessions"
    sessions.mkdir()
    (sessions / "cli_main.jsonl").write_text('{"media_assets": not json\n')

    assert library.backfill_from_sessions(sessions)["enriched"] == 0


def test_backfill_without_a_sessions_directory(home: Path, library: MediaLibrary):
    assert library.backfill_from_sessions(home / "nope")["sessions"] == 0


# ── Retention interplay ───────────────────────────────────────────────────────


def test_expire_marks_rows_and_drops_thumbnails(home: Path, library: MediaLibrary):
    library.record([describe(_png(home / "media" / "img-a.png"))], source=SOURCE_GENERATED)
    library.list(with_thumbs=True)

    assert library.expire(["img-a.png"]) == 1
    assert library.get("img-a.png")["status"] == "expired"
    assert not (thumbs_dir(home / "media") / "img-a.png.jpg").exists()


def test_expire_is_idempotent(home: Path, library: MediaLibrary):
    library.record([describe(_png(home / "media" / "img-a.png"))], source=SOURCE_GENERATED)

    assert library.expire(["img-a.png"]) == 1
    assert library.expire(["img-a.png"]) == 0


def test_created_at_only_ever_moves_earlier(home: Path, library: MediaLibrary):
    """A rescan must not reshuffle the gallery by restamping old files."""
    path = _png(home / "media" / "img-a.png")
    library.record([describe(path)], source=SOURCE_GENERATED)
    first = library.get("img-a.png")["createdAt"]

    time.sleep(0.01)
    library.record([describe(path)], source=SOURCE_GENERATED)

    assert library.get("img-a.png")["createdAt"] == first


# ── Backfill: recovering the prompt from tool calls ───────────────────────────


def test_backfill_recovers_the_prompt_from_the_tool_call(home: Path, library: MediaLibrary):
    """The point of the whole FTS table, for media that predates the index.

    `MediaAsset.prompt` only exists from this feature onward, so no historical
    descriptor carries one — and without this, prompt search finds nothing for
    everything a real user already has.
    """
    path = _png(home / "media" / "img-a.png")
    _transcript(
        home / "sessions",
        "web_abc123",
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "image_generate",
                            "arguments": '{"prompt": "a single red tulip", "num_images": 1}',
                        }
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "here you go",
                "timestamp": "2026-01-01T00:00:09",
                "media_assets": [describe(path, provider="fal").to_dict()],
            },
        ],
    )

    library.backfill_from_sessions(home / "sessions")

    item = library.get("img-a.png")
    assert item["prompt"] == "a single red tulip"
    # And it is searchable, which is the actual deliverable.
    found, total = library.list(search="tulip")
    assert total == 1 and found[0]["mediaId"] == "img-a.png"


def test_backfill_accepts_tool_arguments_as_a_dict(home: Path, library: MediaLibrary):
    """Most providers hand back a JSON string; some hand back an object."""
    path = _png(home / "media" / "img-a.png")
    _transcript(
        home / "sessions",
        "cli_main",
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "video_generate", "arguments": {"prompt": "a blue boat"}}}
                ],
            },
            {"role": "assistant", "media_assets": [describe(path).to_dict()]},
        ],
    )

    library.backfill_from_sessions(home / "sessions")

    assert library.get("img-a.png")["prompt"] == "a blue boat"


def test_a_recorded_prompt_wins_over_a_recovered_one(home: Path, library: MediaLibrary):
    """Media generated after this feature carries its own, better prompt."""
    path = _png(home / "media" / "img-a.png")
    _transcript(
        home / "sessions",
        "cli_main",
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "image_generate", "arguments": '{"prompt": "stale"}'}}
                ],
            },
            {
                "role": "assistant",
                "media_assets": [describe(path, prompt="the real prompt").to_dict()],
            },
        ],
    )

    library.backfill_from_sessions(home / "sessions")

    assert library.get("img-a.png")["prompt"] == "the real prompt"


def test_a_prompt_is_not_carried_to_the_next_turn(home: Path, library: MediaLibrary):
    """A screenshot two turns later must not inherit an earlier generation."""
    media = home / "media"
    generated = _png(media / "img-a.png")
    shot = _png(media / "shot-b.png")
    _transcript(
        home / "sessions",
        "cli_main",
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "image_generate", "arguments": '{"prompt": "a red car"}'}}
                ],
            },
            {"role": "assistant", "media_assets": [describe(generated).to_dict()]},
            {"role": "assistant", "media_assets": [describe(shot).to_dict()]},
        ],
    )

    library.backfill_from_sessions(home / "sessions")

    assert library.get("img-a.png")["prompt"] == "a red car"
    assert "prompt" not in library.get("shot-b.png")


def test_a_users_own_upload_never_inherits_a_prompt(home: Path, library: MediaLibrary):
    path = _png(home / "media" / "upload-d.png")
    _transcript(
        home / "sessions",
        "cli_main",
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "image_generate", "arguments": '{"prompt": "a red car"}'}}
                ],
            },
            {"role": "user", "media_assets": [describe(path).to_dict()]},
        ],
    )

    library.backfill_from_sessions(home / "sessions")

    item = library.get("upload-d.png")
    assert item["source"] == SOURCE_RECEIVED
    assert "prompt" not in item


def test_backfill_ignores_tool_calls_that_are_not_generators(home: Path, library: MediaLibrary):
    path = _png(home / "media" / "shot-b.png")
    _transcript(
        home / "sessions",
        "cli_main",
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "web_search", "arguments": '{"prompt": "not this"}'}}
                ],
            },
            {"role": "assistant", "media_assets": [describe(path).to_dict()]},
        ],
    )

    library.backfill_from_sessions(home / "sessions")

    assert "prompt" not in library.get("shot-b.png")


def test_backfill_survives_unparseable_tool_arguments(home: Path, library: MediaLibrary):
    path = _png(home / "media" / "img-a.png")
    _transcript(
        home / "sessions",
        "cli_main",
        [
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "image_generate", "arguments": "{not json"}}],
            },
            {"role": "assistant", "media_assets": [describe(path).to_dict()]},
        ],
    )

    assert library.backfill_from_sessions(home / "sessions")["enriched"] == 1
    assert "prompt" not in library.get("img-a.png")
