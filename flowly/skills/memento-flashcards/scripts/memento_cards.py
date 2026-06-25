#!/usr/bin/env python3
"""Memento flashcard store: persistence, review scheduling, and CSV transfer.

Standard library only. Every subcommand prints a JSON object so the calling
agent can parse it. The deck lives at
$FLOWLY_HOME/skills/memento-flashcards/data/cards.json
"""

import argparse
import csv
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("FLOWLY_HOME", Path.home() / ".flowly"))
STORE_DIR = HOME / "skills" / "memento-flashcards" / "data"
STORE_PATH = STORE_DIR / "cards.json"

# Far-future timestamp parked on retired cards so they never come due.
NEVER = "9999-12-31T23:59:59+00:00"

# Interval (in days) granted by each non-retire rating, and whether the rating
# extends the consecutive-easy streak.
SCHEDULE = {
    "hard": (1, False),
    "good": (3, False),
    "easy": (7, True),
}
STREAK_TO_RETIRE = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(when: datetime) -> str:
    return when.isoformat()


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def blank_deck() -> dict:
    return {"cards": [], "version": 1}


def make_card(question: str, answer: str, collection: str, video_id=None) -> dict:
    """Build a fresh card record, due immediately."""
    stamp = to_iso(utcnow())
    return {
        "id": str(uuid.uuid4()),
        "question": question,
        "answer": answer,
        "collection": collection,
        "status": "learning",
        "ease_streak": 0,
        "next_review_at": stamp,
        "created_at": stamp,
        "video_id": video_id,
        "last_user_answer": None,
    }


def read_deck() -> dict:
    """Load the deck, tolerating a missing or corrupt file by returning empty."""
    if not STORE_PATH.exists():
        return blank_deck()
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return blank_deck()
    if not isinstance(loaded, dict) or "cards" not in loaded:
        return blank_deck()
    return loaded


