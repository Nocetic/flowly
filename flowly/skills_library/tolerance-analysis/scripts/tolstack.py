#!/usr/bin/env python3
"""Tolerance stack-up calculator — worst-case + statistical (RSS), fits, Cpk.
Stdlib only. Chat-ready markdown.

Contributor format: nominal:tol  (prefix the nominal with - if it SUBTRACTS
from the gap). tol is the symmetric ± value.

Usage:
    tolstack.py stack 50:0.1 -30:0.05 -19.5:0.05
    tolstack.py stack --csv dims.csv          # columns: nominal,tol[,sign]
    tolstack.py fit --hole 10:0.015 --shaft 10:-0.01:0.006
    tolstack.py cpk --tol 0.1 --sigma 0.02 [--offset 0.0]
"""
from __future__ import annotations

import argparse
import csv
import math
import sys


def parse_contrib(tok):
    parts = tok.split(":")
    nominal = float(parts[0])
    tol = abs(float(parts[1]))
    return nominal, tol


def cmd_stack(a):
    items = []
    if a.csv:
        with open(a.csv, newline="", encoding="utf-8-sig") as f:
            r = csv.reader(f)
            rows = list(r)
        start = 1 if rows and not _isnum(rows[0][0]) else 0
        for row in rows[start:]:
            if not row or not row[0].strip():
                continue
            nominal = float(row[0]); tol = abs(float(row[1]))
            if len(row) > 2 and row[2].strip() in ("-", "-1", "neg"):
                nominal = -abs(nominal)
            items.append((nominal, tol))
    else:
        items = [parse_contrib(t) for t in a.items]
    if not items:
        sys.exit("no contributors")

    nominal_gap = sum(n for n, _ in items)
    wc_tol = sum(t for _, t in items)
    rss_tol = math.sqrt(sum(t * t for _, t in items))

    print(f"**Tolerance stack-up** ({len(items)} contributors)\n")
    print(f"Nominal gap: {nominal_gap:.4g}")
    print(f"Worst-case: ±{wc_tol:.4g} → [{nominal_gap-wc_tol:.4g}, {nominal_gap+wc_tol:.4g}]  "
          f"(guaranteed range, tight tols)")
    print(f"RSS (±3σ):  ±{rss_tol:.4g} → [{nominal_gap-rss_tol:.4g}, {nominal_gap+rss_tol:.4g}]  "
          f"(looser tols, ~0.27% beyond if off-center)")

    # fit verdict
    if nominal_gap - wc_tol > 0:
        print("\n✅ Gap never closes at worst-case → assembly guaranteed.")
    elif nominal_gap - rss_tol > 0:
        print("\n⚠️ Worst-case can close the gap, but RSS stays positive → fits statistically "
              "(small interference risk; verify process Cpk).")
    else:
        print("\n❌ Gap can go negative even at RSS → interference likely. Tighten tolerances or redesign.")

    # biggest contributor
    by_wc = max(items, key=lambda x: x[1])
    by_rss = max(items, key=lambda x: x[1] ** 2)
    print(f"\nLargest contributor: ±{by_wc[1]:.4g} (nominal {by_wc[0]:.4g}) — tighten this first.")


def cmd_fit(a):
    # hole/shaft as nominal:lower:upper deviations OR nominal:tol(sym)
    def band(spec):
        p = spec.split(":")
        nom = float(p[0])
        if len(p) == 3:
            lo, hi = float(p[1]), float(p[2])
        else:
            t = abs(float(p[1])); lo, hi = -t, t
        return nom, nom + lo, nom + hi  # nominal, min, max
    hn, hmin, hmax = band(a.hole)
    sn, smin, smax = band(a.shaft)
    max_clear = hmax - smin   # biggest hole, smallest shaft
    min_clear = hmin - smax   # smallest hole, biggest shaft
    print(f"**Fit — hole {hn} [{hmin:.4g}, {hmax:.4g}], shaft {sn} [{smin:.4g}, {smax:.4g}]**\n")
    print(f"Max clearance = hole_max − shaft_min = {max_clear:.4g}")
    print(f"Min clearance = hole_min − shaft_max = {min_clear:.4g}")
    if min_clear > 0:
        print(f"→ CLEARANCE fit (always a gap, {min_clear:.4g} to {max_clear:.4g}). Sliding/rotating.")
    elif max_clear < 0:
        print(f"→ INTERFERENCE (press) fit (always {-max_clear:.4g} to {-min_clear:.4g} interference). Permanent.")
    else:
        print(f"→ TRANSITION fit (may clear up to {max_clear:.4g} or interfere up to {-min_clear:.4g}). Location.")


def cmd_cpk(a):
    sigma = a.sigma
    half = a.tol  # spec is ±tol
    cp = (2 * half) / (6 * sigma)
    cpu = (half - a.offset) / (3 * sigma)
    cpl = (half + a.offset) / (3 * sigma)
    cpk = min(cpu, cpl)
    print(f"**Process capability** (spec ±{half}, σ={sigma}, mean offset {a.offset})\n")
    print(f"Cp  = tol/(6σ) = {cp:.3f}")
    print(f"Cpk = min(Cpu,Cpl) = {cpk:.3f} "
          + ("✅ capable (≥1.33)" if cpk >= 1.33 else ("⚠️ marginal (≥1.0)" if cpk >= 1.0 else "❌ not capable (<1.0)")))


def _isnum(s):
    try:
        float(s); return True
    except (ValueError, TypeError):
        return False


def main():
    ap = argparse.ArgumentParser(description="Tolerance stack-up calculator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("stack"); p.add_argument("items", nargs="*"); p.add_argument("--csv"); p.set_defaults(fn=cmd_stack)
    p = sub.add_parser("fit"); p.add_argument("--hole", required=True); p.add_argument("--shaft", required=True); p.set_defaults(fn=cmd_fit)
    p = sub.add_parser("cpk"); p.add_argument("--tol", type=float, required=True); p.add_argument("--sigma", type=float, required=True); p.add_argument("--offset", type=float, default=0.0); p.set_defaults(fn=cmd_cpk)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
