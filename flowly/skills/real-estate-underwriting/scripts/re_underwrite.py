#!/usr/bin/env python3
"""Real-estate underwriting — NOI, cap rate, DSCR, cash-on-cash, levered IRR.

Stdlib only. Prints a chat-ready markdown summary + cap-rate x rent-growth grid.

Example:
    re_underwrite.py --gpr 240000 --vacancy 0.07 --other-income 12000 \
        --opex-ratio 0.42 --reserves-per-unit 300 --units 20 \
        --price 3000000 --ltv 0.70 --rate 0.065 --amort-years 30 \
        --hold-years 5 --rent-growth 0.03 --expense-growth 0.025 \
        --exit-cap 0.062 --selling-costs 0.05
"""
from __future__ import annotations

import argparse


def mortgage_payment(principal, annual_rate, amort_years):
    """Annual amortizing payment (monthly compounding, ×12)."""
    if principal <= 0:
        return 0.0
    r = annual_rate / 12.0
    n = amort_years * 12
    if r == 0:
        return principal / amort_years
    m = principal * r / (1 - (1 + r) ** -n)
    return m * 12.0


def balance_after(principal, annual_rate, amort_years, months_paid):
    r = annual_rate / 12.0
    n = amort_years * 12
    if r == 0:
        return max(principal * (1 - months_paid / n), 0.0)
    pmt = principal * r / (1 - (1 + r) ** -n)
    bal = principal * (1 + r) ** months_paid - pmt * (((1 + r) ** months_paid - 1) / r)
    return max(bal, 0.0)


def irr(cashflows, lo=-0.99, hi=2.0, tol=1e-7):
    """IRR via bisection on NPV; cashflows[0] is the (negative) initial outlay."""
    def npv(rate):
        return sum(cf / (1 + rate) ** i for i, cf in enumerate(cashflows))
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < tol:
            return mid
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def noi_for_year(a, year):
    """NOI in a given year (1-indexed) with rent/expense growth."""
    gpr = a.gpr * (1 + a.rent_growth) ** (year - 1)
    other = a.other_income * (1 + a.rent_growth) ** (year - 1)
    egi = gpr * (1 - a.vacancy) + other
    opex = egi * a.opex_ratio if a.opex is None else a.opex * (1 + a.expense_growth) ** (year - 1)
    reserves = a.reserves_per_unit * a.units * (1 + a.expense_growth) ** (year - 1)
    return egi - opex - reserves


def underwrite(a, exit_cap=None, rent_growth=None):
    aa = argparse.Namespace(**vars(a))
    if rent_growth is not None:
        aa.rent_growth = rent_growth
    exit_cap = exit_cap if exit_cap is not None else a.exit_cap

    noi1 = noi_for_year(aa, 1)
    going_in_cap = noi1 / a.price if a.price else float("nan")
    # Default exit cap = going-in cap + 25 bps (conservative) when not supplied.
    if exit_cap is None:
        exit_cap = going_in_cap + 0.0025

    # Loan sizing: lower of LTV and DSCR constraints
    loan_ltv = a.ltv * a.price
    ds_at_ltv = mortgage_payment(loan_ltv, a.rate, a.amort_years)
    if a.min_dscr > 0 and ds_at_ltv > 0:
        max_ds = noi1 / a.min_dscr
        # scale loan so debt service <= max_ds
        loan_dscr = loan_ltv * (max_ds / ds_at_ltv) if ds_at_ltv else loan_ltv
        loan = min(loan_ltv, loan_dscr)
    else:
        loan = loan_ltv
    debt_service = mortgage_payment(loan, a.rate, a.amort_years)
    dscr = noi1 / debt_service if debt_service else float("inf")
    debt_yield = noi1 / loan if loan else float("nan")

    equity = a.price - loan + a.price * a.closing_costs
    coc = (noi1 - debt_service) / equity if equity else float("nan")

    # Cash flows over hold
    cfs = [-equity]
    for yr in range(1, a.hold_years + 1):
        noi_y = noi_for_year(aa, yr)
        cf = noi_y - debt_service
        if yr == a.hold_years:
            exit_noi = noi_for_year(aa, yr + 1)  # forward NOI for exit valuation
            sale_value = exit_noi / exit_cap
            net_sale = sale_value * (1 - a.selling_costs)
            payoff = balance_after(loan, a.rate, a.amort_years, yr * 12)
            cf += net_sale - payoff
        cfs.append(cf)
    deal_irr = irr(cfs)
    total_dist = sum(cfs[1:])
    equity_mult = (total_dist + equity) / equity if equity else float("nan")  # distributions incl. return of capital
    # equity multiple as total cash returned / invested:
    equity_mult = sum(cf for cf in cfs[1:]) / equity if equity else float("nan")

    return {
        "noi1": noi1, "going_in_cap": going_in_cap, "loan": loan, "equity": equity,
        "debt_service": debt_service, "dscr": dscr, "debt_yield": debt_yield,
        "coc": coc, "irr": deal_irr, "equity_mult": equity_mult,
    }


