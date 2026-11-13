#!/usr/bin/env python3
"""A/B test calculator — sample size, two-proportion z-test, means t-test, CIs.
Stdlib only (normal/t approximations). Chat-ready markdown.

Usage:
    abtest.py size --baseline 0.10 --mde 0.02 [--power 0.8] [--alpha 0.05]
    abtest.py size --baseline 0.10 --mde-rel 0.10
    abtest.py prop --a 1000 --conv-a 100 --b 1000 --conv-b 130 [--alpha 0.05]
    abtest.py means --mean-a 50 --sd-a 12 --n-a 500 --mean-b 53 --sd-b 13 --n-b 500
"""
from __future__ import annotations

import argparse
import math

# z for common two-sided alpha / one-sided power
Z = {0.10: 1.2816, 0.05: 1.9600, 0.01: 2.5758, 0.001: 3.2905}
Z_POWER = {0.80: 0.8416, 0.90: 1.2816, 0.95: 1.6449, 0.99: 2.3263}


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def two_sided_p(z):
    return 2 * (1 - norm_cdf(abs(z)))


def cmd_size(a):
    p1 = a.baseline
    if a.mde_rel is not None:
        p2 = p1 * (1 + a.mde_rel)
        mde = p2 - p1
    else:
        mde = a.mde
        p2 = p1 + mde
    za = Z.get(round(a.alpha, 3), 1.96)
    zb = Z_POWER.get(round(a.power, 2), 0.8416)
    pbar = (p1 + p2) / 2
    # standard two-proportion sample size per arm
    n = ((za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2) / (mde ** 2)
    n = math.ceil(n)
    print(f"**Sample size** (baseline {p1*100:.1f}%, detect → {p2*100:.1f}%, "
          f"abs MDE {mde*100:.2f}pp, α={a.alpha}, power={a.power})\n")
    print(f"Required ≈ **{n:,} per arm** ({2*n:,} total).")
    print(f"At a smaller MDE this grows ~quadratically. Run to this N over full business cycles; "
          f"don't stop early.")


def cmd_prop(a):
    pa = a.conv_a / a.a
    pb = a.conv_b / a.b
    diff = pb - pa
    rel = diff / pa if pa else float("nan")
    # pooled for the test
    pool = (a.conv_a + a.conv_b) / (a.a + a.b)
    se_pool = math.sqrt(pool * (1 - pool) * (1 / a.a + 1 / a.b))
    z = diff / se_pool if se_pool else 0.0
    p = two_sided_p(z)
    # unpooled SE for the CI on the difference
    se_diff = math.sqrt(pa * (1 - pa) / a.a + pb * (1 - pb) / a.b)
    zc = Z.get(round(a.alpha, 3), 1.96)
    lo, hi = diff - zc * se_diff, diff + zc * se_diff
    print(f"**Two-proportion test (B vs A)**\n")
    print(f"A: {a.conv_a}/{a.a} = {pa*100:.2f}%   B: {a.conv_b}/{a.b} = {pb*100:.2f}%")
    print(f"Absolute lift {diff*100:+.2f}pp · relative {rel*100:+.1f}%")
    print(f"z = {z:.2f}, p = {p:.4f} (two-sided) → "
          + ("significant ✅" if p < a.alpha else "NOT significant ❌") + f" at α={a.alpha}")
    print(f"{int((1-a.alpha)*100)}% CI on difference: [{lo*100:+.2f}pp, {hi*100:+.2f}pp]"
          + (" (excludes 0)" if lo * hi > 0 else " (includes 0 — inconclusive)"))
    print("\n_Significance ≠ importance: check the lift clears your MDE and the test wasn't peeked/underpowered._")


def cmd_means(a):
    diff = a.mean_b - a.mean_a
    se = math.sqrt(a.sd_a ** 2 / a.n_a + a.sd_b ** 2 / a.n_b)
    t = diff / se if se else 0.0
    # large-n: normal approximation for p and CI
    p = two_sided_p(t)
    zc = Z.get(round(a.alpha, 3), 1.96)
    lo, hi = diff - zc * se, diff + zc * se
    print(f"**Means test (B vs A)** (Welch, normal approx for large n)\n")
    print(f"A: {a.mean_a} ± {a.sd_a} (n={a.n_a})   B: {a.mean_b} ± {a.sd_b} (n={a.n_b})")
    print(f"Difference {diff:+.4g} · t≈{t:.2f}, p = {p:.4f} → "
          + ("significant ✅" if p < a.alpha else "NOT significant ❌") + f" at α={a.alpha}")
    print(f"{int((1-a.alpha)*100)}% CI on difference: [{lo:+.4g}, {hi:+.4g}]"
          + (" (excludes 0)" if lo * hi > 0 else " (includes 0)"))
    print("\n_For skewed data (e.g. revenue) prefer a bootstrap or rank test — means are outlier-sensitive._")


def main():
    ap = argparse.ArgumentParser(description="A/B test calculator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("size"); p.add_argument("--baseline", type=float, required=True); p.add_argument("--mde", type=float); p.add_argument("--mde-rel", type=float); p.add_argument("--power", type=float, default=0.8); p.add_argument("--alpha", type=float, default=0.05); p.set_defaults(fn=cmd_size)
    p = sub.add_parser("prop"); [p.add_argument(f"--{x}", type=int, required=True) for x in ("a", "b")]; p.add_argument("--conv-a", type=int, required=True); p.add_argument("--conv-b", type=int, required=True); p.add_argument("--alpha", type=float, default=0.05); p.set_defaults(fn=cmd_prop)
    p = sub.add_parser("means"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("mean-a", "sd-a", "n-a", "mean-b", "sd-b", "n-b")]; p.add_argument("--alpha", type=float, default=0.05); p.set_defaults(fn=cmd_means)
    a = ap.parse_args()
    if a.cmd == "size" and a.mde is None and a.mde_rel is None:
        ap.error("size needs --mde or --mde-rel")
    a.fn(a)


if __name__ == "__main__":
    main()
