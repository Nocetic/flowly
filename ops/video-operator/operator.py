#!/usr/bin/env python3
"""Durable run ledger for Flowly's YouTube channel experiment.

The agent and its connected media tools do the creative work. This module owns
only deterministic orchestration state:

    PLANNED -> PRODUCING -> READY -> PUBLISHING -> PUBLISHED

It deliberately keeps production and publication separate. A finished video
waits in READY until its publication slot. A late or incomplete video never
gets published merely because a timer fired.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DB_PATH = ROOT / "operator.sqlite3"
RUNS_DIR = ROOT / "runs"

STATUSES = {
    "PLANNED",
    "PRODUCING",
    "READY",
    "PUBLISHING",
    "PUBLISHED",
    "LATE",
    "FAILED",
    "SKIPPED",
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def emit(payload: dict, *, code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"missing config: {CONFIG_PATH}")
    data = json.loads(CONFIG_PATH.read_text())
    required = {
        "timezone",
        "publishTimes",
        "productionLeadHours",
        "publishGraceMinutes",
        "language",
        "durationSeconds",
        "visibility",
    }
    missing = sorted(required - data.keys())
    if missing:
        raise RuntimeError(f"config is missing: {', '.join(missing)}")
    ZoneInfo(str(data["timezone"]))
    if not data["publishTimes"]:
        raise RuntimeError("publishTimes must not be empty")
    return data


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                publish_at TEXT NOT NULL UNIQUE,
                production_at TEXT NOT NULL,
                status TEXT NOT NULL,
                run_dir TEXT NOT NULL,
                production_attempts INTEGER NOT NULL DEFAULT 0,
                publish_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                youtube_video_id TEXT,
                youtube_url TEXT,
                actual_visibility TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runs_status_publish_idx
                ON runs(status, publish_at);
            CREATE INDEX IF NOT EXISTS runs_status_production_idx
                ON runs(status, production_at);
            """
        )


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def run_paths(run_id: str) -> dict:
    run_dir = RUNS_DIR / run_id
    return {
        "runDir": str(run_dir),
        "brief": str(run_dir / "brief.md"),
        "script": str(run_dir / "script.json"),
        "voice": str(run_dir / "voice.mp3"),
        "music": str(run_dir / "music.mp3"),
        "assetsDir": str(run_dir / "assets"),
        "video": str(run_dir / "final.mp4"),
        "thumbnail": str(run_dir / "thumbnail.png"),
        "metadata": str(run_dir / "metadata.json"),
        "report": str(run_dir / "report.json"),
    }


def create_run_files(run_id: str, publish_at: datetime, config: dict) -> None:
    paths = run_paths(run_id)
    Path(paths["assetsDir"]).mkdir(parents=True, exist_ok=True)
    contract = {
        "runId": run_id,
        "publishAt": iso(publish_at),
        "status": "PLANNED",
        "language": config["language"],
        "durationSeconds": config["durationSeconds"],
        "resolution": config.get("resolution", "1080x1920"),
        "visibility": config["visibility"],
        "aiDisclosure": bool(config.get("aiDisclosure", True)),
        "idempotencyTag": f"flowly-run-{run_id}",
    }
    Path(paths["report"]).write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n"
    )


def schedule(days: int) -> None:
    init_db()
    config = load_config()
    tz = ZoneInfo(str(config["timezone"]))
    current = now_utc()
    local_today = current.astimezone(tz).date()
    lead = timedelta(hours=float(config["productionLeadHours"]))
    candidates: list[datetime] = []

    day_offset = 0
    while len(candidates) < days * len(config["publishTimes"]):
        local_day = local_today + timedelta(days=day_offset)
        for hhmm in config["publishTimes"]:
            hour, minute = [int(part) for part in str(hhmm).split(":", 1)]
            local_dt = datetime(
                local_day.year,
                local_day.month,
                local_day.day,
                hour,
                minute,
                tzinfo=tz,
            )
            publish_at = local_dt.astimezone(UTC)
            if publish_at > current + timedelta(minutes=30):
                candidates.append(publish_at)
        day_offset += 1

    candidates = candidates[: days * len(config["publishTimes"])]
    created: list[dict] = []
    with db() as conn:
        for publish_at in candidates:
            local_stamp = publish_at.astimezone(tz).strftime("%Y%m%d-%H%M")
            run_id = f"yt-{local_stamp}"
            run_dir = RUNS_DIR / run_id
            created_at = iso(current)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO runs (
                    id, publish_at, production_at, status, run_dir,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'PLANNED', ?, ?, ?)
                """,
                (
                    run_id,
                    iso(publish_at),
                    iso(publish_at - lead),
                    str(run_dir),
                    created_at,
                    created_at,
                ),
            )
            if cursor.rowcount:
                create_run_files(run_id, publish_at, config)
                created.append(
                    {
                        "runId": run_id,
                        "productionAt": iso(publish_at - lead),
                        "publishAt": iso(publish_at),
                    }
                )
    emit({"ok": True, "created": created, "count": len(created)})


def claim_production() -> None:
    init_db()
    current = iso(now_utc())
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM runs
            WHERE status = 'PLANNED' AND production_at <= ?
            ORDER BY production_at
            LIMIT 1
            """,
            (current,),
        ).fetchone()
        if row is None:
            emit({"ok": True, "action": "none", "wakeAgent": False})
        conn.execute(
            """
            UPDATE runs
            SET status = 'PRODUCING',
                production_attempts = production_attempts + 1,
                updated_at = ?
            WHERE id = ? AND status = 'PLANNED'
            """,
            (current, row["id"]),
        )
        claimed = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (row["id"],)
        ).fetchone()
    emit(
        {
            "ok": True,
            "action": "produce",
            "run": row_dict(claimed),
            "paths": run_paths(claimed["id"]),
            "config": load_config(),
            "wakeAgent": True,
        }
    )


