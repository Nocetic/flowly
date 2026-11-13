#!/usr/bin/env python3
"""Credit ratio scorecard — compute leverage/coverage/liquidity ratios + bands.

Stdlib only. Prints a chat-ready markdown scorecard with qualitative bands and,
optionally, a downside stress (EBITDA haircut). All money figures in the same
unit ($M recommended).

Example:
    credit_ratios.py --ebitda 500 --capex 120 --interest 78 --taxes 60 \
        --total-debt 1050 --cash 300 --mand-amort 50 \
        --equity 1400 --assets 3200 --cash-revolver 750 --stress 0.25
"""
from __future__ import annotations

import argparse


def band(value, thresholds, labels):
    """Map a value to a label. `labels` is ordered to match ascending `value`
    (len(labels) == len(thresholds) + 1). idx = count of thresholds met."""
    if value != value:  # NaN
        return "n.m."
    idx = sum(1 for t in thresholds if value >= t)
    return labels[min(idx, len(labels) - 1)]


def safe_div(a, b):
    return a / b if b else float("nan")


def compute(a):
    net_debt = a.total_debt - a.cash
    ebitda_capex = a.ebitda - a.capex
    fcf = a.ebitda - a.capex - a.taxes - a.interest
    rows = []

    gross_lev = safe_div(a.total_debt, a.ebitda)
    rows.append(("Gross debt/EBITDA", f"{gross_lev:.1f}x",
                 band(gross_lev, [2, 4, 6], ["conservative", "moderate", "aggressive", "highly levered"])))

    net_lev = safe_div(net_debt, a.ebitda)
    rows.append(("Net debt/EBITDA", f"{net_lev:.1f}x",
                 band(net_lev, [2, 4, 6], ["conservative", "moderate", "aggressive", "highly levered"])))

    int_cov = safe_div(a.ebitda, a.interest)
    rows.append(("EBITDA/interest", f"{int_cov:.1f}x",
                 band(int_cov, [2, 4], ["stressed", "adequate", "comfortable"])))

    capex_cov = safe_div(ebitda_capex, a.interest)
    rows.append(("(EBITDA−capex)/int", f"{capex_cov:.1f}x",
                 band(capex_cov, [1.5, 3], ["weak", "adequate", "strong"])))

    dscr = safe_div(ebitda_capex - a.taxes, a.interest + a.mand_amort)
    rows.append(("DSCR", f"{dscr:.2f}x",
                 band(dscr, [1.0, 1.5], ["can't cover", "tight", "healthy"])))

    fcf_debt = safe_div(fcf, a.total_debt)
    rows.append(("FCF/debt", f"{fcf_debt*100:.0f}%" if fcf_debt == fcf_debt else "n.m.",
                 band(fcf_debt, [0.05, 0.15], ["thin", "moderate", "healthy"])))

    if a.assets:
        debt_assets = safe_div(a.total_debt, a.assets)
        rows.append(("Debt/assets", f"{debt_assets*100:.0f}%",
                     band(debt_assets, [0.3, 0.5], ["low", "moderate", "high"])))
    if a.equity:
        de = safe_div(a.total_debt, a.total_debt + a.equity)
        rows.append(("Debt/(debt+equity)", f"{de*100:.0f}%",
                     band(de, [0.4, 0.6], ["low", "moderate", "high"])))

    return rows, {"net_debt": net_debt, "fcf": fcf, "int_cov": int_cov, "gross_lev": gross_lev}


def main():
    ap = argparse.ArgumentParser(description="Credit ratio scorecard")
    ap.add_argument("--ebitda", type=float, required=True)
    ap.add_argument("--capex", type=float, default=0.0)
    ap.add_argument("--interest", type=float, required=True)
    ap.add_argument("--taxes", type=float, default=0.0)
    ap.add_argument("--total-debt", type=float, required=True)
    ap.add_argument("--cash", type=float, default=0.0)
    ap.add_argument("--mand-amort", type=float, default=0.0, help="mandatory debt amortization")
    ap.add_argument("--equity", type=float, default=0.0, help="book equity (optional)")
    ap.add_argument("--assets", type=float, default=0.0, help="total assets (optional)")
    ap.add_argument("--cash-revolver", type=float, default=0.0, help="undrawn revolver (for liquidity note)")
    ap.add_argument("--stress", type=float, default=0.0, help="EBITDA haircut to stress, e.g. 0.25 = -25%%")
    a = ap.parse_args()

    rows, summ = compute(a)
    print("**Credit scorecard**\n")
    print("| Metric | Value | Band |")
    print("|--------|-------|------|")
    for name, val, b in rows:
        print(f"| {name} | {val} | {b} |")

    print(f"\nNet debt ${summ['net_debt']:.0f}M · FCF ${summ['fcf']:.0f}M")

    # Covenant-style headroom: how far EBITDA can fall before interest coverage hits 1x
    if a.interest:
        breakeven_drop = 1 - (a.interest / a.ebitda) if a.ebitda else float("nan")
        if breakeven_drop == breakeven_drop and breakeven_drop > 0:
            print(f"Headroom: EBITDA can fall ~{breakeven_drop*100:.0f}% before interest coverage hits 1.0x.")

    if a.cash_revolver:
        print(f"Liquidity buffer (undrawn revolver): ${a.cash_revolver:.0f}M")

    if a.stress > 0:
        import copy
        sa = copy.copy(a)
        sa.ebitda = a.ebitda * (1 - a.stress)
        srows, ssumm = compute(sa)
        print(f"\n**Stress: EBITDA −{a.stress*100:.0f}% (→ ${sa.ebitda:.0f}M)**\n")
        print("| Metric | Stressed | Band |")
        print("|--------|----------|------|")
        for name, val, b in srows:
            if name in ("Gross debt/EBITDA", "Net debt/EBITDA", "EBITDA/interest", "DSCR", "FCF/debt"):
                print(f"| {name} | {val} | {b} |")


if __name__ == "__main__":
    main()
