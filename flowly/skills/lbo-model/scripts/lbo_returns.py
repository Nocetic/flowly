#!/usr/bin/env python3
"""LBO returns calculator — sources & uses, debt sweep, IRR/MOIC, sensitivity grid.

Stdlib only. Prints a chat-ready markdown summary. A simplified single-tranche
sweep model intended for a fast, directionally-correct read; build the full
multi-tranche workbook with excel-author when an auditable model is needed.

Example:
    lbo_returns.py --ebitda 100 --entry-mult 8.0 --exit-mult 8.0 \
        --leverage 5.0 --rate 0.09 --years 5 --ebitda-growth 0.06 \
        --fcf-conv 0.55 --fees 0.025 --net-debt 0
"""
from __future__ import annotations

import argparse


def irr_from_moic(moic: float, years: int) -> float:
    """IRR for a single in/out cash flow (no interim distributions)."""
    if moic <= 0 or years <= 0:
        return float("nan")
    return moic ** (1.0 / years) - 1.0


def run_lbo(ebitda, entry_mult, exit_mult, leverage, rate, years,
            ebitda_growth, fcf_conv, fees, net_debt):
    """Return a dict of deal economics. EBITDA figures in $M."""
    entry_ev = entry_mult * ebitda
    new_debt = leverage * ebitda
    fee_amt = fees * entry_ev
    # Sponsor equity is the plug. Assume existing net debt is refinanced into new debt.
    equity = entry_ev + fee_amt + net_debt - new_debt
    equity = max(equity, 0.0)

    # Year-by-year EBITDA and a simple 100%-excess-cash sweep.
    debt = new_debt
    eb = ebitda
    schedule = []
    for yr in range(1, years + 1):
        eb = eb * (1 + ebitda_growth)
        interest = debt * rate
        # FCF available to sweep = EBITDA * conversion - interest (post-tax already in conv assumption)
        fcf = eb * fcf_conv - interest
        sweep = max(min(fcf, debt), 0.0)
        debt = max(debt - sweep, 0.0)
        schedule.append({"yr": yr, "ebitda": eb, "interest": interest,
                         "fcf": fcf, "sweep": sweep, "debt_end": debt})

    exit_ebitda = eb
    exit_ev = exit_mult * exit_ebitda
    exit_net_debt = debt
    exit_equity = max(exit_ev - exit_net_debt, 0.0)
    moic = exit_equity / equity if equity > 0 else float("nan")
    irr = irr_from_moic(moic, years)

    # Lever attribution (approximate, additive decomposition of equity value created)
    eb0 = ebitda
    debt0 = new_debt
    eq_growth = (exit_ebitda - eb0) * exit_mult            # value from EBITDA growth (at exit mult)
    eq_multiple = (exit_mult - entry_mult) * eb0           # value from multiple change (on entry EBITDA)
    eq_delever = (debt0 - exit_net_debt)                   # value from net debt reduction
    total_created = eq_growth + eq_multiple + eq_delever
    if abs(total_created) < 1e-9:
        attr = {"growth": 0.0, "multiple": 0.0, "delever": 0.0}
    else:
        attr = {"growth": eq_growth / total_created,
                "multiple": eq_multiple / total_created,
                "delever": eq_delever / total_created}

    return {
        "entry_ev": entry_ev, "new_debt": new_debt, "fee_amt": fee_amt, "equity": equity,
        "schedule": schedule, "exit_ebitda": exit_ebitda, "exit_ev": exit_ev,
        "exit_net_debt": exit_net_debt, "exit_equity": exit_equity,
        "moic": moic, "irr": irr, "attr": attr,
    }


def fmt_pct(x):
    return "n.m." if x != x else f"{x*100:.0f}%"


def main():
    ap = argparse.ArgumentParser(description="LBO returns calculator")
    ap.add_argument("--ebitda", type=float, required=True, help="LTM EBITDA ($M)")
    ap.add_argument("--entry-mult", type=float, required=True, help="entry EV/EBITDA")
    ap.add_argument("--exit-mult", type=float, default=None, help="exit EV/EBITDA (default = entry)")
    ap.add_argument("--leverage", type=float, default=5.0, help="total debt / EBITDA at entry")
    ap.add_argument("--rate", type=float, default=0.09, help="blended debt interest rate")
    ap.add_argument("--years", type=int, default=5, help="hold period")
    ap.add_argument("--ebitda-growth", type=float, default=0.05, help="annual EBITDA growth")
    ap.add_argument("--fcf-conv", type=float, default=0.55,
                    help="EBITDA→FCF conversion (post-tax, after capex/NWC, before interest)")
    ap.add_argument("--fees", type=float, default=0.025, help="transaction+financing fees as %% of EV")
    ap.add_argument("--net-debt", type=float, default=0.0, help="existing net debt refinanced ($M)")
    a = ap.parse_args()
    exit_mult = a.exit_mult if a.exit_mult is not None else a.entry_mult

    r = run_lbo(a.ebitda, a.entry_mult, exit_mult, a.leverage, a.rate, a.years,
                a.ebitda_growth, a.fcf_conv, a.fees, a.net_debt)

    print(f"**LBO — entry {a.entry_mult:.1f}x ${a.ebitda:.0f}M EBITDA, "
          f"{a.leverage:.1f}x leverage, {a.years}-yr hold**\n")
    print("Sources & Uses:")
    print(f"  Entry EV ${r['entry_ev']:.0f}M + fees ${r['fee_amt']:.0f}M"
          + (f" + refi net debt ${a.net_debt:.0f}M" if a.net_debt else ""))
    print(f"  = Debt ${r['new_debt']:.0f}M + Sponsor equity ${r['equity']:.0f}M\n")

    print(f"Exit ({exit_mult:.1f}x): EV ${r['exit_ev']:.0f}M − net debt "
          f"${r['exit_net_debt']:.0f}M = equity ${r['exit_equity']:.0f}M")
    print(f"**MOIC {r['moic']:.2f}x · IRR {fmt_pct(r['irr'])}**")
    at = r["attr"]
    print(f"Lever mix: deleveraging {at['delever']*100:.0f}% / "
          f"EBITDA growth {at['growth']*100:.0f}% / multiple {at['multiple']*100:.0f}%\n")

    # Sensitivity grid: exit multiple (rows) x hold year (cols)
    exit_mults = [round(exit_mult + d, 1) for d in (-1.0, -0.5, 0.0, 0.5, 1.0)]
    yrs = [max(a.years - 1, 1), a.years, a.years + 1]
    print("Exit-multiple × hold-year IRR:")
    print("| Exit \\ Yr | " + " | ".join(str(y) for y in yrs) + " |")
    print("|" + "---|" * (len(yrs) + 1))
    for em in exit_mults:
        cells = []
        for y in yrs:
            rr = run_lbo(a.ebitda, a.entry_mult, em, a.leverage, a.rate, y,
                         a.ebitda_growth, a.fcf_conv, a.fees, a.net_debt)
            cells.append(fmt_pct(rr["irr"]))
        mark = " ⟵ base" if abs(em - exit_mult) < 1e-9 else ""
        print(f"| {em:.1f}x | " + " | ".join(cells) + f" |{mark}")


if __name__ == "__main__":
    main()