def probe_video(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffprobe failed")
    data = json.loads(proc.stdout)
    streams = data.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not video_streams:
        raise RuntimeError("final.mp4 has no video stream")
    if not audio_streams:
        raise RuntimeError("final.mp4 has no audio stream")
    duration = float((data.get("format") or {}).get("duration") or 0)
    if duration < 10 or duration > 90:
        raise RuntimeError(f"unexpected video duration: {duration:.2f}s")
    first = video_streams[0]
    return {
        "duration": round(duration, 3),
        "width": first.get("width"),
        "height": first.get("height"),
        "hasAudio": True,
    }


def mark_ready(run_id: str) -> None:
    init_db()
    paths = run_paths(run_id)
    required = [
        Path(paths["brief"]),
        Path(paths["script"]),
        Path(paths["voice"]),
        Path(paths["video"]),
        Path(paths["thumbnail"]),
        Path(paths["metadata"]),
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        emit({"ok": False, "error": "missing artifacts", "missing": missing}, code=2)

    try:
        probe = probe_video(Path(paths["video"]))
        metadata = json.loads(Path(paths["metadata"]).read_text())
        for key in ("title", "description"):
            if not str(metadata.get(key) or "").strip():
                raise RuntimeError(f"metadata is missing {key}")
    except Exception as exc:
        emit({"ok": False, "error": str(exc)}, code=2)

    current_dt = now_utc()
    config = load_config()
    with db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            emit({"ok": False, "error": f"unknown run: {run_id}"}, code=2)
        if row["status"] not in {"PRODUCING", "FAILED"}:
            emit(
                {
                    "ok": False,
                    "error": f"cannot mark READY from {row['status']}",
                },
                code=2,
            )
        deadline = parse_iso(row["publish_at"]) + timedelta(
            minutes=int(config["publishGraceMinutes"])
        )
        status = "READY" if current_dt <= deadline else "LATE"
        conn.execute(
            "UPDATE runs SET status = ?, last_error = NULL, updated_at = ? WHERE id = ?",
            (status, iso(current_dt), run_id),
        )
    report_path = Path(paths["report"])
    report = json.loads(report_path.read_text())
    report.update({"status": status, "qualityProbe": probe, "readyAt": iso(current_dt)})
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    emit({"ok": status == "READY", "status": status, "probe": probe})


def claim_publish() -> None:
    init_db()
    current_dt = now_utc()
    current = iso(current_dt)
    config = load_config()
    stale_before = iso(current_dt - timedelta(minutes=20))
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM runs
            WHERE (
                status = 'READY' AND publish_at <= ?
            ) OR (
                status = 'PUBLISHING' AND updated_at <= ?
            )
            ORDER BY publish_at
            LIMIT 1
            """,
            (current, stale_before),
        ).fetchone()
        if row is None:
            due_incomplete = conn.execute(
                """
                SELECT * FROM runs
                WHERE status IN ('PLANNED', 'PRODUCING', 'FAILED')
                  AND publish_at <= ?
                ORDER BY publish_at
                LIMIT 1
                """,
                (current,),
            ).fetchone()
            if due_incomplete is None:
                emit({"ok": True, "action": "none", "wakeAgent": False})
            deadline = parse_iso(due_incomplete["publish_at"]) + timedelta(
                minutes=int(config["publishGraceMinutes"])
            )
            if current_dt > deadline:
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'SKIPPED',
                        last_error = 'publication slot missed before video became ready',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (current, due_incomplete["id"]),
                )
                emit(
                    {
                        "ok": False,
                        "action": "skipped",
                        "runId": due_incomplete["id"],
                        "wakeAgent": True,
                    }
                )
            emit(
                {
                    "ok": False,
                    "action": "retry_later",
                    "runId": due_incomplete["id"],
                    "deadline": iso(deadline),
                    "wakeAgent": True,
                }
            )

        recovery = row["status"] == "PUBLISHING"
        conn.execute(
            """
            UPDATE runs
            SET status = 'PUBLISHING',
                publish_attempts = publish_attempts + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (current, row["id"]),
        )
        claimed = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (row["id"],)
        ).fetchone()
    emit(
        {
            "ok": True,
            "action": "publish",
            "recovery": recovery,
            "run": row_dict(claimed),
            "paths": run_paths(claimed["id"]),
            "idempotencyTag": f"flowly-run-{claimed['id']}",
            "wakeAgent": True,
        }
    )


