#!/usr/bin/env python3
"""Economic-release surprise analyzer — surprise %, z-scores, trend.

Stdlib only. Prints chat-ready markdown.

CSV columns (header, case-insensitive; consensus/prior optional):
    date, indicator, actual, consensus, prior

Modes:
  - Multiple indicators (one row each): a surprise table.
  - One indicator over time (repeated 'indicator', many dates): a trend +
    z-score of the latest actual vs the series' own history, and surprise stats.

Usage:
    surprise.py releases.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import sys


def _num(x):
    if x is None:
        return None
    x = str(x).strip().replace(",", "").replace("%", "").replace("$", "")
    if not x:
        return None
    try:
        return float(x)
    except ValueError:
        return None


def load(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit("empty/headerless CSV")
        norm = {fn: fn.strip().lower() for fn in reader.fieldnames}
        rows = []
        for raw in reader:
            r = {norm[k]: v for k, v in raw.items()}
            rows.append({
                "date": (r.get("date") or "").strip(),
                "indicator": (r.get("indicator") or "series").strip(),
                "actual": _num(r.get("actual")),
                "consensus": _num(r.get("consensus")),
                "prior": _num(r.get("prior")),
            })
    rows = [r for r in rows if r["actual"] is not None]
    if not rows:
        sys.exit("no rows with an 'actual' value")
    return rows


def stdev(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def surprise_label(z):
    if z is None:
        return ""
    az = abs(z)
    if az < 0.5:
        return "noise"
    if az < 1.0:
        return "modest"
    if az < 2.0:
        return "meaningful"
    return "large"


def main():
    ap = argparse.ArgumentParser(description="Economic surprise analyzer")
    ap.add_argument("csv")
    a = ap.parse_args()
    rows = load(a.csv)

    by_ind = {}
    for r in rows:
        by_ind.setdefault(r["indicator"], []).append(r)

    single = len(by_ind) == 1 and len(rows) > 2

    if single:
        ind = rows[0]["indicator"]
        actuals = [r["actual"] for r in rows]
        surprises = [(r["actual"] - r["consensus"]) for r in rows if r["consensus"] is not None]
        sstd = stdev(surprises)
        latest = rows[-1]
        print(f"**{ind}** — {len(rows)} releases ({rows[0]['date']} → {latest['date']})\n")
        # series stats
        astd = stdev(actuals)
        amean = sum(actuals) / len(actuals)
        last = latest["actual"]
        z_actual = (last - amean) / astd if astd else None
        print(f"Latest actual {last:g} (date {latest['date']})"
              + (f" · {z_actual:+.1f}σ vs series mean {amean:.2g}" if z_actual is not None else ""))
        if latest["consensus"] is not None:
            surp = last - latest["consensus"]
            z = surp / sstd if sstd else None
            zt = f", {abs(z):.1f}σ {surprise_label(z)}" if z is not None else ""
            print(f"Surprise vs consensus {latest['consensus']:g}: {surp:+.2g}{zt}")
        if latest["prior"] is not None:
            print(f"Change vs prior {latest['prior']:g}: {last - latest['prior']:+.2g}")
        # trend over last up to 6
        recent = actuals[-6:]
        direction = "rising" if recent[-1] > recent[0] else ("falling" if recent[-1] < recent[0] else "flat")
        print(f"Trend (last {len(recent)}): {direction} ({' → '.join(f'{v:g}' for v in recent)})")
        return

    # Multi-indicator surprise table
    print(f"**Release surprises** ({len(rows)} indicators)\n")
    print("| Indicator | Actual | Cons | Prior | Surprise |")
    print("|-----------|--------|------|-------|----------|")
    for r in rows:
        cons = r["consensus"]
        prior = r["prior"]
        if cons is not None:
            surp = r["actual"] - cons
            mark = "✅" if surp > 0 else ("❌" if surp < 0 else "—")
            surp_txt = f"{surp:+.2g} {mark}"
        else:
            surp_txt = "—"
        print(f"| {r['indicator']} | {r['actual']:g} | "
              f"{cons:g} | {prior:g} | {surp_txt} |"
              if cons is not None and prior is not None else
              f"| {r['indicator']} | {r['actual']:g} | "
              f"{cons if cons is not None else '—'} | "
              f"{prior if prior is not None else '—'} | {surp_txt} |")
    print("\n_Surprise = actual − consensus. ✅ above / ❌ below. "
          "Size with a z-score if a series history is available._")


if __name__ == "__main__":
    main()
