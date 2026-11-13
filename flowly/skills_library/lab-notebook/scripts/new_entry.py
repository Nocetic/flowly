#!/usr/bin/env python3
"""Create a dated scientific lab notebook entry."""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "entry"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=".", help="Project directory")
    parser.add_argument("--title", default="research entry", help="Entry title")
    parser.add_argument("--date", default=date.today().isoformat(), help="Entry date YYYY-MM-DD")
    parser.add_argument("--out-dir", default="lab-notebook", help="Notebook directory under project")
    args = parser.parse_args()

    project = Path(args.project).expanduser().resolve()
    out_dir = project / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{args.date}-{slugify(args.title)}.md"
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing entry: {path}")

    text = f"""# {args.date} - {args.title}

## Context


## Objective


## Protocol Or Plan


## Materials, Data, Or Instruments


## Observations


## Results


## Deviations


## Interpretation


## Raw Data And Artifacts


## Next Actions

1.
2.
3.
"""
    path.write_text(text, encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
