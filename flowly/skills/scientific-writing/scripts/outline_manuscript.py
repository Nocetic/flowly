#!/usr/bin/env python3
"""Create scientific manuscript, abstract, or rebuttal outlines."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "scientific-writing"


def article(title: str) -> str:
    return f"""# {title}

## Abstract

### Problem


### Gap


### Method


### Main Result


### Interpretation


## Introduction


## Methods


## Results


## Discussion


## Limitations


## Conclusion


"""


def abstract(title: str) -> str:
    return f"""# Abstract: {title}

## Background


## Objective


## Methods


## Results


## Conclusion


## Keywords


"""


def rebuttal(title: str) -> str:
    return f"""# Response To Reviewers: {title}

We thank the reviewers for their careful reading and constructive comments. Below we respond point by point.

## Summary Of Major Changes

1.
2.
3.

## Reviewer 1

### Comment 1

> Reviewer comment excerpt.

**Response.**

**Change made.**

## Reviewer 2


## Remaining Limitations


"""


TEMPLATES = {
    "article": article,
    "preprint": article,
    "abstract": abstract,
    "rebuttal": rebuttal,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title", help="Scientific artifact title")
    parser.add_argument("--kind", choices=sorted(TEMPLATES), default="article")
    parser.add_argument("--out", help="Output markdown path")
    args = parser.parse_args()

    path = Path(args.out or f"{slugify(args.title)}-{args.kind}.md").expanduser().resolve()
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing outline: {path}")

    path.write_text(TEMPLATES[args.kind](args.title), encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
