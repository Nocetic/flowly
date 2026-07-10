"""Photo capture + `vision` op — schema validation, the attachment store, and
the capture orchestration (image → item)."""

from __future__ import annotations

import pytest

from flowly.flowlets.actions import apply_action
from flowly.flowlets.schema import FlowletValidationError, validate_definition
from flowly.flowlets.vision import FlowletCaptureError, apply_capture

_JPEG = b"\xff\xd8\xff\xe0jpeg-bytes"


def _meal_def(*, keep_photo: bool = True) -> dict:
    item = {"name": "string", "kcal": "number"}
    if keep_photo:
        item["shot"] = "image"
    return {
        "catalog": 2, "name": "Kalori",
        "state": {"meals": {"type": "list", "item": item}},
        "layout": [
            {"type": "photo", "id": "add",
             "action": {"op": "vision", "prompt": "Estimate this meal.", "into": "meals"}},
            {"type": "repeater", "source": "meals",
             "item": {"type": "text", "text": "{$.name}"}},
        ],
    }


def _photo_component(defn: dict) -> dict:
    return defn["layout"][0]


_SEEN_PATHS: list[str] = []


async def _runner_ok(flowlet, prompt, image_path):
    # The media contract: a LOCAL FILE PATH to the captured JPEG (never a data
    # URI — the agent's pipeline would drop/choke on one).
    from pathlib import Path
    p = Path(image_path)
    assert p.is_file() and p.suffix == ".jpg"
    assert p.read_bytes() == _JPEG
    _SEEN_PATHS.append(image_path)
    return '{"name": "Tavuklu salata", "kcal": 420}'


# ── schema ────────────────────────────────────────────────────────────────────

def test_valid_vision_definition():
    validate_definition(_meal_def())


def test_image_field_type_valid():
    validate_definition(_meal_def(keep_photo=True))


def test_vision_requires_prompt():
    d = _meal_def()
    d["layout"][0]["action"].pop("prompt")
    with pytest.raises(FlowletValidationError, match="prompt"):
        validate_definition(d)


def test_vision_into_must_be_a_list():
    d = _meal_def()
    d["layout"][0]["action"]["into"] = "ghost"
    with pytest.raises(FlowletValidationError, match="declared list"):
        validate_definition(d)


def test_vision_cannot_target_a_source_key():
    d = _meal_def()
    d["state"]["meals"]["source"] = True   # source-owned → read-only
    d["layout"] = [d["layout"][0]]         # drop the repeater (source lists render elsewhere)
    with pytest.raises(FlowletValidationError, match="read-only"):
        validate_definition(d)


def test_image_src_allows_item_field_ref():
    # the calorie recipe shows the photo via `image src="$.shot"` in a row
    d = _meal_def()
    d["layout"][1]["item"] = {"type": "row", "children": [
        {"type": "image", "src": "$.shot", "height": 44},
        {"type": "text", "text": "{$.name}"},
    ]}
    validate_definition(d)


def test_image_src_still_rejects_garbage():
    d = _meal_def()
    d["layout"].append({"type": "image", "src": "not a url or ref!"})
    with pytest.raises(FlowletValidationError, match="src"):
        validate_definition(d)


# ── capture orchestration ─────────────────────────────────────────────────────

async def test_capture_adds_item_with_photo(store):
    f = store.create("Kalori", _meal_def())
    fl = store.get(f["id"])
    values = await apply_capture(store, fl, _photo_component(fl["definition"]), _JPEG, runner=_runner_ok)
    meals = values["meals"]
    assert len(meals) == 1
    assert meals[0]["name"] == "Tavuklu salata"
    assert meals[0]["kcal"] == 420
    assert meals[0]["shot"].startswith("att_")
    # the photo is retrievable via the attachment store
    assert store.get_attachment(f["id"], meals[0]["shot"]) == _JPEG


async def test_analyze_only_keeps_no_photo(store):
    from pathlib import Path
    _SEEN_PATHS.clear()
    f = store.create("Kalori", _meal_def(keep_photo=False))
    fl = store.get(f["id"])
    values = await apply_capture(store, fl, _photo_component(fl["definition"]), _JPEG, runner=_runner_ok)
    meals = values["meals"]
    assert len(meals) == 1 and "shot" not in meals[0]
    # nothing stored on disk (no image field to hold it)…
    assert not store._attach_dir(f["id"]).exists()
    # …and the temp file the model turn read is cleaned up too
    assert _SEEN_PATHS and not Path(_SEEN_PATHS[-1]).exists()


async def test_runner_failure_cleans_up_orphan(store):
    async def _boom(flowlet, prompt, image):
        raise RuntimeError("model down")
    f = store.create("Kalori", _meal_def())
    fl = store.get(f["id"])
    with pytest.raises(FlowletCaptureError):
        await apply_capture(store, fl, _photo_component(fl["definition"]), _JPEG, runner=_boom)
    # the just-stored photo was rolled back
    d = store._attach_dir(f["id"])
    assert not d.exists() or not list(d.glob("*.jpg"))


async def test_capture_needs_a_runner(store):
    f = store.create("Kalori", _meal_def())
    fl = store.get(f["id"])
    with pytest.raises(FlowletCaptureError):
        await apply_capture(store, fl, _photo_component(fl["definition"]), _JPEG, runner=None)


# ── attachment store + GC ─────────────────────────────────────────────────────

def test_attachment_roundtrip_and_delete(store):
    f = store.create("Kalori", _meal_def())
    att = store.put_attachment(f["id"], _JPEG)
    assert att.startswith("att_")
    assert store.get_attachment(f["id"], att) == _JPEG
    store.delete_attachment(f["id"], att)
    assert store.get_attachment(f["id"], att) is None


def test_attachment_id_is_path_safe(store):
    f = store.create("Kalori", _meal_def())
    assert store.get_attachment(f["id"], "../../etc/passwd") is None
    assert store.get_attachment(f["id"], "not-an-att-id") is None


def test_delete_flowlet_removes_attachments(store):
    f = store.create("Kalori", _meal_def())
    att = store.put_attachment(f["id"], _JPEG)
    assert store._attach_dir(f["id"]).exists()
    store.delete(f["id"])
    assert not store._attach_dir(f["id"]).exists()
    assert store.get_attachment(f["id"], att) is None


async def test_item_remove_gcs_the_photo(store):
    f = store.create("Kalori", _meal_def())
    fl = store.get(f["id"])
    values = await apply_capture(store, fl, _photo_component(fl["definition"]), _JPEG, runner=_runner_ok)
    att = values["meals"][0]["shot"]
    item_id = values["meals"][0]["id"]
    assert store.get_attachment(f["id"], att) == _JPEG
    # remove the row via the interpreter with a delete component
    d = _meal_def()
    d["layout"].append({"type": "button", "id": "del", "text": "x",
                        "action": {"op": "item_remove", "key": "meals"}})
    store.update(f["id"], definition=d)
    await apply_action(store, f["id"], "del", value={"itemId": item_id})
    assert store.get_attachment(f["id"], att) is None
