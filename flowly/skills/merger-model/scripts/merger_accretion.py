#!/usr/bin/env python3
"""Merger accretion/dilution calculator + breakeven synergies.

Stdlib only. Prints chat-ready markdown. NI, synergies, D&A in $M; shares in M;
prices per share.

Example:
    merger_accretion.py --acq-ni 1000 --acq-shares 500 --acq-price 50 \
        --tgt-ni 200 --tgt-shares 100 --offer-price 40 --tgt-unaffected 30 \
        --pct-stock 0.5 --pct-cash 0.3 --pct-debt 0.2 \
        --debt-rate 0.06 --cash-yield 0.03 --tax 0.25 \
        --synergies 50 --incremental-da 20
"""
from __future__ import annotations

import argparse


def pro_forma_eps(a, synergies=None, pct_stock=None, pct_cash=None, pct_debt=None):
    syn = a.synergies if synergies is None else synergies
    ps = a.pct_stock if pct_stock is None else pct_stock
    pc = a.pct_cash if pct_cash is None else pct_cash
    pd = a.pct_debt if pct_debt is None else pct_debt
    tot = ps + pc + pd
    if tot <= 0:
        ps, pc, pd = 1.0, 0.0, 0.0
        tot = 1.0
    ps, pc, pd = ps / tot, pc / tot, pd / tot

    equity_purchase = a.offer_price * a.tgt_shares  # equity consideration ($M)
    cash_used = equity_purchase * pc
    debt_used = equity_purchase * pd
    stock_used = equity_purchase * ps

    new_shares = stock_used / a.acq_price if a.acq_price else 0.0
    after_tax = 1 - a.tax

    new_interest = debt_used * a.debt_rate * after_tax
    foregone_interest = cash_used * a.cash_yield * after_tax
    da_drag = a.incremental_da * after_tax  # incremental D&A from PPA write-ups (pretax) -> after-tax NI hit
    syn_at = syn * after_tax

    pf_ni = a.acq_ni + a.tgt_ni + syn_at - new_interest - foregone_interest - da_drag
    pf_shares = a.acq_shares + new_shares
    return pf_ni / pf_shares if pf_shares else float("nan"), {
        "new_shares": new_shares, "cash_used": cash_used, "debt_used": debt_used,
        "stock_used": stock_used, "new_interest": new_interest,
        "foregone_interest": foregone_interest, "da_drag": da_drag, "pf_ni": pf_ni,
        "pf_shares": pf_shares,
    }


def breakeven_synergies(a):
    """Synergy level (pretax $M) that makes pro-forma EPS == standalone EPS."""
    standalone = a.acq_ni / a.acq_shares
    lo, hi = -1e6, 1e6
    for _ in range(200):
        mid = (lo + hi) / 2
        eps, _ = pro_forma_eps(a, synergies=mid)
        if eps < standalone:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def main():
    ap = argparse.ArgumentParser(description="Merger accretion/dilution")
    ap.add_argument("--acq-ni", type=float, required=True, help="acquirer net income ($M)")
    ap.add_argument("--acq-shares", type=float, required=True, help="acquirer diluted shares (M)")
    ap.add_argument("--acq-price", type=float, required=True, help="acquirer share price")
    ap.add_argument("--tgt-ni", type=float, required=True, help="target net income ($M)")
    ap.add_argument("--tgt-shares", type=float, required=True, help="target shares (M)")
    ap.add_argument("--offer-price", type=float, required=True, help="offer price per target share")
    ap.add_argument("--tgt-unaffected", type=float, default=None, help="target unaffected price (for premium)")
    ap.add_argument("--pct-stock", type=float, default=0.0)
    ap.add_argument("--pct-cash", type=float, default=0.0)
    ap.add_argument("--pct-debt", type=float, default=0.0)
    ap.add_argument("--debt-rate", type=float, default=0.06)
    ap.add_argument("--cash-yield", type=float, default=0.03, help="opportunity yield on cash used")
    ap.add_argument("--tax", type=float, default=0.25)
    ap.add_argument("--synergies", type=float, default=0.0, help="annual pretax synergies ($M)")
    ap.add_argument("--incremental-da", type=float, default=0.0, help="incremental pretax D&A from PPA ($M)")
    a = ap.parse_args()

    standalone = a.acq_ni / a.acq_shares
    pf_eps, d = pro_forma_eps(a)
    accr = pf_eps / standalone - 1

    premium_txt = ""
    if a.tgt_unaffected:
        prem = a.offer_price / a.tgt_unaffected - 1
        premium_txt = f", {prem*100:.0f}% premium"
    deal_pe = (a.offer_price * a.tgt_shares) / a.tgt_ni if a.tgt_ni else float("nan")
    acq_pe = a.acq_price / standalone

    verdict = "accretive ✅" if accr > 0.001 else ("dilutive ❌" if accr < -0.001 else "≈ neutral")
    print(f"**Merger accretion/dilution** (${a.offer_price:.2f}/sh offer{premium_txt})\n")
    print(f"Financing: {a.pct_stock*100:.0f}% stock / {a.pct_cash*100:.0f}% cash / {a.pct_debt*100:.0f}% debt")
    print(f"Acquirer P/E {acq_pe:.1f}x · target deal P/E {deal_pe:.1f}x")
    print(f"Standalone EPS ${standalone:.2f} → Pro-forma EPS ${pf_eps:.2f} → "
          f"**{accr*100:+.1f}% {verdict}**")
    print(f"(new shares {d['new_shares']:.0f}M · after-tax: new int ${d['new_interest']:.0f}M, "
          f"foregone int ${d['foregone_interest']:.0f}M, PPA D&A drag ${d['da_drag']:.0f}M)\n")

    be = breakeven_synergies(a)
    print(f"Breakeven synergies: ~${be:.0f}M/yr (pretax) for EPS-neutral; "
          f"deal assumes ${a.synergies:.0f}M.")

    # Financing comparison
    print("\nAccretion by financing mix:")
    print("| Mix | Pro-forma EPS | Δ |")
    print("|-----|---------------|---|")
    for label, (s, c, dpt) in [("100% stock", (1, 0, 0)), ("100% cash", (0, 1, 0)),
                               ("100% debt", (0, 0, 1)), ("as-entered", (a.pct_stock, a.pct_cash, a.pct_debt))]:
        eps, _ = pro_forma_eps(a, pct_stock=s, pct_cash=c, pct_debt=dpt)
        print(f"| {label} | ${eps:.2f} | {(eps/standalone-1)*100:+.1f}% |")


if __name__ == "__main__":
    main()
