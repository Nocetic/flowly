#!/usr/bin/env python3
"""How calls started with the agent's memory, from gateway.log.

    python scripts/voice_memory_report.py [--since 2026-10-04] [--log PATH]

Counts the snapshot and recall lines the gateway writes (shape only) and
flags refusals: a refused snapshot means a call started without the
agent's memory.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

LINE = re.compile(r"^(?P<time>\S+ \S+) \|.*Live Voice (?P<what>memory snapshot|recall) (?P<outcome>served|refused|requested before)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=str(Path.home() / ".flowly" / "logs" / "gateway.log"))
    parser.add_argument("--since", default="", help="ISO date or datetime prefix")
    args = parser.parse_args()
    counts: Counter[tuple[str, str]] = Counter()
    last_refusal = ""
    for raw in Path(args.log).read_text(encoding="utf-8", errors="replace").splitlines():
        match = LINE.match(raw)
        if not match or match["time"] < args.since:
            continue
        counts[(match["what"], match["outcome"])] += 1
        if match["outcome"] != "served":
            last_refusal = raw[:240]
    for (what, outcome), count in sorted(counts.items()):
        print(f"{what:<16} {outcome:<18} {count}")
    if not counts:
        print("no Live Voice memory lines (gateway older than the logging, or no calls)")
    if last_refusal:
        print(f"last problem: {last_refusal}")
    return 1 if last_refusal and counts[("memory snapshot", "refused")] else 0


if __name__ == "__main__":
    sys.exit(main())
