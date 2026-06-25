#!/usr/bin/env python3
"""Create a study-design protocol canvas."""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "study-design"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title", help="Study or experiment title")
    parser.add_argument("--out", help="Output markdown path")
    parser.add_argument("--design", default="", help="Experiment, observational, benchmark, simulation, etc.")
    args = parser.parse_args()

    path = Path(args.out or f"{slugify(args.title)}-protocol.md").expanduser().resolve()
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing protocol: {path}")

    text = f"""# {args.title}

Created: {date.today().isoformat()}
Design type: {args.design}

## Research Question


## Hypothesis


## Unit Of Analysis


## Population Or System


## Intervention, Exposure, Or Method


## Comparator Or Baseline


## Primary Outcome


## Secondary Outcomes


## Inclusion Criteria


## Exclusion Criteria


## Controls

- Positive control:
- Negative control:
- Baseline:
- Sham/placebo:

## Data Collection


## Analysis Plan


## Power Or Precision Rationale


## Threats To Validity


## Decision Rules


## Ethics, Safety, Or Privacy


## Reproducibility Plan


"""
    path.write_text(text, encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
