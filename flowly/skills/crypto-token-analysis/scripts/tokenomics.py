#!/usr/bin/env python3
"""Token supply/FDV/unlock math — chat-ready tokenomics snapshot.

Stdlib only. Computes market cap, FDV, circulating %, unlock impact, inflation.

Example:
    token.py --price 1.50 --circulating 200000000 --max-supply 1000000000 \
        --daily-volume 30000000 \
        --next-unlock-tokens 50000000 --next-unlock-label "investor cliff (Aug)" \
        --annual-emissions 80000000 --annual-burns 0
"""
from __future__ import annotations

import argparse


def human(n):
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n/div:.2f}{unit}"
    return f"{n:.0f}"


def main():
    ap = argparse.ArgumentParser(description="Token supply / FDV / unlock math")
    ap.add_argument("--price", type=float, required=True)
    ap.add_argument("--circulating", type=float, required=True)
    ap.add_argument("--max-supply", type=float, help="max or fully-diluted supply")
    ap.add_argument("--total-supply", type=float, help="total minted (if no hard max)")
    ap.add_argument("--daily-volume", type=float, default=0.0)
    ap.add_argument("--next-unlock-tokens", type=float, default=0.0)
    ap.add_argument("--next-unlock-label", default="next unlock")
    ap.add_argument("--annual-emissions", type=float, default=0.0)
    ap.add_argument("--annual-burns", type=float, default=0.0)
    ap.add_argument("--annual-fees", type=float, default=0.0, help="annualized protocol fees/revenue")
    ap.add_argument("--insider-pct", type=float, default=None, help="team+VC allocation fraction")
    a = ap.parse_args()

    fd_supply = a.max_supply or a.total_supply or a.circulating
    mc = a.price * a.circulating
    fdv = a.price * fd_supply
    circ_pct = a.circulating / fd_supply if fd_supply else float("nan")

    print(f"**Tokenomics** (price ${a.price:,.4f})\n")
    print(f"Market cap ${human(mc)} · FDV ${human(fdv)} · "
          f"circulating {circ_pct*100:.0f}%"
          + ("  🚩 thin float / high dilution overhang" if circ_pct < 0.30 else
             ("  ⚠️ majority still locked" if circ_pct < 0.50 else "")))
    if fdv and mc:
        print(f"MC/FDV {mc/fdv:.2f} (lower = more supply still to unlock)")

    if a.next_unlock_tokens:
        pct_float = a.next_unlock_tokens / a.circulating if a.circulating else float("nan")
        line = (f"\nNext unlock: {human(a.next_unlock_tokens)} ({a.next_unlock_label}) "
                f"= {pct_float*100:.0f}% of float")
        if a.daily_volume:
            days = (a.next_unlock_tokens * a.price) / a.daily_volume
            line += f" ≈ {days:.1f} days' volume"
        flag = " 🚩" if pct_float > 0.10 else (" ⚠️" if pct_float > 0.05 else "")
        print(line + flag)

    if a.annual_emissions or a.annual_burns:
        net = a.annual_emissions - a.annual_burns
        infl = net / a.circulating if a.circulating else float("nan")
        desc = "inflation" if net >= 0 else "deflation"
        flag = " 🚩" if infl > 0.30 else (" ⚠️" if infl > 0.10 else "")
        print(f"\nEmissions {human(a.annual_emissions)}/yr"
              + (f" − burns {human(a.annual_burns)}" if a.annual_burns else "")
              + f" → net {infl*100:.0f}% {desc} on circulating{flag}")

    if a.insider_pct is not None:
        flag = " 🚩" if a.insider_pct > 0.40 else (" ⚠️" if a.insider_pct > 0.30 else "")
        print(f"\nInsider (team+VC) allocation {a.insider_pct*100:.0f}%{flag}")

    if a.annual_fees:
        pf = fdv / a.annual_fees
        print(f"\nAnnualized fees ${human(a.annual_fees)} → P/F {pf:.0f}x (FDV-based) — "
              f"compare to peer protocols")

    # Bottom-line flags
    flags = []
    if circ_pct < 0.30:
        flags.append("low circulating float vs FDV (dilution overhang)")
    if a.next_unlock_tokens and a.circulating and a.next_unlock_tokens / a.circulating > 0.10:
        flags.append("large near-term unlock relative to float")
    if a.annual_emissions and a.circulating and (a.annual_emissions - a.annual_burns) / a.circulating > 0.30:
        flags.append("high net inflation")
    if a.insider_pct is not None and a.insider_pct > 0.40:
        flags.append("heavy insider allocation")
    if flags:
        print("\n🚩 Key risks: " + "; ".join(flags))
    print("\n_Not financial advice. Verify supply/unlocks on-chain; timestamp the price._")


if __name__ == "__main__":
    main()
