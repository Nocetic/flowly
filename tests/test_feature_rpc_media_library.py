"""The media-library RPC surface, exercised through the real dispatch.

Calling ``feature_rpc.dispatch`` rather than the handler functions is the point:
it proves the methods are actually registered, and registration is what makes
them reachable from every transport at once — the direct gateway falls through
to this table generically, and the relay serves it.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from flowly.channels import feature_rpc
from flowly.media.assets import describe
from flowly.media.library import (
    MEDIA_CHANGED_EVENT,
    SOURCE_GENERATED,
    SOURCE_RECEIVED,
    MediaLibrary,
    set_on_change,
)

METHODS = (
    "media.library.list",
    "media.library.get",
    "media.library.star",
    "media.library.delete",
    "media.library.stats",
)


def _png(path: Path, width: int = 8, height: int = 6) -> Path:
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


@pytest.fixture()
def seeded(tmp_path: Path, monkeypatch) -> MediaLibrary:
    """A library pinned into the RPC handlers, with three items in it."""
    media = tmp_path / "media"
    media.mkdir()
    library = MediaLibrary(tmp_path / "media.sqlite", media)

    library.record(
        [describe(_png(media / "img-a.png"), provider="fal", prompt="a red car")],
        source=SOURCE_GENERATED,
        session_key="cli:main",
        message_ts="2026-01-01T00:00:09",
    )
    library.record(
        [describe(_png(media / "img-b.png"), provider="fal", prompt="a blue boat")],
        source=SOURCE_GENERATED,
    )
    library.record([describe(_png(media / "upload-c.png"))], source=SOURCE_RECEIVED)

    monkeypatch.setattr(feature_rpc, "_media_library", lambda: library)
    yield library
    library.close()


# ── Registration ──────────────────────────────────────────────────────────────


def test_every_method_is_registered():
    """One entry per method is what reaches gateway AND relay at once."""
    for method in METHODS:
        assert method in feature_rpc.FEATURE_METHODS


def test_methods_are_not_restart_gated():
    """Browsing a gallery must never ask a user to restart their agent."""
    for method in METHODS:
        _fn, _wants_params, needs_restart = feature_rpc._DISPATCH[method]
        assert needs_restart is False


# ── list ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_items_and_a_total(seeded):
    result, _restart = await feature_rpc.dispatch("media.library.list", {})

    assert result["total"] == 3
    assert len(result["items"]) == 3


@pytest.mark.asyncio
async def test_list_filters_by_kind_and_source(seeded):
    images, _ = await feature_rpc.dispatch("media.library.list", {"kind": "image"})
    received, _ = await feature_rpc.dispatch(
        "media.library.list", {"source": "received"}
    )

    assert images["total"] == 3
    assert received["total"] == 1


@pytest.mark.asyncio
async def test_list_searches_the_prompt(seeded):
    result, _ = await feature_rpc.dispatch(
        "media.library.list", {"search": "red car"}
    )

    assert [i["mediaId"] for i in result["items"]] == ["img-a.png"]


@pytest.mark.asyncio
async def test_list_rejects_an_unknown_source(seeded):
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        await feature_rpc.dispatch("media.library.list", {"source": "invented"})

    assert exc.value.code == "INVALID"


@pytest.mark.asyncio
async def test_list_pages(seeded):
    first, _ = await feature_rpc.dispatch(
        "media.library.list", {"limit": 2, "offset": 0}
    )
    second, _ = await feature_rpc.dispatch(
        "media.library.list", {"limit": 2, "offset": 2}
    )

    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert first["total"] == second["total"] == 3


@pytest.mark.asyncio
async def test_thumbnails_are_opt_in(seeded):
    """A count-only caller must not pay for a page of base64."""
    without, _ = await feature_rpc.dispatch("media.library.list", {})
    with_thumbs, _ = await feature_rpc.dispatch(
        "media.library.list", {"withThumbs": True}
    )

    assert all("thumbnail" not in i for i in without["items"])
    assert all(
        i["thumbnail"].startswith("data:image/jpeg;base64,")
        for i in with_thumbs["items"]
    )


@pytest.mark.asyncio
async def test_items_carry_what_a_client_needs_to_play_and_navigate(seeded):
    result, _ = await feature_rpc.dispatch(
        "media.library.list", {"search": "red car"}
    )
    item = result["items"][0]

    # Delivery: the same key a chat bubble resolves through.
    assert item["mediaId"] == "img-a.png"
    assert item["version"] == 2
    # Navigation: what "Open in chat" needs.
    assert item["sessionKey"] == "cli:main"
    assert item["messageTs"] == "2026-01-01T00:00:09"


# ── get / star / delete ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_one_item(seeded):
    result, _ = await feature_rpc.dispatch(
        "media.library.get", {"id": "img-a.png"}
    )

    assert result["item"]["prompt"] == "a red car"


@pytest.mark.asyncio
async def test_get_requires_an_id(seeded):
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        await feature_rpc.dispatch("media.library.get", {})

    assert exc.value.code == "INVALID"


@pytest.mark.asyncio
async def test_get_reports_a_missing_item(seeded):
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        await feature_rpc.dispatch("media.library.get", {"id": "nope.png"})

    assert exc.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_star_round_trips(seeded):
    starred, _ = await feature_rpc.dispatch(
        "media.library.star", {"id": "img-a.png", "starred": True}
    )
    assert starred["item"]["starred"] is True

    unstarred, _ = await feature_rpc.dispatch(
        "media.library.star", {"id": "img-a.png", "starred": False}
    )
    assert unstarred["item"]["starred"] is False


@pytest.mark.asyncio
async def test_delete_removes_the_bytes(seeded, tmp_path):
    result, _ = await feature_rpc.dispatch(
        "media.library.delete", {"id": "img-a.png"}
    )

    assert result["ok"] is True
    assert not (tmp_path / "media" / "img-a.png").exists()


@pytest.mark.asyncio
async def test_delete_of_an_unknown_id_is_not_an_error(seeded):
    result, _ = await feature_rpc.dispatch("media.library.delete", {"id": "nope.png"})

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_an_id_cannot_escape_the_media_directory(seeded, tmp_path):
    """The id is a basename. Traversal is refused before it touches the disk."""
    outside = tmp_path / "secret.png"
    _png(outside)

    result, _ = await feature_rpc.dispatch(
        "media.library.delete", {"id": "../secret.png"}
    )

    assert result["ok"] is False
    assert outside.exists()


# ── stats ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_reports_usage_and_the_retention_knobs(seeded):
    result, _ = await feature_rpc.dispatch("media.library.stats", {})

    assert result["totalItems"] == 3
    assert result["byKind"]["image"]["count"] == 3
    # Retention rides along so a Library header can show the number AND the
    # setting that governs it.
    assert result["retention"]["audioMaxSizeMb"] == 1000
    assert "retentionDays" in result["retention"]


# ── change events ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mutations_broadcast_a_change(seeded):
    events: list[tuple[str, dict]] = []

    async def capture(name: str, data: dict) -> None:
        events.append((name, data))

    set_on_change(capture)
    try:
        await feature_rpc.dispatch(
            "media.library.star", {"id": "img-a.png", "starred": True}
        )
        await feature_rpc.dispatch("media.library.delete", {"id": "img-b.png"})
    finally:
        set_on_change(None)

    assert [name for name, _ in events] == [MEDIA_CHANGED_EVENT] * 2


@pytest.mark.asyncio
async def test_a_failing_listener_cannot_break_a_mutation(seeded):
    async def explode(_name: str, _data: dict) -> None:
        raise RuntimeError("subscriber is on fire")

    set_on_change(explode)
    try:
        result, _ = await feature_rpc.dispatch(
            "media.library.star", {"id": "img-a.png", "starred": True}
        )
    finally:
        set_on_change(None)

    assert result["item"]["starred"] is True
