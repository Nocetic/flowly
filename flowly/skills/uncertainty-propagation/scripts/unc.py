#!/usr/bin/env python3
"""Uncertainty propagation — turn input errors into an output error. Stdlib only.

Given a formula and each input as value ± standard uncertainty, propagate to the
result two independent ways — first-order analytic (partial derivatives, the GUM
combined-uncertainty method) and Monte Carlo (sampling) — and show which input
dominates the error budget. Also: significant-figure formatting of a measurement,
and the uncertainty-weighted mean of repeated measurements.

Assumes inputs are INDEPENDENT (no covariance) and, for Monte Carlo, Gaussian.

Usage:
    unc.py propagate "V*I" --var V=2.00:0.05 --var I=0.50:0.01
    unc.py propagate "0.5*m*v**2" --var m=2.0:0.1 --var v=3.00:0.05
    unc.py sigfig --value 9.8124 --unc 0.032
    unc.py weighted "9.81:0.02,9.78:0.05,9.83:0.03"
"""
from __future__ import annotations

import argparse
import math
import random

_MATHNS = {k: getattr(math, k) for k in (
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh",
    "exp", "log", "log10", "log2", "sqrt", "pow", "pi", "e", "fabs", "floor", "ceil")}


def _make_f(expr):
    code = compile(expr, "<formula>", "eval")

    def f(values):
        return eval(code, {"__builtins__": {}}, {**_MATHNS, **values})  # noqa: S307 trusted CLI
    return f


def _partial(f, values, key):
    x = values[key]
    h = 1e-6 * (abs(x) if x != 0 else 1.0)
    up, dn = dict(values), dict(values)
    up[key], dn[key] = x + h, x - h
    return (f(up) - f(dn)) / (2 * h)


def _parse_vars(specs):
    values, uncs = {}, {}
    for s in specs:
        name, rest = s.split("=", 1)
        val, unc = rest.split(":", 1)
        values[name.strip()] = float(val)
        uncs[name.strip()] = float(unc)
    return values, uncs


def _round_measurement(value, unc):
    """Round uncertainty to 1 sig fig (2 if it starts with 1), value to match."""
    if unc <= 0 or not math.isfinite(unc):
        return f"{value:.4g}", f"{unc:.2g}"
    exp = math.floor(math.log10(abs(unc)))
    lead = unc / 10 ** exp
    sig = 2 if lead < 3 else 1           # keep 2 figs when the lead digit is 1 or 2
    dec = -(exp - (sig - 1))
    if dec >= 0:
        return f"{value:.{dec}f}", f"{unc:.{dec}f}"
    factor = 10 ** (-dec)
    return f"{round(value / factor) * factor:g}", f"{round(unc / factor) * factor:g}"


def cmd_propagate(a):
    values, uncs = _parse_vars(a.var)
    f = _make_f(a.expr)
    nominal = f(values)

    # Analytic (first-order): combined = sqrt( Σ (∂f/∂xᵢ · uᵢ)² )
    contribs = {}
    for k in values:
        d = _partial(f, values, k)
        contribs[k] = (d * uncs[k]) ** 2
    sigma_analytic = math.sqrt(sum(contribs.values()))

    # Monte Carlo (seeded → reproducible)
    rng = random.Random(12345)
    n = 100_000
    samples = []
    for _ in range(n):
        pt = {k: rng.gauss(values[k], uncs[k]) for k in values}
        try:
            samples.append(f(pt))
        except (ValueError, ZeroDivisionError):
            continue
    mc_mean = sum(samples) / len(samples)
    mc_sigma = math.sqrt(sum((s - mc_mean) ** 2 for s in samples) / (len(samples) - 1))

    vfmt, ufmt = _round_measurement(nominal, sigma_analytic)
    rel = sigma_analytic / abs(nominal) * 100 if nominal else float("nan")
    print(f"**Uncertainty propagation** — {a.expr}\n")
    print("Inputs: " + ", ".join(f"{k} = {values[k]:g} ± {uncs[k]:g}" for k in values))
    print(f"\nResult = {vfmt} ± {ufmt}")
    print(f"  analytic (1st-order):  σ = {sigma_analytic:.4g}   ({rel:.2g}% relative)")
    print(f"  Monte Carlo (n={n:,}):  σ = {mc_sigma:.4g}   mean = {mc_mean:.4g}")
    if abs(mc_sigma - sigma_analytic) > 0.15 * sigma_analytic:
        print("  ⚠ analytic and MC disagree >15% — formula is nonlinear over these"
              " uncertainties; trust the Monte Carlo value.")
    print("\nError budget (share of variance):")
    for k, v in sorted(contribs.items(), key=lambda kv: -kv[1]):
        share = v / sum(contribs.values()) * 100 if sum(contribs.values()) else 0
        print(f"  {k}: {share:5.1f}%   (±{math.sqrt(v):.4g} contributed)")


def cmd_sigfig(a):
    vfmt, ufmt = _round_measurement(a.value, a.unc)
    rel = a.unc / abs(a.value) * 100 if a.value else float("nan")
    print("**Significant figures**\n")
    print(f"Reported: {vfmt} ± {ufmt}   ({rel:.2g}% relative)")
    print("(Uncertainty → 1 sig fig, or 2 when it leads with 1–2; value rounded to"
          " the same decimal place. Never quote the value more precisely than its error.)")


def cmd_weighted(a):
    pairs = []
    for tok in a.data.split(","):
        v, u = tok.split(":")
        pairs.append((float(v), float(u)))
    weights = [1 / u ** 2 for _, u in pairs]
    wsum = sum(weights)
    mean = sum(w * v for (v, _), w in zip(pairs, weights)) / wsum
    sigma = math.sqrt(1 / wsum)
    vfmt, ufmt = _round_measurement(mean, sigma)
    print("**Uncertainty-weighted mean** (inverse-variance)\n")
    for v, u in pairs:
        print(f"  {v:g} ± {u:g}   (weight {1/u**2/wsum*100:.0f}%)")
    print(f"\nWeighted mean = {vfmt} ± {ufmt}")
    print("(Each measurement weighted by 1/σ²; the most precise dominates. The"
          " combined error is smaller than any single input.)")


def main():
    ap = argparse.ArgumentParser(description="Uncertainty propagation (stdlib)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propagate"); p.add_argument("expr"); p.add_argument("--var", action="append", required=True, help="name=value:uncertainty"); p.set_defaults(fn=cmd_propagate)
    p = sub.add_parser("sigfig"); p.add_argument("--value", type=float, required=True); p.add_argument("--unc", type=float, required=True); p.set_defaults(fn=cmd_sigfig)
    p = sub.add_parser("weighted"); p.add_argument("data", help="v1:u1,v2:u2,..."); p.set_defaults(fn=cmd_weighted)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
