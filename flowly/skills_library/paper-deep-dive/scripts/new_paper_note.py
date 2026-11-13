#!/usr/bin/env python3
"""Create a structured scientific paper note."""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:90] or "paper-note"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title", help="Paper title")
    parser.add_argument("--id", default="", help="DOI, PMID, arXiv id, or local id")
    parser.add_argument("--source", default="", help="URL or local path")
    parser.add_argument("--authors", default="", help="Author list")
    parser.add_argument("--year", default="", help="Publication year")
    parser.add_argument("--venue", default="", help="Journal, conference, or source")
    parser.add_argument("--out", default="paper-notes", help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slugify(args.title)}.md"

    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing note: {path}")

    today = date.today().isoformat()
    content = f"""# {args.title}

## Metadata

- ID: {args.id}
- Authors: {args.authors}
- Year: {args.year}
- Venue: {args.venue}
- Source: {args.source}
- Note created: {today}

## One-Sentence Takeaway


## Problem


## Claimed Contributions

1.
2.
3.

## Method

- Design:
- Data/sample:
- Controls/baselines:
- Metrics/outcomes:
- Assumptions:

## Evidence

| Claim | Evidence | Figure/table | Strength | Notes |
| --- | --- | --- | --- | --- |

## Figure And Table Notes

### Figure 1


## Limitations

- Stated by authors:
- Inferred:

## Reproducibility

- Code:
- Data:
- Environment:
- Compute/instruments:
- Missing details:

## Related Work Position


## Follow-Up Questions

1.
2.
3.
"""
    path.write_text(content, encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
