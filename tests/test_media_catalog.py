"""The dynamic model catalog and the schema-driven adapter.

Flowly used to ship six hand-written image models. Everything else fal offered
— including every video model — was invisible until someone edited the source.
The catalog replaces that list, so these tests care most about the awkward
cases: a network that fails, a cache that is stale, a model whose fields are
spelled differently, and a model Flowly simply cannot drive.
"""

from __future__ import annotations

import json
import time

import pytest

from flowly.media.adapters import (
    AdapterError,
    build_payload,
    coerce_value,
    extract_media_urls,
    input_schema_from_openapi,
    resolve_property,
    verdict_for,
)
from flowly.media.catalog import (
    COMPAT_READY,
    COMPAT_UNSUPPORTED,
    FALLBACK_MODELS,
    MediaModel,
    ModelCatalog,
    model_from_catalog_entry,
)


def _entry(endpoint_id: str, category: str, name: str = "", **meta):
    return {
        "endpoint_id": endpoint_id,
        "metadata": {
            "display_name": name or endpoint_id,
            "category": category,
            "status": "active",
            **meta,
        },
    }


# ── catalog rows ────────────────────────────────────────────────────────────


def test_catalog_entry_maps_to_a_model():
    model = model_from_catalog_entry(
        _entry(
            "fal-ai/veo3.1",
            "text-to-video",
            "Veo 3.1",
            description="cinematic",
            tags=["stylized", 7],
            thumbnail_url="https://x/y.jpg",
            updated_at="2026-07-10T18:12:05.187Z",
        )
    )
    assert model is not None
    assert model.endpoint_id == "fal-ai/veo3.1"
    assert model.label == "Veo 3.1"
    assert model.category == "text-to-video"
    assert model.tags == ("stylized",)  # the non-string is dropped
    assert model.is_video is True


def test_malformed_rows_are_skipped():
    assert model_from_catalog_entry({}) is None
    assert model_from_catalog_entry({"endpoint_id": ""}) is None
    assert model_from_catalog_entry("nonsense") is None  # type: ignore[arg-type]


# ── caching and failure ─────────────────────────────────────────────────────


@pytest.fixture
def cache_file(tmp_path):
    return tmp_path / "model-catalog.json"


async def test_sync_populates_the_cache(cache_file, monkeypatch):
    catalog = ModelCatalog(cache_path=cache_file)

    async def fake_sync():
        return {"text-to-video": [MediaModel("a/b", "A B", "text-to-video")]}

    monkeypatch.setattr(catalog, "_sync", fake_sync)
    models = await catalog.list_models(category="text-to-video")

    assert [m.endpoint_id for m in models] == ["a/b"]
    assert cache_file.is_file()
    assert json.loads(cache_file.read_text())["categories"]["text-to-video"]


async def test_a_fresh_cache_is_served_without_a_sync(cache_file, monkeypatch):
    cache_file.write_text(json.dumps({
        "version": 1,
        "fetchedAt": time.time(),
        "categories": {"text-to-video": [MediaModel("cached/one", "Cached", "text-to-video").to_dict()]},
    }))
    catalog = ModelCatalog(cache_path=cache_file)

    async def boom():
        raise AssertionError("a fresh cache must not hit the network")

    monkeypatch.setattr(catalog, "_sync", boom)
    assert [m.endpoint_id for m in await catalog.list_models()] == ["cached/one"]


async def test_a_stale_cache_beats_an_error(cache_file, monkeypatch):
    """Someone picking a model on a flaky connection wants yesterday's list."""
    cache_file.write_text(json.dumps({
        "version": 1,
        "fetchedAt": time.time() - 60 * 60 * 24,  # a day old
        "categories": {"text-to-video": [MediaModel("stale/one", "Stale", "text-to-video").to_dict()]},
    }))
    catalog = ModelCatalog(cache_path=cache_file)

    async def failing_sync():
        from flowly.media.fal_catalog import CatalogError

        raise CatalogError("no network")

    monkeypatch.setattr(catalog, "_sync", failing_sync)
    assert [m.endpoint_id for m in await catalog.list_models()] == ["stale/one"]


