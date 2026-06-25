#!/usr/bin/env python3
"""Pull a YouTube transcript so the agent can write quiz questions from it.

Needs the youtube-transcript-api package (pip install youtube-transcript-api).
This script only retrieves and flattens the transcript text; composing the
actual quiz is the agent's job.
"""

import argparse
import json
import re
import sys


def emit(payload: object) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def flatten(segments) -> str:
    """Join transcript segments into one whitespace-collapsed string."""
    pieces = [str(seg.get("text", "")).strip() for seg in segments]
    joined = " ".join(p for p in pieces if p)
    return re.sub(r"\s+", " ", joined).strip()


def fetch_transcript(video_id: str):
    """Return flattened transcript text, or emit an error JSON and exit."""
    try:
        import youtube_transcript_api
    except ImportError:
        emit({
            "ok": False,
            "error": "missing_dependency",
            "message": "Run: pip install youtube-transcript-api",
        })
        sys.exit(1)

    english = ["en", "en-US", "en-GB", "en-CA", "en-AU"]
    try:
        result = youtube_transcript_api.YouTubeTranscriptApi().fetch(
            video_id, languages=english
        )
    except Exception as err:
        emit({
            "ok": False,
            "error": "transcript_unavailable",
            "error_type": type(err).__name__,
            "message": f"Could not fetch transcript for {video_id}: {err}",
        })
        sys.exit(1)

    # Newer API versions return an object exposing to_raw_data(); older ones
    # already hand back a list of segment dicts.
    segments = result.to_raw_data() if hasattr(result, "to_raw_data") else result

    text = flatten(segments)
    if not text:
        emit({
            "ok": False,
            "error": "empty_transcript",
            "message": f"Transcript for {video_id} contained no usable text.",
        })
        sys.exit(1)

    return text


def do_fetch(args: argparse.Namespace) -> None:
    text = fetch_transcript(args.video_id)
    emit({"ok": True, "video_id": args.video_id, "transcript": text})


def main() -> None:
    parser = argparse.ArgumentParser(description="Memento YouTube transcript fetcher")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="Fetch transcript for a video")
    fetch.add_argument("video_id", help="YouTube video ID")
    fetch.set_defaults(func=do_fetch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
