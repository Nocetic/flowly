#!/usr/bin/env python3
"""Startup unit-economics scorecard — CAC, LTV, payback, NRR, burn multiple, etc.

Stdlib only. Pass only the inputs you have; it computes what's derivable and
flags each metric. Prints chat-ready markdown.

Example:
    unit_econ.py --arpa 1200 --gross-margin 0.78 --monthly-churn 0.02 \
        --sm-spend 500000 --new-customers 250 \
        --net-new-arr 1500000 --net-burn 1200000 --prior-sm 450000 \
        --revenue-growth 0.9 --fcf-margin -0.3 --cash 18000000 --monthly-burn 1000000
"""
from __future__ import annotations

import argparse


def flag(ok, warn=False):
    return "✅" if ok else ("⚠️" if warn else "❌")


def main():
    ap = argparse.ArgumentParser(description="Unit-economics scorecard")
    ap.add_argument("--arpa", type=float, help="avg revenue per account (match churn period)")
    ap.add_argument("--gross-margin", type=float, default=0.75, help="gross margin fraction")
    ap.add_argument("--monthly-churn", type=float, help="monthly revenue churn fraction")
    ap.add_argument("--annual-churn", type=float, help="annual revenue churn fraction")
    ap.add_argument("--sm-spend", type=float, help="fully-loaded S&M in period")
    ap.add_argument("--new-customers", type=float, help="new customers in period")
    ap.add_argument("--cac", type=float, help="CAC (overrides sm/new)")
    ap.add_argument("--net-new-arr", type=float, help="net new ARR in period")
    ap.add_argument("--net-burn", type=float, help="net cash burn in period")
    ap.add_argument("--prior-sm", type=float, help="prior-period S&M (for magic number)")
    ap.add_argument("--start-arr", type=float, help="starting ARR (for NRR)")
    ap.add_argument("--expansion", type=float, default=0.0)
    ap.add_argument("--contraction", type=float, default=0.0)
    ap.add_argument("--churned-arr", type=float, default=0.0)
    ap.add_argument("--nrr", type=float, help="NRR fraction (overrides components)")
    ap.add_argument("--revenue-growth", type=float, help="YoY revenue growth fraction (Rule of 40)")
    ap.add_argument("--fcf-margin", type=float, help="FCF or profit margin fraction (Rule of 40)")
    ap.add_argument("--cash", type=float, help="cash balance")
    ap.add_argument("--monthly-burn", type=float, help="net monthly burn (for runway)")
    ap.add_argument("--arpa-monthly", action="store_true", help="ARPA is monthly (annualize lifetime accordingly)")
    a = ap.parse_args()

    lines = []
    print("**Unit economics**\n")

    # Churn normalization
    m_churn = a.monthly_churn
    an_churn = a.annual_churn
    if m_churn is not None and an_churn is None:
        an_churn = 1 - (1 - m_churn) ** 12
    if an_churn is not None and m_churn is None:
        m_churn = 1 - (1 - an_churn) ** (1 / 12)

    # CAC
    cac = a.cac
    if cac is None and a.sm_spend and a.new_customers:
        cac = a.sm_spend / a.new_customers
    if cac is not None:
        lines.append(f"CAC ${cac:,.0f}")

    # LTV (gross-margin based)
    ltv = None
    churn_for_ltv = m_churn if a.arpa_monthly else (an_churn if an_churn else m_churn)
    if a.arpa and churn_for_ltv and churn_for_ltv > 0:
        ltv = (a.arpa * a.gross_margin) / churn_for_ltv
        lines.append(f"LTV ${ltv:,.0f} (GM-based)")

    # LTV/CAC
    if ltv is not None and cac:
        ratio = ltv / cac
        f = flag(3 <= ratio <= 5, warn=(ratio > 5 or 1 <= ratio < 3))
        note = " (likely under-investing)" if ratio > 5 else (" (losing money/customer)" if ratio < 1 else "")
        lines.append(f"**LTV/CAC {ratio:.1f}x** {f}{note}")

    # CAC payback (months)
    if cac and a.arpa:
        monthly_gp = (a.arpa * a.gross_margin) if a.arpa_monthly else (a.arpa * a.gross_margin / 12)
        if monthly_gp > 0:
            payback = cac / monthly_gp
            lines.append(f"CAC payback {payback:.1f} mo {flag(payback < 12, warn=payback < 18)}")

    # Gross margin
    lines.append(f"Gross margin {a.gross_margin*100:.0f}% {flag(a.gross_margin >= 0.70, warn=a.gross_margin >= 0.55)}")

    # Churn
    if an_churn is not None:
        mc = f"{m_churn*100:.1f}%/mo " if m_churn is not None else ""
        lines.append(f"Churn {mc}(≈{an_churn*100:.0f}%/yr) {flag(an_churn < 0.10, warn=an_churn < 0.20)}")

    # NRR
    nrr = a.nrr
    if nrr is None and a.start_arr:
        nrr = (a.start_arr + a.expansion - a.contraction - a.churned_arr) / a.start_arr
    if nrr is not None:
        lines.append(f"NRR {nrr*100:.0f}% {flag(nrr >= 1.10, warn=nrr >= 1.00)}")

    # Burn multiple
    if a.net_burn is not None and a.net_new_arr:
        bm = a.net_burn / a.net_new_arr
        lines.append(f"Burn multiple {bm:.1f} {flag(bm < 1.5, warn=bm < 3)}")

    # Magic number
    if a.net_new_arr is not None and a.prior_sm:
        mn = a.net_new_arr / a.prior_sm
        lines.append(f"Magic number {mn:.2f} {flag(mn >= 0.75, warn=mn >= 0.5)}")

    # Rule of 40
    if a.revenue_growth is not None and a.fcf_margin is not None:
        r40 = (a.revenue_growth + a.fcf_margin) * 100
        lines.append(f"Rule of 40 = {r40:.0f} {flag(r40 >= 40, warn=r40 >= 30)}")

    # Runway
    if a.cash and a.monthly_burn and a.monthly_burn > 0:
        runway = a.cash / a.monthly_burn
        lines.append(f"Runway {runway:.0f} mo {flag(runway >= 18, warn=runway >= 12)}")
    elif a.cash and a.net_burn and a.net_burn > 0:
        # assume net_burn is per-period; can't infer months without period — note it
        lines.append(f"Cash ${a.cash:,.0f} (pass --monthly-burn for runway)")

    print("\n".join(lines))
    print("\n_Legend: ✅ healthy · ⚠️ watch · ❌ problem. Interpret metrics together, not in isolation._")


if __name__ == "__main__":
    main()
