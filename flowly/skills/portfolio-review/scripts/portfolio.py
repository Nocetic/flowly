#!/usr/bin/env python3
"""Portfolio analyzer — holdings CSV -> weights, concentration, exposure tables.

Stdlib only. Prints chat-ready markdown.

CSV columns (header row, case-insensitive; extra columns ignored):
    ticker        required
    value         market value  (or supply quantity + price)
    quantity      optional (used with price if value missing)
    price         optional
    name          optional
    sector        optional   -> sector exposure
    geography     optional   -> geography exposure
    asset_class   optional   -> asset-class exposure
    beta          optional   -> weighted portfolio beta

Usage:
    portfolio.py holdings.csv
    portfolio.py holdings.csv --top 10
"""
from __future__ import annotations

import argparse
import csv
import sys


def _num(x):
    if x is None:
        return None
    x = str(x).strip().replace(",", "").replace("$", "").replace("%", "")
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
            sys.exit("empty or headerless CSV")
        norm = {fn: fn.strip().lower().replace(" ", "_") for fn in reader.fieldnames}
        rows = []
        for raw in reader:
            r = {norm[k]: v for k, v in raw.items() if k in norm}
            value = _num(r.get("value"))
            if value is None:
                q, p = _num(r.get("quantity")), _num(r.get("price"))
                value = (q * p) if (q is not None and p is not None) else None
            if value is None or value <= 0:
                continue
            rows.append({
                "ticker": (r.get("ticker") or r.get("name") or "?").strip().upper(),
                "name": (r.get("name") or "").strip(),
                "value": value,
                "sector": (r.get("sector") or "Unclassified").strip() or "Unclassified",
                "geography": (r.get("geography") or "Unclassified").strip() or "Unclassified",
                "asset_class": (r.get("asset_class") or r.get("assetclass") or "Unclassified").strip() or "Unclassified",
                "beta": _num(r.get("beta")),
            })
    if not rows:
        sys.exit("no valid holdings found (need a positive value, or quantity+price)")
    return rows


def breakdown(rows, key, total):
    agg = {}
    for r in rows:
        agg[r[key]] = agg.get(r[key], 0.0) + r["value"]
    return sorted(((k, v, v / total) for k, v in agg.items()), key=lambda x: -x[1])


def main():
    ap = argparse.ArgumentParser(description="Portfolio analyzer")
    ap.add_argument("csv")
    ap.add_argument("--top", type=int, default=10, help="positions to show")
    a = ap.parse_args()

    rows = load(a.csv)
    total = sum(r["value"] for r in rows)
    for r in rows:
        r["weight"] = r["value"] / total
    rows.sort(key=lambda r: -r["weight"])

    n = len(rows)
    hhi = sum(r["weight"] ** 2 for r in rows)
    eff_n = 1 / hhi if hhi else float("nan")
    top5 = sum(r["weight"] for r in rows[:5])
    top10 = sum(r["weight"] for r in rows[:10])

    print(f"**Portfolio review** ({n} holdings, ${total:,.0f} total)\n")
    print(f"Concentration: top-1 {rows[0]['weight']*100:.0f}% · "
          f"top-5 {top5*100:.0f}% · top-10 {top10*100:.0f}% · "
          f"HHI {hhi:.3f} → effective ~{eff_n:.0f} holdings\n")

    print(f"| Position | Weight | Value |")
    print(f"|----------|--------|-------|")
    for r in rows[: a.top]:
        print(f"| {r['ticker']} | {r['weight']*100:.1f}% | ${r['value']:,.0f} |")
    if n > a.top:
        rest = sum(r["weight"] for r in rows[a.top:])
        print(f"| _({n - a.top} more)_ | {rest*100:.1f}% | |")

    for key, label in (("asset_class", "Asset class"), ("sector", "Sector"), ("geography", "Geography")):
        bd = breakdown(rows, key, total)
        if len(bd) == 1 and bd[0][0] == "Unclassified":
            continue
        parts = " · ".join(f"{k} {w*100:.0f}%" for k, _, w in bd[:6])
        if len(bd) > 6:
            parts += f" · +{len(bd)-6} more"
        print(f"\n**{label}:** {parts}")

    betas = [(r["weight"], r["beta"]) for r in rows if r["beta"] is not None]
    if betas:
        covered = sum(w for w, _ in betas)
        wbeta = sum(w * b for w, b in betas) / covered if covered else float("nan")
        print(f"\nWeighted portfolio beta ≈ {wbeta:.2f} (over {covered*100:.0f}% of book)")

    # Flags
    flags = []
    big = [r for r in rows if r["weight"] > 0.10]
    if big:
        flags.append(f"{len(big)} position(s) >10%: " + ", ".join(f"{r['ticker']} {r['weight']*100:.0f}%" for r in big[:5]))
    if top5 > 0.50:
        flags.append(f"top-5 = {top5*100:.0f}% of the book — concentrated")
    sec = breakdown(rows, "sector", total)
    if sec and sec[0][0] != "Unclassified" and sec[0][2] > 0.40:
        flags.append(f"{sec[0][0]} = {sec[0][2]*100:.0f}% — single-sector heavy")
    geo = breakdown(rows, "geography", total)
    if geo and geo[0][0] != "Unclassified" and geo[0][2] > 0.85:
        flags.append(f"{geo[0][0]} = {geo[0][2]*100:.0f}% — home/region concentration")
    if flags:
        print("\n⚠️ Flags:")
        for fl in flags:
            print(f"- {fl}")


if __name__ == "__main__":
    main()
