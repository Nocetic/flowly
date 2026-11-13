#!/usr/bin/env python3
"""Market-sizing calculator — bottom-up, top-down, reconcile. Stdlib only.
Chat-ready markdown.

Usage:
    sizing.py bottomup --customers 2000000 --penetration 0.05 --price 1200
    sizing.py topdown --total 50e9 --slices 0.4 0.5 0.3
    sizing.py reconcile --a 1.2e9 --b 0.9e9
"""
from __future__ import annotations

import argparse


def human(n):
    for u, d in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= d:
            return f"${n/d:.2f}{u}"
    return f"${n:,.0f}"


def cmd_bottomup(a):
    val = a.customers * a.penetration * a.price
    print(f"**Bottom-up market size**\n")
    print(f"{a.customers:,.0f} potential customers × {a.penetration*100:.1f}% penetration "
          f"× ${a.price:,.0f} = **{human(val)}/period**")
    served = a.customers * a.penetration
    print(f"(= {served:,.0f} paying customers)")
    if a.som_share:
        som = val * a.som_share
        print(f"SOM at {a.som_share*100:.0f}% obtainable share = {human(som)}/period")


def cmd_topdown(a):
    val = a.total
    chain = [human(val)]
    for s in a.slices:
        val *= s
        chain.append(f"×{s*100:.0f}%")
    print(f"**Top-down market size**\n")
    print(f"{' '.join(chain)} = **{human(val)}**")
    print(f"(Cite the source + date for the ${a.total:,.0f} starting figure.)")


def cmd_reconcile(a):
    lo, hi = sorted((a.a, a.b))
    ratio = hi / lo if lo else float("inf")
    print(f"**Reconcile estimates**\n")
    print(f"Estimate A = {human(a.a)} · Estimate B = {human(a.b)}")
    print(f"Ratio = {ratio:.2f}×")
    if ratio <= 2:
        mid = (a.a + a.b) / 2
        print(f"✅ Within 2× — defensible. Range {human(lo)}–{human(hi)} (midpoint {human(mid)}).")
    else:
        print(f"⚠️ Differ by {ratio:.1f}× — an assumption is off. Check top-down segment %s "
              f"and bottom-up penetration; don't average blindly.")


def main():
    ap = argparse.ArgumentParser(description="Market-sizing calculator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bottomup"); p.add_argument("--customers", type=float, required=True); p.add_argument("--penetration", type=float, required=True); p.add_argument("--price", type=float, required=True); p.add_argument("--som-share", type=float); p.set_defaults(fn=cmd_bottomup)
    p = sub.add_parser("topdown"); p.add_argument("--total", type=float, required=True); p.add_argument("--slices", type=float, nargs="+", required=True); p.set_defaults(fn=cmd_topdown)
    p = sub.add_parser("reconcile"); p.add_argument("--a", type=float, required=True); p.add_argument("--b", type=float, required=True); p.set_defaults(fn=cmd_reconcile)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
