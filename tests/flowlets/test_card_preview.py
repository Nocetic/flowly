"""A flowlet's card is a picture of the person's data: its headline figure,
how full it is, ninety days of activity as a growth ring, and — when one of
its own reminders matters right now — the sentence its author wrote for it
and a one-tap action. All of it computed here, without a model."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flowly.flowlets.card import PORTRAIT_DAYS, card_preview
from flowly.flowlets.queries import flowlet_preview, resolve_values
from flowly.flowlets.templates import build_template

TZ = ZoneInfo("Europe/Istanbul")


def at(day, hour=12, minute=0, month=10):
    return int(datetime(2026, month, day, hour, minute, tzinfo=TZ).timestamp() * 1000)


def card(defn, state=None, events=None, now=None):
    now = now or at(9, 15, 30)
    values = resolve_values(defn, state or {}, events or [], now, TZ)
    return card_preview(defn, values, events or [], now, TZ)


def test_expenses_card_reads_the_month_and_draws_ninety_days():
    defn = build_template("expenses", "tr")
    rows = [
        {"id": "a", "title": "Market", "amount": 432.5, "category": "Yemek", "date": "2026-10-09"},
        {"id": "b", "title": "Taksi", "amount": 1250, "category": "Ulaşım", "date": "2026-10-07"},
        {"id": "c", "title": "Eski", "amount": 90, "category": "Diğer", "date": "2026-08-20"},
    ]
    c = card(defn, {"expenses": rows})

    assert c["figure"]["value"] == 1682.5
    assert c["figure"]["label"] == "Bu ay"
    assert c["locale"] == "tr"
    portrait = c["portrait"]
    assert portrait["days"] == PORTRAIT_DAYS and len(portrait["v"]) == PORTRAIT_DAYS
    assert portrait["end"] == "2026-10-09"
    # The biggest day is the ring's longest line; today and two days ago show.
    assert portrait["v"][-3] == 1.0
    assert 0 < portrait["v"][-1] < 1
    assert portrait["v"][-2] == 0
    assert sum(1 for v in portrait["v"] if v > 0) == 3   # the August row too
    assert "fill" not in c                               # no goal, no vessel


def test_a_goal_fills_the_card_and_numbers_speak_the_screen_language():
    defn = build_template("water", "tr")
    events = [{"series": "water", "value": 750, "ts": at(9, 9)},
              {"series": "water", "value": 1500, "ts": at(9, 13)}]
    c = card(defn, events=events)

    assert c["fill"] == 1.0
    assert c["figure"] == {"value": 2250, "max": 2000, "unit": "ml"}   # unit read off "{today_ml} ml"
    assert c["portrait"]["v"][-1] == 1.0


def test_preview_text_is_written_in_the_screen_language():
    defn = {"locale": "tr", "layout": [{"type": "stat", "value": "total", "label": "Son 30 gün"}]}
    assert flowlet_preview(defn, {"total": 6132})["text"] == "6.132 · Son 30 gün"
    assert flowlet_preview(defn, {"total": 1234.5})["text"] == "1.234,5 · Son 30 gün"


def test_a_new_flowlet_is_a_faint_ring_not_nothing():
    c = card(build_template("water", "en"))
    assert c["portrait"]["v"] == [0.0] * PORTRAIT_DAYS
    assert c["fill"] == 0.0


def _evening(when=None, otherwise=None):
    watch = {"id": "evening", "trigger": "schedule", "at": "21:00",
             "notify": {"title": "Water", "body": "{remaining} ml to go today."}}
    if when:
        watch["when"] = when
    if otherwise:
        watch["otherwise"] = otherwise
    defn = build_template("water", "en")
    defn["watches"] = [watch]
    return defn


def test_a_scheduled_reminder_is_the_moment_around_its_time():
    defn = _evening()
    events = [{"series": "water", "value": 500, "ts": at(9, 9)}]

    early = card(defn, events=events, now=at(9, 19, 30))
    assert "moment" not in early                              # 90 minutes before

    due = card(defn, events=events, now=at(9, 21, 20))
    moment = due["moment"]
    assert moment["text"] == "1,500 ml to go today."
    assert moment["title"] == "Water" and moment["at"] == "21:00"
    # The water screen's first quick button is the one-tap action.
    assert moment["action"]["kind"] == "tap" and moment["action"]["id"]

    late = card(defn, events=events, now=at(9, 23, 59))
    assert late["moment"]["score"] < moment["score"]          # still relevant, less so


def test_a_met_goal_shows_the_authors_otherwise_line_or_nothing():
    events = [{"series": "water", "value": 2500, "ts": at(9, 9)}]
    gated = _evening(when="remaining > 0")
    assert "moment" not in card(gated, events=events, now=at(9, 21, 10))

    done = _evening(when="remaining > 0", otherwise={"title": "Water", "body": "All done for today."})
    assert card(done, events=events, now=at(9, 21, 10))["moment"]["text"] == "All done for today."


def test_a_holding_condition_is_a_moment_and_an_off_day_is_not():
    defn = build_template("water", "en")
    defn["watches"] = [{"id": "low", "trigger": "condition", "when": "today_ml < 500", "after": "14:00",
                        "notify": {"title": "Water", "body": "Barely any water yet."}}]
    assert card(defn, now=at(9, 13))  .get("moment") is None   # before `after`
    assert card(defn, now=at(9, 15))["moment"]["text"] == "Barely any water yet."

    weekend = _evening()
    weekend["watches"][0]["days"] = ["sat", "sun"]            # 2026-10-09 is a Friday
    assert "moment" not in card(weekend, now=at(9, 21, 5))


def test_a_photo_capture_is_the_one_tap_action_when_it_comes_first():
    defn = build_template("expenses", "tr")
    defn["watches"] = [{"id": "receipts", "trigger": "schedule", "at": "20:00",
                        "notify": {"title": "Fişler", "body": "Bugünün fişlerini ekle."}}]
    moment = card(defn, now=at(9, 20, 15))["moment"]
    assert moment["action"] == {"id": "receiptShot", "kind": "photo", "label": "Fişten ekle"}


def test_a_screen_saved_with_a_slip_is_healed_when_read(store):
    """An older screen whose category chart said `groupBy` showed "No data
    yet" forever; reading it now yields the repaired chart."""
    defn = build_template("expenses", "tr")
    for node in defn["layout"]:
        if node.get("id") == "byCategory":
            node["data"]["groupBy"] = node["data"].pop("by")
    fid = store.create(name="Aylık fişlerim", definition=defn)["id"]

    data = next(n for n in store.get(fid)["definition"]["layout"] if n.get("id") == "byCategory")["data"]
    assert data["by"] == "category" and "groupBy" not in data
