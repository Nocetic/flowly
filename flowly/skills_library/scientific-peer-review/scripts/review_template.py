#!/usr/bin/env python3
"""Create a scientific peer-review report scaffold."""

from __future__ import annotations

import argparse
from pathlib import Path


SECTIONS = {
    "article": ["Summary", "Major Issues", "Minor Issues", "Methods", "Evidence", "Statistics", "Reproducibility", "Recommendation"],
    "grant": ["Summary", "Significance", "Innovation", "Approach", "Feasibility", "Risks", "Budget Or Resources", "Recommendation"],
    "dataset": ["Summary", "Provenance", "Documentation", "Coverage", "Bias And Limitations", "Licensing And Privacy", "Usability", "Recommendation"],
    "code": ["Summary", "Reproducibility", "Correctness", "Tests", "Data Handling", "Documentation", "Risks", "Recommendation"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title", help="Artifact title")
    parser.add_argument("--type", choices=sorted(SECTIONS), default="article", help="Review type")
    parser.add_argument("--out", default="scientific-review.md", help="Output markdown path")
    args = parser.parse_args()

    path = Path(args.out).expanduser().resolve()
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing review: {path}")

    lines = [
        f"# Scientific Review: {args.title}",
        "",
        f"Artifact type: {args.type}",
        "",
        "## Reviewer Stance",
        "",
        "Prioritize scientific correctness, evidence strength, methods, statistics, and reproducibility before style.",
        "",
    ]
    for section in SECTIONS[args.type]:
        lines.extend([f"## {section}", "", ""])
    lines.extend([
        "## Actionable Requests",
        "",
        "| Priority | Request | Rationale |",
        "| --- | --- | --- |",
        "",
    ])

    path.write_text("\n".join(lines), encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
