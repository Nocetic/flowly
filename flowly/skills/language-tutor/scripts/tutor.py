#!/usr/bin/env python3
"""Language-tutor store: learner profile, vocabulary, and SRS scheduling.

This helper owns *all* persistence and the spaced-repetition math. It never
generates language content or judges answers — that is the agent's job. The
split keeps grading conversational while the scheduling stays deterministic
and crash-safe.

Data lives under ``~/.flowly/skills/language-tutor/data/``:

    profile.json        — learner profile + streak (one per install)
    <lang>.json         — one vocabulary store per target language

All writes go through a temp-file + atomic rename so a crash can't leave a
half-written store behind. Every command prints JSON to stdout.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── Storage layout ──────────────────────────────────────────────────────────

STORE_DIR = Path(
    os.environ.get("FLOWLY_HOME", str(Path.home() / ".flowly"))
) / "skills" / "language-tutor" / "data"
PROFILE_PATH = STORE_DIR / "profile.json"

# ── Scheduling constants (SM-2 lite) ────────────────────────────────────────

EASE_DEFAULT = 2.5
EASE_MIN = 1.3
GRADUATE_DAYS = 21          # interval at which a word counts as "known"
NEVER = "9999-12-31T00:00:00+00:00"
RATINGS = ("again", "hard", "good", "easy", "retire")
LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")


# ── Time helpers ────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def from_iso(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def today_str() -> str:
    return utcnow().date().isoformat()


# ── Persistence ─────────────────────────────────────────────────────────────

def _lang_path(lang: str) -> Path:
    return STORE_DIR / f"{lang}.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=STORE_DIR, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _load_profile() -> dict[str, Any]:
    if PROFILE_PATH.exists():
        with open(PROFILE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {"active_lang": None, "langs": {}, "streak": {"count": 0, "last_session": None}}


def _save_profile(profile: dict[str, Any]) -> None:
    _atomic_write(PROFILE_PATH, profile)


def _load_lang(lang: str) -> dict[str, Any]:
    path = _lang_path(lang)
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"lang": lang, "words": {}}


def _save_lang(store: dict[str, Any]) -> None:
    _atomic_write(_lang_path(store["lang"]), store)


def emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def fail(message: str, code: int = 2) -> None:
    emit({"ok": False, "error": message})
    sys.exit(code)


# ── Resolve the active language ─────────────────────────────────────────────

def resolve_lang(profile: dict[str, Any], requested: str | None) -> str:
    lang = (requested or profile.get("active_lang") or "").strip().lower()
    if not lang:
        fail("no language given and no active language set — run `profile set` first")
    return lang


# ── Scheduling ──────────────────────────────────────────────────────────────

def reschedule(card: dict[str, Any], rating: str) -> dict[str, Any]:
    """Apply a rating and return the mutated card. SM-2 lite.

    againg  — lapse: due again now, ease drops, stays in rotation
    hard    — short interval, ease drops a little
    good    — interval * ease
    easy    — interval * ease * 1.3, ease rises, faster graduation
    retire  — removed from rotation
    """
    now = utcnow()
    card["last_rated_at"] = to_iso(now)

    if rating == "retire":
        card["state"] = "retired"
        card["next_review_at"] = NEVER
        return card

    ease = float(card.get("ease", EASE_DEFAULT))
    interval = float(card.get("interval_days", 0))
    reps = int(card.get("reps", 0))
    first = reps == 0

    if rating == "again":
        card["lapses"] = int(card.get("lapses", 0)) + 1
        ease = max(EASE_MIN, ease - 0.2)
        interval = 0.0  # due again today
    elif rating == "hard":
        ease = max(EASE_MIN, ease - 0.15)
        interval = 1.0 if first else max(1.0, round(interval * 1.2))
        reps += 1
    elif rating == "good":
        interval = 1.0 if first else max(1.0, round(interval * ease))
        reps += 1
    elif rating == "easy":
        ease = ease + 0.15
        interval = 4.0 if first else max(1.0, round(interval * ease * 1.3))
        reps += 1
    else:
        raise ValueError(f"unknown rating: {rating}")

    card["ease"] = round(ease, 3)
    card["interval_days"] = interval
    card["reps"] = reps
    card["next_review_at"] = to_iso(now + timedelta(days=interval))

    if interval >= GRADUATE_DAYS:
        card["state"] = "known"
    elif card.get("state") in (None, "new"):
        card["state"] = "learning"
    elif rating == "again":
        card["state"] = "learning"
    return card


def new_card(word: str, translation: str, example: str, pos: str,
             note: str, tags: list[str]) -> dict[str, Any]:
    stamp = to_iso(utcnow())
    return {
        "id": uuid.uuid4().hex[:6].upper(),
        "word": word,
        "translation": translation,
        "example": example or "",
        "pos": pos or "",
        "note": note or "",
        "tags": tags or [],
        "state": "new",
        "ease": EASE_DEFAULT,
        "interval_days": 0,
        "reps": 0,
        "lapses": 0,
        "next_review_at": stamp,
        "created_at": stamp,
        "last_rated_at": None,
    }


# ── Commands ────────────────────────────────────────────────────────────────

def do_profile_set(args: argparse.Namespace) -> None:
    profile = _load_profile()
    lang = (args.lang or "").strip().lower()
    if not lang:
        fail("--lang is required")
    if args.level and args.level.upper() not in LEVELS:
        fail(f"--level must be one of {', '.join(LEVELS)}")

    entry = profile["langs"].get(lang, {"created_at": to_iso(utcnow())})
    if args.native:
        entry["native"] = args.native.strip().lower()
    if args.level:
        entry["level"] = args.level.upper()
    if args.daily is not None:
        entry["daily_goal"] = max(1, args.daily)
    entry.setdefault("native", "en")
    entry.setdefault("level", "A1")
    entry.setdefault("daily_goal", 10)
    profile["langs"][lang] = entry
    profile["active_lang"] = lang
    _save_profile(profile)
    # make sure the vocab store exists
    if not _lang_path(lang).exists():
        _save_lang(_load_lang(lang))
    emit({"ok": True, "active_lang": lang, "profile": entry})


def do_profile_show(_args: argparse.Namespace) -> None:
    profile = _load_profile()
    emit({"ok": True, **profile})


def do_add(args: argparse.Namespace) -> None:
    profile = _load_profile()
    lang = resolve_lang(profile, args.lang)
    store = _load_lang(lang)
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    card = new_card(args.word, args.translation, args.example,
                    args.pos, args.note, tags)
    store["words"][card["id"]] = card
    _save_lang(store)
    emit({"ok": True, "card": card})


def do_add_batch(args: argparse.Namespace) -> None:
    profile = _load_profile()
    lang = resolve_lang(profile, args.lang)
    store = _load_lang(lang)
    try:
        items = json.loads(args.json)
    except json.JSONDecodeError as exc:
        fail(f"--json is not valid JSON: {exc}")
    if not isinstance(items, list):
        fail("--json must be a JSON array of {word, translation, ...} objects")

    existing = {c["word"].strip().lower() for c in store["words"].values()}
    added, skipped = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        word = str(it.get("word", "")).strip()
        translation = str(it.get("translation", "")).strip()
        if not word or not translation:
            continue
        if word.lower() in existing:
            skipped.append(word)
            continue
        tags = it.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        card = new_card(word, translation, str(it.get("example", "")),
                        str(it.get("pos", "")), str(it.get("note", "")), tags)
        store["words"][card["id"]] = card
        existing.add(word.lower())
        added.append(card)
    _save_lang(store)
    emit({"ok": True, "added": len(added), "skipped": skipped, "cards": added})


def _due_cards(store: dict[str, Any], moment: datetime) -> list[dict[str, Any]]:
    out = []
    for card in store["words"].values():
        if card.get("state") == "retired":
            continue
        if int(card.get("reps", 0)) < 1:
            continue  # never studied → surfaced as "new", not "due"
        if from_iso(card["next_review_at"]) <= moment:
            out.append(card)
    out.sort(key=lambda c: c["next_review_at"])
    return out


def do_due(args: argparse.Namespace) -> None:
    profile = _load_profile()
    lang = resolve_lang(profile, args.lang)
    store = _load_lang(lang)
    cards = _due_cards(store, utcnow())
    if args.limit:
        cards = cards[: args.limit]
    emit({"ok": True, "lang": lang, "count": len(cards), "cards": cards})


def do_lesson(args: argparse.Namespace) -> None:
    """Assemble today's queue: due reviews + new words, plus a generate hint.

    The helper cannot invent vocabulary — if there aren't enough unstudied
    cards on file to meet the daily goal, it reports how many more the agent
    should generate (and then add via add-batch).
    """
    profile = _load_profile()
    lang = resolve_lang(profile, args.lang)
    entry = profile["langs"].get(lang, {})
    daily = int(entry.get("daily_goal", 10))
    store = _load_lang(lang)
    now = utcnow()

    due = _due_cards(store, now)
    new_pool = [c for c in store["words"].values()
                if int(c.get("reps", 0)) == 0 and c.get("state") != "retired"]
    new_pool.sort(key=lambda c: c["created_at"])
    new_words = new_pool[:daily]
    generate_more = max(0, daily - len(new_words))

    emit({
        "ok": True,
        "lang": lang,
        "level": entry.get("level"),
        "native": entry.get("native"),
        "daily_goal": daily,
        "streak": profile.get("streak", {}).get("count", 0),
        "due_reviews": due,
        "new_words": new_words,
        "generate_more": generate_more,
    })


def do_rate(args: argparse.Namespace) -> None:
    if args.rating not in RATINGS:
        fail(f"--rating must be one of {', '.join(RATINGS)}")
    profile = _load_profile()
    lang = resolve_lang(profile, args.lang)
    store = _load_lang(lang)
    card = store["words"].get(args.id)
    if not card:
        fail(f"no card with id {args.id} in {lang}")
    if args.user_answer is not None:
        card["last_user_answer"] = args.user_answer
    reschedule(card, args.rating)
    _save_lang(store)
    emit({"ok": True, "card": card})


def do_log_session(args: argparse.Namespace) -> None:
    """Bump the practice streak. Idempotent per calendar day."""
    profile = _load_profile()
    resolve_lang(profile, args.lang)  # validate a language exists
    streak = profile.setdefault("streak", {"count": 0, "last_session": None})
    today = today_str()
    last = streak.get("last_session")
    if last == today:
        pass  # already counted today
    elif last == (utcnow().date() - timedelta(days=1)).isoformat():
        streak["count"] = int(streak.get("count", 0)) + 1
    else:
        streak["count"] = 1
    streak["last_session"] = today
    _save_profile(profile)
    emit({"ok": True, "streak": streak["count"], "date": today})


def do_search(args: argparse.Namespace) -> None:
    profile = _load_profile()
    lang = resolve_lang(profile, args.lang)
    store = _load_lang(lang)
    q = args.query.strip().lower()
    hits = [c for c in store["words"].values()
            if q in c["word"].lower() or q in c["translation"].lower()
            or q in c.get("note", "").lower()]
    emit({"ok": True, "lang": lang, "count": len(hits), "cards": hits})


def do_stats(args: argparse.Namespace) -> None:
    profile = _load_profile()
    lang = resolve_lang(profile, args.lang)
    entry = profile["langs"].get(lang, {})
    store = _load_lang(lang)
    now = utcnow()
    by_state = {"new": 0, "learning": 0, "known": 0, "retired": 0}
    due = 0
    for card in store["words"].values():
        by_state[card.get("state", "new")] = by_state.get(card.get("state", "new"), 0) + 1
        if (card.get("state") != "retired" and int(card.get("reps", 0)) >= 1
                and from_iso(card["next_review_at"]) <= now):
            due += 1
    emit({
        "ok": True,
        "lang": lang,
        "level": entry.get("level"),
        "daily_goal": entry.get("daily_goal"),
        "streak": profile.get("streak", {}).get("count", 0),
        "total": len(store["words"]),
        "by_state": by_state,
        "due_now": due,
    })


def do_export(args: argparse.Namespace) -> None:
    profile = _load_profile()
    lang = resolve_lang(profile, args.lang)
    store = _load_lang(lang)
    out = Path(args.output).expanduser()
    with open(out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        for c in store["words"].values():
            writer.writerow([c["word"], c["translation"], c.get("example", ""),
                             c.get("pos", ""), c.get("note", "")])
    emit({"ok": True, "lang": lang, "exported": len(store["words"]), "path": str(out)})


def do_import(args: argparse.Namespace) -> None:
    profile = _load_profile()
    lang = resolve_lang(profile, args.lang)
    store = _load_lang(lang)
    src = Path(args.file).expanduser()
    if not src.exists():
        fail(f"file not found: {src}")
    existing = {c["word"].strip().lower() for c in store["words"].values()}
    added = 0
    with open(src, encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].strip():
                continue
            word = row[0].strip()
            if word.lower() in existing:
                continue
            translation = row[1].strip() if len(row) > 1 else ""
            example = row[2].strip() if len(row) > 2 else ""
            pos = row[3].strip() if len(row) > 3 else ""
            note = row[4].strip() if len(row) > 4 else ""
            card = new_card(word, translation, example, pos, note, [])
            store["words"][card["id"]] = card
            existing.add(word.lower())
            added += 1
    _save_lang(store)
    emit({"ok": True, "lang": lang, "imported": added})


def do_delete(args: argparse.Namespace) -> None:
    profile = _load_profile()
    lang = resolve_lang(profile, args.lang)
    store = _load_lang(lang)
    if args.id not in store["words"]:
        fail(f"no card with id {args.id} in {lang}")
    removed = store["words"].pop(args.id)
    _save_lang(store)
    emit({"ok": True, "deleted": removed["word"]})


# ── CLI wiring ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Language-tutor store")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("profile", help="View or update the learner profile")
    psub = p.add_subparsers(dest="sub", required=True)
    pset = psub.add_parser("set", help="Set target language / native / level / daily goal")
    pset.add_argument("--lang", required=True, help="Target language code, e.g. es, de, fr")
    pset.add_argument("--native", help="Native language code, e.g. tr, en")
    pset.add_argument("--level", help="CEFR level: A1 A2 B1 B2 C1 C2")
    pset.add_argument("--daily", type=int, help="New words per day (default 10)")
    pset.set_defaults(func=do_profile_set)
    pshow = psub.add_parser("show", help="Show the full profile")
    pshow.set_defaults(func=do_profile_show)

    add = sub.add_parser("add", help="Add one vocabulary item")
    add.add_argument("--lang")
    add.add_argument("--word", required=True)
    add.add_argument("--translation", required=True)
    add.add_argument("--example", default="")
    add.add_argument("--pos", default="", help="Part of speech")
    add.add_argument("--note", default="")
    add.add_argument("--tags", default="", help="Comma-separated")
    add.set_defaults(func=do_add)

    batch = sub.add_parser("add-batch", help="Add many items from a JSON array")
    batch.add_argument("--lang")
    batch.add_argument("--json", required=True,
                       help='[{"word","translation","example","pos","note","tags"}]')
    batch.set_defaults(func=do_add_batch)

    due = sub.add_parser("due", help="List vocabulary due for review")
    due.add_argument("--lang")
    due.add_argument("--limit", type=int, default=0)
    due.set_defaults(func=do_due)

    lesson = sub.add_parser("lesson", help="Assemble today's review + new-word queue")
    lesson.add_argument("--lang")
    lesson.set_defaults(func=do_lesson)

    rate = sub.add_parser("rate", help="Grade a review and reschedule")
    rate.add_argument("--lang")
    rate.add_argument("--id", required=True)
    rate.add_argument("--rating", required=True, help="again|hard|good|easy|retire")
    rate.add_argument("--user-answer", dest="user_answer")
    rate.set_defaults(func=do_rate)

    logs = sub.add_parser("log-session", help="Record a practice session (streak)")
    logs.add_argument("--lang")
    logs.set_defaults(func=do_log_session)

    search = sub.add_parser("search", help="Find words by text")
    search.add_argument("--lang")
    search.add_argument("--query", required=True)
    search.set_defaults(func=do_search)

    stats = sub.add_parser("stats", help="Show progress statistics")
    stats.add_argument("--lang")
    stats.set_defaults(func=do_stats)

    export = sub.add_parser("export", help="Export vocabulary to CSV")
    export.add_argument("--lang")
    export.add_argument("--output", required=True)
    export.set_defaults(func=do_export)

    imp = sub.add_parser("import", help="Import vocabulary from CSV")
    imp.add_argument("--lang")
    imp.add_argument("--file", required=True)
    imp.set_defaults(func=do_import)

    dele = sub.add_parser("delete", help="Delete one card")
    dele.add_argument("--lang")
    dele.add_argument("--id", required=True)
    dele.set_defaults(func=do_delete)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
