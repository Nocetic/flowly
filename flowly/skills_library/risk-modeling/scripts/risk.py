#!/usr/bin/env python3
"""Risk report — returns/prices CSV -> VaR, CVaR, vol, drawdown, stress.

Stdlib only (no numpy). Prints a chat-ready markdown snapshot.

CSV: a column of periodic returns (e.g. 0.012, -0.004) OR prices with --prices.

Usage:
    risk.py returns.csv --col daily_return --confidence 0.95 --horizon 1 --value 1000000
    risk.py prices.csv --prices --col close --confidence 0.99 --periods-per-year 252
"""
from __future__ import annotations

import argparse
import csv
import math
import sys

Z = {0.90: 1.2816, 0.95: 1.6449, 0.975: 1.9600, 0.99: 2.3263, 0.995: 2.5758}


def load_returns(path, col, prices):
    vals = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit("empty/headerless CSV")
        fields = {fn.strip().lower(): fn for fn in reader.fieldnames}
        key = fields.get(col.strip().lower())
        if key is None:
            # single-column fallback
            if len(reader.fieldnames) == 1:
                key = reader.fieldnames[0]
            else:
                sys.exit(f"column '{col}' not found. Available: {', '.join(reader.fieldnames)}")
        for row in reader:
            raw = (row.get(key) or "").strip().replace(",", "").replace("%", "").replace("$", "")
            if not raw:
                continue
            try:
                vals.append(float(raw))
            except ValueError:
                continue
    if prices:
        if len(vals) < 2:
            sys.exit("need >=2 prices")
        vals = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))]
    if len(vals) < 5:
        sys.exit("need at least 5 return observations")
    return vals


def mean(x):
    return sum(x) / len(x)


def stdev(x):
    m = mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def skew_kurt(x):
    n = len(x); m = mean(x); s = stdev(x)
    if s == 0:
        return 0.0, 0.0
    sk = sum(((v - m) / s) ** 3 for v in x) / n
    ku = sum(((v - m) / s) ** 4 for v in x) / n - 3.0  # excess
    return sk, ku


def max_drawdown(returns):
    """Max peak-to-trough on a cumulative-return path."""
    cum = 1.0
    peak = 1.0
    mdd = 0.0
    for r in returns:
        cum *= (1 + r)
        peak = max(peak, cum)
        mdd = min(mdd, cum / peak - 1)
    return mdd


def percentile(sorted_x, p):
    """Linear-interpolated percentile, p in [0,1]."""
    if not sorted_x:
        return float("nan")
    k = p * (len(sorted_x) - 1)
    lo = int(math.floor(k)); hi = int(math.ceil(k))
    if lo == hi:
        return sorted_x[lo]
    return sorted_x[lo] + (sorted_x[hi] - sorted_x[lo]) * (k - lo)


def main():
    ap = argparse.ArgumentParser(description="Risk report from returns/prices")
    ap.add_argument("csv")
    ap.add_argument("--col", default="return")
    ap.add_argument("--prices", action="store_true", help="input is prices, convert to returns")
    ap.add_argument("--confidence", type=float, default=0.95)
    ap.add_argument("--horizon", type=int, default=1, help="horizon in periods (sqrt-scaled)")
    ap.add_argument("--value", type=float, default=0.0, help="portfolio value for $ figures")
    ap.add_argument("--periods-per-year", type=int, default=252)
    ap.add_argument("--rf", type=float, default=0.0, help="risk-free per period for Sharpe")
    a = ap.parse_args()

    r = load_returns(a.csv, a.col, a.prices)
    n = len(r)
    mu = mean(r); sig = stdev(r)
    sk, ku = skew_kurt(r)
    ppy = a.periods_per_year
    ann_ret = mu * ppy
    ann_vol = sig * math.sqrt(ppy)
    conf = a.confidence
    z = Z.get(round(conf, 3))
    if z is None:
        # nearest
        z = Z[min(Z, key=lambda k: abs(k - conf))]
    h = math.sqrt(a.horizon)

    # Parametric VaR/CVaR (loss as positive number)
    par_var = (z * sig - mu) * h
    # CVaR normal: phi(z)/(1-conf)
    phi = math.exp(-z * z / 2) / math.sqrt(2 * math.pi)
    par_cvar = (phi / (1 - conf) * sig - mu) * h

    # Historical VaR/CVaR
    sr = sorted(r)
    q = percentile(sr, 1 - conf)
    hist_var = -q * h
    tail = [x for x in sr if x <= q]
    hist_cvar = -(mean(tail) if tail else q) * h

    mdd = max_drawdown(r)
    downside = [x for x in r if x < a.rf]
    dd = math.sqrt(sum((x - a.rf) ** 2 for x in downside) / len(r)) if downside else 0.0
    sharpe = (mu - a.rf) / sig * math.sqrt(ppy) if sig else float("nan")
    sortino = (mu - a.rf) / dd * math.sqrt(ppy) if dd else float("nan")

    def dollar(p):
        return f" (~${p*a.value:,.0f})" if a.value else ""

    cpct = int(conf * 100)
    print(f"**Risk snapshot** ({n} obs"
          + (f", ${a.value:,.0f}" if a.value else "") + f", {a.horizon}-period horizon)\n")
    print(f"σ {ann_vol*100:.1f}% ann · mean {ann_ret*100:.1f}% ann · "
          f"Sharpe {sharpe:.2f} · Sortino {sortino:.2f}")
    print(f"{cpct}% VaR — historical {hist_var*100:.2f}%{dollar(hist_var)} · "
          f"parametric {par_var*100:.2f}%{dollar(par_var)}")
    print(f"{cpct}% CVaR — historical {hist_cvar*100:.2f}%{dollar(hist_cvar)} · "
          f"parametric {par_cvar*100:.2f}%{dollar(par_cvar)}")
    print(f"Max drawdown {mdd*100:.1f}% · skew {sk:.2f} · excess kurtosis {ku:.2f}")
    if ku > 1 or sk < -0.2:
        print("→ fat left tail: parametric VaR likely understates; lean on historical/CVaR & stress.")

    # Deterministic factor-shock stress (uses a beta-of-1 proxy unless extended)
    print("\nStress P&L (single-factor, illustrative — replace with portfolio betas):")
    print("| Scenario | Move | Est. P&L |")
    print("|----------|------|----------|")
    for label, move in [("Equities −20%", -0.20), ("Sharp drop −10%", -0.10),
                        ("2008-style", -0.45), ("COVID-Mar20", -0.34)]:
        print(f"| {label} | {move*100:.0f}% | {move*100:.0f}%{dollar(move)} |")


if __name__ == "__main__":
    main()
