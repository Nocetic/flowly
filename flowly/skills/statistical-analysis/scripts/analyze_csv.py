#!/usr/bin/env python3
"""Dependency-free CSV profiling for scientific data analysis."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


def as_float(value: str) -> float | None:
    value = value.strip()
    if value == "" or value.lower() in {"na", "n/a", "nan", "null", "none"}:
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def quantile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[int(pos)]
    weight = pos - lo
    return sorted_values[lo] * (1 - weight) + sorted_values[hi] * weight


def fmt(value: float | None) -> str:
    if value is None:
        return ""
    if abs(value) >= 1000 or (0 < abs(value) < 0.001):
        return f"{value:.4g}"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    den_x = math.sqrt(sum(x * x for x in dx))
    den_y = math.sqrt(sum(y * y for y in dy))
    if den_x == 0 or den_y == 0:
        return None
    return sum(x * y for x, y in zip(dx, dy)) / (den_x * den_y)


def load_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise SystemExit("CSV has no header row.")
        rows = [{k: (v if v is not None else "") for k, v in row.items()} for row in reader]
        return list(reader.fieldnames), rows


def numeric_columns(headers: list[str], rows: list[dict[str, str]]) -> dict[str, list[float | None]]:
    result: dict[str, list[float | None]] = {}
    for header in headers:
        converted = [as_float(row.get(header, "")) for row in rows]
        non_missing = [x for x in converted if x is not None]
        if non_missing and len(non_missing) >= max(3, len(rows) * 0.5):
            result[header] = converted
    return result


def markdown_table(headers: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    headers = list(headers)
    out = ["| " + " | ".join(headers) + " |"]
    out.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        out.append("| " + " | ".join(str(cell).replace("\n", " ") for cell in row) + " |")
    return "\n".join(out)


def profile(path: Path, by: str | None, outcome: str | None) -> str:
    headers, rows = load_rows(path)
    n = len(rows)
    nums = numeric_columns(headers, rows)

    lines: list[str] = []
    lines.append(f"# CSV Analysis Profile: {path.name}")
    lines.append("")
    lines.append(f"- Rows: {n}")
    lines.append(f"- Columns: {len(headers)}")
    lines.append(f"- Numeric columns inferred: {len(nums)}")
    lines.append("")

    missing_rows = []
    for header in headers:
        missing = sum(1 for row in rows if as_float(row.get(header, "")) is None and row.get(header, "").strip() == "")
        missing_rows.append([header, str(missing), fmt(missing / n * 100 if n else 0) + "%"])
    lines.append("## Missing Values")
    lines.append("")
    lines.append(markdown_table(["Column", "Missing", "Missing %"], missing_rows))
    lines.append("")

    if nums:
        stat_rows = []
        for header, values in nums.items():
            clean = sorted(x for x in values if x is not None)
            sd = statistics.stdev(clean) if len(clean) > 1 else None
            stat_rows.append([
                header,
                str(len(clean)),
                fmt(statistics.fmean(clean)),
                fmt(sd),
                fmt(clean[0]),
                fmt(quantile(clean, 0.25)),
                fmt(quantile(clean, 0.5)),
                fmt(quantile(clean, 0.75)),
                fmt(clean[-1]),
            ])
        lines.append("## Numeric Summary")
        lines.append("")
        lines.append(markdown_table(
            ["Column", "N", "Mean", "SD", "Min", "Q1", "Median", "Q3", "Max"],
            stat_rows,
        ))
        lines.append("")

    categorical = [h for h in headers if h not in nums]
    if categorical:
        cat_rows = []
        for header in categorical[:30]:
            values = [row.get(header, "").strip() for row in rows if row.get(header, "").strip()]
            counts = Counter(values)
            top = ", ".join(f"{k} ({v})" for k, v in counts.most_common(5))
            cat_rows.append([header, str(len(counts)), top])
        lines.append("## Categorical Summary")
        lines.append("")
        lines.append(markdown_table(["Column", "Unique", "Top values"], cat_rows))
        lines.append("")

    if len(nums) >= 2:
        corr_rows = []
        keys = list(nums)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                pairs = [(x, y) for x, y in zip(nums[a], nums[b]) if x is not None and y is not None]
                if len(pairs) < 3:
                    continue
                r = pearson([p[0] for p in pairs], [p[1] for p in pairs])
                if r is not None:
                    corr_rows.append((abs(r), a, b, r, len(pairs)))
        corr_rows.sort(reverse=True)
        lines.append("## Strongest Numeric Correlations")
        lines.append("")
        if corr_rows:
            lines.append(markdown_table(
                ["Column A", "Column B", "Pearson r", "N"],
                ([a, b, fmt(r), str(count)] for _, a, b, r, count in corr_rows[:15]),
            ))
        else:
            lines.append("No numeric column pairs with enough complete observations.")
        lines.append("")

    if by and outcome:
        if by not in headers:
            raise SystemExit(f"--by column not found: {by}")
        if outcome not in nums:
            raise SystemExit(f"--outcome must be an inferred numeric column: {outcome}")
        grouped: dict[str, list[float]] = defaultdict(list)
        for row, value in zip(rows, nums[outcome]):
            group = row.get(by, "").strip() or "(missing)"
            if value is not None:
                grouped[group].append(value)
        group_rows = []
        for group, values in sorted(grouped.items()):
            sd = statistics.stdev(values) if len(values) > 1 else None
            group_rows.append([group, str(len(values)), fmt(statistics.fmean(values)), fmt(sd)])
        lines.append(f"## Grouped Summary: {outcome} by {by}")
        lines.append("")
        lines.append(markdown_table(["Group", "N", "Mean", "SD"], group_rows))
        lines.append("")

    lines.append("## Analysis Notes")
    lines.append("")
    lines.append("- Confirm the experimental unit before inferential tests.")
    lines.append("- Inspect plots and data provenance before interpreting correlations.")
    lines.append("- Report effect sizes and uncertainty, not only p-values.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", help="CSV file to profile")
    parser.add_argument("--out", help="Write Markdown output to this path")
    parser.add_argument("--by", help="Categorical grouping column")
    parser.add_argument("--outcome", help="Numeric outcome for grouped summary")
    args = parser.parse_args()

    text = profile(Path(args.csv_path), args.by, args.outcome)
    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.write_text(text, encoding="utf-8")
        print(out)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