def mark_published(
    run_id: str,
    video_id: str,
    url: str | None,
    visibility: str,
) -> None:
    init_db()
    if not video_id.strip():
        emit({"ok": False, "error": "video_id is required"}, code=2)
    with db() as conn:
        row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            emit({"ok": False, "error": f"unknown run: {run_id}"}, code=2)
        if row["status"] not in {"PUBLISHING", "PUBLISHED"}:
            emit(
                {
                    "ok": False,
                    "error": f"cannot mark PUBLISHED from {row['status']}",
                },
                code=2,
            )
        conn.execute(
            """
            UPDATE runs
            SET status = 'PUBLISHED',
                youtube_video_id = ?,
                youtube_url = ?,
                actual_visibility = ?,
                last_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (video_id, url, visibility, iso(now_utc()), run_id),
        )
    paths = run_paths(run_id)
    report_path = Path(paths["report"])
    report = json.loads(report_path.read_text())
    report.update(
        {
            "status": "PUBLISHED",
            "youtubeVideoId": video_id,
            "youtubeUrl": url,
            "actualVisibility": visibility,
            "publishedAt": iso(now_utc()),
        }
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    emit({"ok": True, "status": "PUBLISHED", "runId": run_id, "videoId": video_id})


def fail(run_id: str, stage: str, error: str) -> None:
    init_db()
    error = error.strip()[:1000] or "unknown error"
    target = "FAILED" if stage == "production" else "READY"
    with db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            emit({"ok": False, "error": f"unknown run: {run_id}"}, code=2)
        conn.execute(
            "UPDATE runs SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (target, error, iso(now_utc()), run_id),
        )
    emit(
        {
            "ok": False,
            "status": target,
            "stage": stage,
            "error": error,
            "retryBackoffSeconds": [2, 4, 8],
        }
    )


def status() -> None:
    init_db()
    with db() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY publish_at").fetchall()
    emit({"ok": True, "runs": [row_dict(row) for row in rows]})


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    schedule_parser = sub.add_parser("schedule")
    schedule_parser.add_argument("--days", type=int, default=7)
    sub.add_parser("claim-production")
    ready_parser = sub.add_parser("mark-ready")
    ready_parser.add_argument("--run", required=True)
    sub.add_parser("claim-publish")
    published_parser = sub.add_parser("mark-published")
    published_parser.add_argument("--run", required=True)
    published_parser.add_argument("--video-id", required=True)
    published_parser.add_argument("--url")
    published_parser.add_argument("--visibility", default="public")
    fail_parser = sub.add_parser("fail")
    fail_parser.add_argument("--run", required=True)
    fail_parser.add_argument(
        "--stage", required=True, choices=["production", "publication"]
    )
    fail_parser.add_argument("--error", required=True)
    sub.add_parser("status")

    args = parser.parse_args()
    try:
        if args.command == "init":
            init_db()
            emit({"ok": True, "root": str(ROOT), "statuses": sorted(STATUSES)})
        if args.command == "schedule":
            schedule(max(1, min(args.days, 31)))
        if args.command == "claim-production":
            claim_production()
        if args.command == "mark-ready":
            mark_ready(args.run)
        if args.command == "claim-publish":
            claim_publish()
        if args.command == "mark-published":
            mark_published(
                args.run,
                args.video_id,
                args.url,
                args.visibility,
            )
        if args.command == "fail":
            fail(args.run, args.stage, args.error)
        if args.command == "status":
            status()
    except Exception as exc:
        emit({"ok": False, "error": str(exc)}, code=1)


if __name__ == "__main__":
    main()