def fmt_pct(x):
    return "n.m." if x != x else f"{x*100:.1f}%"


def main():
    ap = argparse.ArgumentParser(description="Real-estate underwriting")
    ap.add_argument("--gpr", type=float, required=True, help="gross potential rent (annual)")
    ap.add_argument("--vacancy", type=float, default=0.07)
    ap.add_argument("--other-income", type=float, default=0.0)
    ap.add_argument("--opex-ratio", dest="opex_ratio", type=float, default=0.42,
                    help="operating expenses as fraction of EGI")
    ap.add_argument("--opex", type=float, default=None, help="absolute opex (overrides ratio)")
    ap.add_argument("--reserves-per-unit", type=float, default=300.0)
    ap.add_argument("--units", type=int, default=1)
    ap.add_argument("--price", type=float, required=True)
    ap.add_argument("--closing-costs", type=float, default=0.02, help="as fraction of price, added to equity")
    ap.add_argument("--ltv", type=float, default=0.70)
    ap.add_argument("--rate", type=float, default=0.065)
    ap.add_argument("--amort-years", type=int, default=30)
    ap.add_argument("--min-dscr", type=float, default=1.25, help="lender minimum DSCR (0 to disable)")
    ap.add_argument("--hold-years", type=int, default=5)
    ap.add_argument("--rent-growth", type=float, default=0.03)
    ap.add_argument("--expense-growth", type=float, default=0.025)
    ap.add_argument("--exit-cap", type=float, default=None, help="exit cap (default = going-in cap +25bps)")
    ap.add_argument("--selling-costs", type=float, default=0.05)
    a = ap.parse_args()

    base = underwrite(a)
    if a.exit_cap is None:
        a.exit_cap = base["going_in_cap"] + 0.0025
        base = underwrite(a)

    pu = a.price / a.units if a.units else a.price
    print(f"**Underwrite — {a.units}-unit @ ${a.price:,.0f}** (${pu:,.0f}/unit)\n")
    print(f"NOI (yr-1) ${base['noi1']:,.0f} · Going-in cap {fmt_pct(base['going_in_cap'])}")
    print(f"Loan ${base['loan']:,.0f} ({a.ltv*100:.0f}% LTV cap, {a.rate*100:.2f}%, {a.amort_years}-yr) · "
          f"DSCR {base['dscr']:.2f}x · Debt yield {fmt_pct(base['debt_yield'])}")
    print(f"Cash invested ${base['equity']:,.0f} · Cash-on-cash {fmt_pct(base['coc'])} (yr-1)")
    print(f"{a.hold_years}-yr levered IRR {fmt_pct(base['irr'])} · "
          f"Equity multiple {base['equity_mult']:.2f}x (exit {fmt_pct(a.exit_cap)} cap)\n")

    exit_caps = [round(a.exit_cap + d, 4) for d in (-0.0025, 0.0, 0.0025, 0.005)]
    rent_gs = [max(a.rent_growth - 0.01, 0), a.rent_growth, a.rent_growth + 0.01]
    print("Exit-cap × rent-growth levered IRR:")
    print("| Exit cap \\ rent g | " + " | ".join(f"{g*100:.0f}%" for g in rent_gs) + " |")
    print("|" + "---|" * (len(rent_gs) + 1))
    for ec in exit_caps:
        cells = [fmt_pct(underwrite(a, exit_cap=ec, rent_growth=g)["irr"]) for g in rent_gs]
        mark = " ⟵ base" if abs(ec - a.exit_cap) < 1e-9 else ""
        print(f"| {ec*100:.2f}% | " + " | ".join(cells) + f" |{mark}")


if __name__ == "__main__":
    main()
