#!/usr/bin/env python3
"""Create a literature-review workspace with standard files."""

from __future__ import annotations

import argparse
import csv
import re
from datetime import date
from pathlib import Path


MATRIX_COLUMNS = [
    "id",
    "title",
    "year",
    "authors",
    "venue",
    "source_url",
    "study_type",
    "sample_or_dataset",
    "method",
    "outcomes",
    "main_claim",
    "evidence_strength",
    "limitations",
    "relevance",
    "notes",
]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "literature-review"


def write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.write_text(content, encoding="utf-8")
    return True


def create_matrix(path: Path) -> bool:
    if path.exists():
        return False
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MATRIX_COLUMNS)
        writer.writeheader()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic", help="Review topic or research question")
    parser.add_argument("--out", help="Output directory. Defaults to ./review-<topic>.")
    parser.add_argument("--question", help="Optional precise review question")
    args = parser.parse_args()

    out = Path(args.out or f"review-{slugify(args.topic)}").expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    created: list[str] = []

    if create_matrix(out / "papers.csv"):
        created.append("papers.csv")

    files = {
        "search-strategy.md": f"""# Search Strategy

Topic: {args.topic}
Review question: {args.question or ""}
Created: {today}

## Databases

- arXiv:
- PubMed:
- Semantic Scholar:
- Google Scholar:
- Domain-specific sources:

## Query Strings

| Source | Query | Date | Results | Notes |
| --- | --- | --- | --- | --- |

## Inclusion Criteria

-

## Exclusion Criteria

-

## Snowballing

- Backward citations:
- Forward citations:
""",
        "screening.md": """# Screening Log

| Paper ID | Decision | Reason | Screener | Date |
| --- | --- | --- | --- | --- |

Decision values: include, exclude, maybe, duplicate.
""",
        "evidence-map.md": """# Evidence Map

## Converging Findings


## Conflicting Findings


## Weak Or Indirect Evidence


## Gaps


## High-Value Sources


""",
        "synthesis.md": f"""# Literature Review Synthesis

Topic: {args.topic}
Search date: {today}

## Scope


## Bottom Line


## Evidence Summary


## Limitations Of The Evidence


## Open Questions


## References


""",
    }

    for name, content in files.items():
        if write_if_missing(out / name, content):
            created.append(name)

    print(f"Review workspace: {out}")
    print("Created: " + (", ".join(created) if created else "nothing new"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