async def test_no_cache_and_no_network_falls_back_to_the_shortlist(cache_file, monkeypatch):
    catalog = ModelCatalog(cache_path=cache_file)

    async def failing_sync():
        raise RuntimeError("offline")

    monkeypatch.setattr(catalog, "_sync", failing_sync)
    models = await catalog.list_models()

    ids = {m.endpoint_id for m in models}
    assert ids == {m.endpoint_id for m in FALLBACK_MODELS}
    assert any(m.is_video for m in models), "the shortlist must make video usable too"


async def test_an_empty_sync_does_not_wipe_a_good_cache(cache_file, monkeypatch):
    cache_file.write_text(json.dumps({
        "version": 1,
        "fetchedAt": time.time() - 60 * 60 * 24,
        "categories": {"text-to-video": [MediaModel("kept/one", "Kept", "text-to-video").to_dict()]},
    }))
    catalog = ModelCatalog(cache_path=cache_file)

    async def empty_sync():
        return {"text-to-video": []}

    monkeypatch.setattr(catalog, "_sync", empty_sync)
    assert [m.endpoint_id for m in await catalog.list_models()] == ["kept/one"]


async def test_a_corrupt_cache_is_ignored_not_fatal(cache_file, monkeypatch):
    cache_file.write_text("{ this is not json")
    catalog = ModelCatalog(cache_path=cache_file)

    async def fake_sync():
        return {"text-to-image": [MediaModel("fresh/one", "Fresh", "text-to-image")]}

    monkeypatch.setattr(catalog, "_sync", fake_sync)
    assert [m.endpoint_id for m in await catalog.list_models()] == ["fresh/one"]


# ── search ──────────────────────────────────────────────────────────────────


async def test_search_ranks_id_and_label_over_description(cache_file, monkeypatch):
    catalog = ModelCatalog(cache_path=cache_file)

    async def fake_sync():
        return {
            "text-to-video": [
                MediaModel("other/model", "Other", "text-to-video",
                           description="works a lot like kling"),
                MediaModel("fal-ai/kling-video/v3", "Kling v3", "text-to-video"),
                MediaModel("third/model", "Third", "text-to-video", tags=("kling-style",)),
            ]
        }

    monkeypatch.setattr(catalog, "_sync", fake_sync)
    hits = await catalog.search("kling")

    assert [m.endpoint_id for m in hits] == [
        "fal-ai/kling-video/v3",  # id match
        "third/model",            # tag match
        "other/model",            # description match
    ]


async def test_empty_search_returns_the_category(cache_file, monkeypatch):
    catalog = ModelCatalog(cache_path=cache_file)

    async def fake_sync():
        return {"text-to-image": [MediaModel("a/b", "A", "text-to-image")]}

    monkeypatch.setattr(catalog, "_sync", fake_sync)
    assert len(await catalog.search("", category="text-to-image")) == 1
    assert await catalog.search("nothing-matches-this") == []


# ── OpenAPI → schema ────────────────────────────────────────────────────────

KLING_SCHEMA = {
    "required": ["start_image_url"],
    "properties": {
        "prompt": {"type": "string"},
        "negative_prompt": {"type": "string"},
        "start_image_url": {"type": "string"},
        "duration": {"enum": ["5", "10"]},
        "generate_audio": {"type": "boolean"},
    },
}


def test_input_schema_is_found_by_naming_convention():
    openapi = {
        "components": {
            "schemas": {
                "QueueStatus": {"type": "object"},
                "SomeModelInput": KLING_SCHEMA,
                "SomeModelOutput": {"properties": {}},
            }
        }
    }
    assert input_schema_from_openapi(openapi) is KLING_SCHEMA


def test_input_schema_falls_back_to_the_request_body():
    openapi = {
        "paths": {
            "/": {
                "post": {
                    "requestBody": {
                        "content": {"application/json": {"schema": KLING_SCHEMA}}
                    }
                }
            }
        }
    }
    assert input_schema_from_openapi(openapi) is KLING_SCHEMA


def test_input_schema_missing_is_not_an_error():
    assert input_schema_from_openapi(None) is None
    assert input_schema_from_openapi({}) is None


# ── compatibility ───────────────────────────────────────────────────────────


def test_a_model_we_can_fully_drive_is_ready():
    assert verdict_for("image-to-video", KLING_SCHEMA) == COMPAT_READY


