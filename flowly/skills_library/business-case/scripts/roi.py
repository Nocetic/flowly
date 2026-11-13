#!/usr/bin/env python3
"""Business-case calculator — ROI, payback period, NPV, IRR. Stdlib only.
Chat-ready markdown.

Usage:
    roi.py simple --cost 50000 --benefit 80000
    roi.py payback --cost 50000 --annual 20000
    roi.py payback --cost 50000 --flows 10000 20000 30000        # uneven flows
    roi.py npv --rate 0.1 --initial 50000 --flows 20000 20000 20000 20000
"""
from __future__ import annotations

import argparse


def cmd_simple(a):
    net = a.benefit - a.cost
    roi = net / a.cost if a.cost else float("inf")
    print(f"**ROI** (cost ${a.cost:,.0f}, benefit ${a.benefit:,.0f})\n")
    print(f"Net benefit = ${net:,.0f}")
    print(f"ROI = net/cost = {roi*100:.0f}%"
          + ("  ✅ positive" if roi > 0 else "  ❌ negative"))


def cmd_payback(a):
    print(f"**Payback period** (cost ${a.cost:,.0f})\n")
    if a.annual is not None:
        if a.annual <= 0:
            print("annual benefit must be > 0"); return
        years = a.cost / a.annual
        print(f"Annual net benefit ${a.annual:,.0f} → payback = {years:.2f} years "
              f"({years*12:.1f} months)")
    elif a.flows:
        cum = 0.0
        for i, f in enumerate(a.flows, 1):
            prev = cum
            cum += f
            if cum >= a.cost:
                frac = (a.cost - prev) / f if f else 0
                period = (i - 1) + frac
                print(f"Cumulative flows reach ${a.cost:,.0f} in period {period:.2f}.")
                print(f"→ payback ≈ {period:.2f} periods ({period*12:.1f} months if annual).")
                return
        print(f"Not recovered within {len(a.flows)} periods (cumulative ${cum:,.0f} < cost).")
    else:
        print("give --annual or --flows")


def npv(rate, initial, flows):
    return -initial + sum(f / (1 + rate) ** t for t, f in enumerate(flows, 1))


def irr(initial, flows, lo=-0.99, hi=2.0):
    def f(r):
        return npv(r, initial, flows)
    flo, fhi = f(lo), f(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < 1e-6:
            return mid
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def cmd_npv(a):
    val = npv(a.rate, a.initial, a.flows)
    r = irr(a.initial, a.flows)
    print(f"**NPV** (rate {a.rate*100:.0f}%, initial ${a.initial:,.0f}, {len(a.flows)} periods)\n")
    print(f"Cash flows: {', '.join(f'${f:,.0f}' for f in a.flows)}")
    print(f"NPV = ${val:,.0f}  " + ("✅ value-creating (>0)" if val > 0 else "❌ destroys value (<0)"))
    if r is not None:
        print(f"IRR = {r*100:.1f}%  " + (f"(> hurdle {a.rate*100:.0f}% ✅)" if r > a.rate else f"(< hurdle {a.rate*100:.0f}% ❌)"))
    total = sum(a.flows)
    print(f"(Undiscounted total return ${total:,.0f} on ${a.initial:,.0f} = {total/a.initial:.1f}×)")


def main():
    ap = argparse.ArgumentParser(description="Business-case calculator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("simple"); p.add_argument("--cost", type=float, required=True); p.add_argument("--benefit", type=float, required=True); p.set_defaults(fn=cmd_simple)
    p = sub.add_parser("payback"); p.add_argument("--cost", type=float, required=True); p.add_argument("--annual", type=float); p.add_argument("--flows", type=float, nargs="+"); p.set_defaults(fn=cmd_payback)
    p = sub.add_parser("npv"); p.add_argument("--rate", type=float, required=True); p.add_argument("--initial", type=float, required=True); p.add_argument("--flows", type=float, nargs="+", required=True); p.set_defaults(fn=cmd_npv)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