def write_deck(deck: dict) -> None:
    """Persist the deck atomically: serialize to a temp file, then rename."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=STORE_DIR, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(deck, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(temp_name, STORE_PATH)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def emit(payload: object) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def fail(message: str) -> None:
    emit({"ok": False, "error": message})
    sys.exit(1)


def find_card(deck: dict, card_id: str):
    for card in deck["cards"]:
        if card["id"] == card_id:
            return card
    return None


# --- subcommands -------------------------------------------------------------

def do_add(args: argparse.Namespace) -> None:
    deck = read_deck()
    card = make_card(args.question, args.answer, args.collection or "General")
    deck["cards"].append(card)
    write_deck(deck)
    emit({"ok": True, "card": card})


def do_add_quiz(args: argparse.Namespace) -> None:
    deck = read_deck()

    try:
        items = json.loads(args.questions)
    except json.JSONDecodeError as err:
        fail(f"Invalid JSON for --questions: {err}")
        return

    # One quiz per video: if this video already produced cards, leave them be.
    already = [c for c in deck["cards"] if c.get("video_id") == args.video_id]
    if already:
        emit({
            "ok": True,
            "skipped": True,
            "reason": "duplicate_video_id",
            "existing_count": len(already),
            "cards": already,
        })
        return

    collection = args.collection or "Quiz"
    added = []
    for item in items:
        card = make_card(item["question"], item["answer"], collection, video_id=args.video_id)
        deck["cards"].append(card)
        added.append(card)

    write_deck(deck)
    emit({"ok": True, "created_count": len(added), "cards": added})


def do_due(args: argparse.Namespace) -> None:
    deck = read_deck()
    moment = utcnow()
    due = []
    for card in deck["cards"]:
        if card["status"] == "retired":
            continue
        if from_iso(card["next_review_at"]) > moment:
            continue
        if args.collection and card["collection"] != args.collection:
            continue
        due.append(card)
    emit({"ok": True, "count": len(due), "cards": due})


def do_rate(args: argparse.Namespace) -> None:
    deck = read_deck()
    card = find_card(deck, args.id)
    if card is None:
        fail(f"Card not found: {args.id}")
        return

    if args.user_answer is not None:
        card["last_user_answer"] = args.user_answer

    if args.rating == "retire":
        card["status"] = "retired"
        card["next_review_at"] = NEVER
        card["ease_streak"] = 0
    else:
        days, builds_streak = SCHEDULE[args.rating]
        card["next_review_at"] = to_iso(utcnow() + timedelta(days=days))
        if builds_streak:
            card["ease_streak"] = card.get("ease_streak", 0) + 1
            if card["ease_streak"] >= STREAK_TO_RETIRE:
                card["status"] = "retired"
        else:
            card["ease_streak"] = 0

    write_deck(deck)
    emit({"ok": True, "card": card})


def do_list(args: argparse.Namespace) -> None:
    deck = read_deck()
    cards = deck["cards"]
    if args.collection:
        cards = [c for c in cards if c["collection"] == args.collection]
    if args.status:
        cards = [c for c in cards if c["status"] == args.status]
    emit({"ok": True, "count": len(cards), "cards": cards})


def do_stats(args: argparse.Namespace) -> None:
    deck = read_deck()
    moment = utcnow()
    cards = deck["cards"]

    learning = 0
    retired = 0
    due_now = 0
    by_collection: dict[str, int] = {}

    for card in cards:
        if card["status"] == "retired":
            retired += 1
        else:
            learning += 1
            if from_iso(card["next_review_at"]) <= moment:
                due_now += 1
        by_collection[card["collection"]] = by_collection.get(card["collection"], 0) + 1

    emit({
        "ok": True,
        "total": len(cards),
        "learning": learning,
        "retired": retired,
        "due_now": due_now,
        "collections": by_collection,
    })


def do_export(args: argparse.Namespace) -> None:
    deck = read_deck()
    target = Path(args.output).expanduser()
    with open(target, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        for card in deck["cards"]:
            writer.writerow([card["question"], card["answer"], card["collection"]])
    emit({"ok": True, "exported": len(deck["cards"]), "path": str(target)})


def do_import(args: argparse.Namespace) -> None:
    source = Path(args.file).expanduser()
    if not source.exists():
        fail(f"File not found: {source}")
        return

    deck = read_deck()
    fallback = args.collection or "Imported"
    count = 0

    with open(source, "r", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < 2:
                continue
            question = row[0].strip()
            answer = row[1].strip()
            if not question or not answer:
                continue
            collection = row[2].strip() if len(row) >= 3 and row[2].strip() else fallback
            deck["cards"].append(make_card(question, answer, collection))
            count += 1

    write_deck(deck)
    emit({"ok": True, "imported": count})


def do_delete(args: argparse.Namespace) -> None:
    deck = read_deck()
    before = len(deck["cards"])
    deck["cards"] = [c for c in deck["cards"] if c["id"] != args.id]
    if len(deck["cards"]) == before:
        fail(f"Card not found: {args.id}")
        return
    write_deck(deck)
    emit({"ok": True, "deleted": args.id})


def do_delete_collection(args: argparse.Namespace) -> None:
    deck = read_deck()
    before = len(deck["cards"])
    deck["cards"] = [c for c in deck["cards"] if c["collection"] != args.collection]
    write_deck(deck)
    emit({"ok": True, "deleted_count": before - len(deck["cards"]), "collection": args.collection})


# --- argument wiring ---------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Memento flashcard manager")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Create one card")
    add.add_argument("--question", required=True)
    add.add_argument("--answer", required=True)
    add.add_argument("--collection", default="General")
    add.set_defaults(func=do_add)

    quiz = sub.add_parser("add-quiz", help="Batch-add quiz cards")
    quiz.add_argument("--video-id", required=True)
    quiz.add_argument("--questions", required=True, help="JSON array of {question, answer}")
    quiz.add_argument("--collection", default="Quiz")
    quiz.set_defaults(func=do_add_quiz)

    due = sub.add_parser("due", help="List due cards")
    due.add_argument("--collection", default=None)
    due.set_defaults(func=do_due)

    rate = sub.add_parser("rate", help="Rate a card")
    rate.add_argument("--id", required=True)
    rate.add_argument("--rating", required=True, choices=["easy", "good", "hard", "retire"])
    rate.add_argument("--user-answer", default=None)
    rate.set_defaults(func=do_rate)

    listing = sub.add_parser("list", help="List cards")
    listing.add_argument("--collection", default=None)
    listing.add_argument("--status", default=None, choices=["learning", "retired"])
    listing.set_defaults(func=do_list)

    stats = sub.add_parser("stats", help="Show statistics")
    stats.set_defaults(func=do_stats)

    export = sub.add_parser("export", help="Export cards to CSV")
    export.add_argument("--output", required=True)
    export.set_defaults(func=do_export)

    imp = sub.add_parser("import", help="Import cards from CSV")
    imp.add_argument("--file", required=True)
    imp.add_argument("--collection", default="Imported")
    imp.set_defaults(func=do_import)

    delete = sub.add_parser("delete", help="Delete one card")
    delete.add_argument("--id", required=True)
    delete.set_defaults(func=do_delete)

    delcol = sub.add_parser("delete-collection", help="Delete all cards in a collection")
    delcol.add_argument("--collection", required=True)
    delcol.set_defaults(func=do_delete_collection)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