def test_a_model_requiring_something_we_cannot_supply_is_unsupported():
    schema = {
        "required": ["prompt", "reference_audio_url"],
        "properties": {"prompt": {"type": "string"}, "reference_audio_url": {"type": "string"}},
    }
    assert verdict_for("text-to-video", schema) == COMPAT_UNSUPPORTED


def test_an_image_to_video_model_without_an_image_input_is_unsupported():
    schema = {"required": [], "properties": {"prompt": {"type": "string"}}}
    assert verdict_for("image-to-video", schema) == COMPAT_UNSUPPORTED


def test_a_text_to_video_model_without_a_prompt_is_unsupported():
    schema = {"required": [], "properties": {"seed": {"type": "integer"}}}
    assert verdict_for("text-to-video", schema) == COMPAT_UNSUPPORTED


# ── payload mapping ─────────────────────────────────────────────────────────


def test_field_names_are_resolved_per_model():
    assert resolve_property(KLING_SCHEMA, "input_image") == "start_image_url"
    assert resolve_property(KLING_SCHEMA, "duration_seconds") == "duration"
    assert resolve_property(KLING_SCHEMA, "resolution") is None


def test_duration_snaps_to_the_nearest_option_the_model_offers():
    """Asking for 7s from a model that does 5 or 10 should give 5, not an error."""
    prop = {"enum": ["5", "10"]}
    assert coerce_value(7, prop) == "5"
    assert coerce_value(9, prop) == "10"
    assert coerce_value(10, prop) == "10"


def test_values_are_coerced_to_the_declared_type():
    assert coerce_value("6", {"type": "integer"}) == 6
    assert coerce_value(6, {"type": "string"}) == "6"
    assert coerce_value("yes", {"type": "boolean"}) is True
    assert coerce_value("nonsense", {"type": "integer"}) is None


def test_payload_uses_the_models_own_field_names():
    payload, dropped = build_payload(
        KLING_SCHEMA,
        {
            "prompt": "a cat",
            "input_image": "https://x/y.png",
            "duration_seconds": 6,
            "aspect_ratio": "9:16",
        },
    )
    assert payload == {
        "prompt": "a cat",
        "start_image_url": "https://x/y.png",
        "duration": "5",
    }
    assert dropped == ["aspect_ratio"]  # this model has no such field


def test_an_explicit_audio_request_is_refused_rather_than_ignored():
    """A user who asked for sound must not silently get a mute clip."""
    schema = {"required": [], "properties": {"prompt": {"type": "string"}}}
    with pytest.raises(AdapterError, match="generate_audio"):
        build_payload(
            schema,
            {"prompt": "a cat", "generate_audio": True},
            explicit=frozenset({"generate_audio"}),
        )


def test_an_unrequested_field_is_dropped_quietly():
    schema = {"required": [], "properties": {"prompt": {"type": "string"}}}
    payload, dropped = build_payload(schema, {"prompt": "a cat", "resolution": "720p"})
    assert payload == {"prompt": "a cat"}
    assert dropped == ["resolution"]


def test_a_missing_required_field_is_an_error():
    with pytest.raises(AdapterError, match="start_image_url"):
        build_payload(KLING_SCHEMA, {"prompt": "a cat"})


# ── output shapes ───────────────────────────────────────────────────────────


def test_video_urls_are_found_across_provider_shapes():
    assert extract_media_urls({"video": {"url": "https://a/v.mp4"}}, kind="video") == [
        "https://a/v.mp4"
    ]
    assert extract_media_urls(
        {"videos": [{"url": "https://a/1.mp4"}, {"url": "https://a/2.mp4"}]}, kind="video"
    ) == ["https://a/1.mp4", "https://a/2.mp4"]
    assert extract_media_urls({"url": "https://a/v.mp4"}, kind="video") == ["https://a/v.mp4"]
    assert extract_media_urls({"images": [{"url": "https://a/i.png"}]}, kind="image") == [
        "https://a/i.png"
    ]


def test_non_http_results_are_not_treated_as_urls():
    assert extract_media_urls({"video": {"url": "file:///etc/passwd"}}, kind="video") == []
    assert extract_media_urls({}, kind="video") == []
    assert extract_media_urls(None, kind="video") == []
