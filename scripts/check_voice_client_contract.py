#!/usr/bin/env python3
"""Fail when a client's copy of the live-voice request contract differs
from Core's canonical one.

    python scripts/check_voice_client_contract.py PATH_TO_COPY [PATH_TO_COPY ...]

Desktop keeps src/renderer/src/lib/live-voice/contracts/live-voice-client-requests.json,
iOS FlowlyTests/Contracts/live-voice-client-requests.json; both must be
byte-identical to tests/fixtures/live_voice_client_requests.json.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

CANONICAL = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "live_voice_client_requests.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(paths: list[str]) -> int:
    expected = digest(CANONICAL)
    stale = [path for path in paths if not Path(path).is_file() or digest(Path(path)) != expected]
    for path in paths:
        print(f"{'ok   ' if path not in stale else 'STALE'} {path}")
    if stale:
        print(f"Copy {CANONICAL} over the stale files, then update the client tests if a shape changed.")
    return 1 if stale or not paths else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
